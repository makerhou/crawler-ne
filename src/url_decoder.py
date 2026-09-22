"""Google News 链接解码（三级 fallback 策略）。

解码顺序：
  1. Base64 本地解码（零网络请求、不怕封、最快）
  2. googlenewsdecoder 库解码（需网络请求 Google）
  3. HTTP 重定向跟踪（最朴素但易被反爬）

库用法（≥0.2.x）：
    from googlenewsdecoder import gnewsdecoder
    result = gnewsdecoder(google_url, interval=1)
    result -> {"success": bool, "decoded_url"|"url": str, "message": str}

注意：库解码每次请求 Google，必须靠 interval 控制节奏，否则容易 429。
"""

from __future__ import annotations

import base64
import logging
import re

logger = logging.getLogger("reuters-crawler")


# ────────────────────────────────────────────────────────────
# 1. Base64 本地解码（零网络、最快、不怕封）
# ────────────────────────────────────────────────────────────

def _decode_base64(google_url: str) -> str | None:
    """本地 Base64 / Protobuf 解码：从 Google News RSS URL 提取原始 URL。

    适用于 ``/articles/CBM...`` 格式的 RSS 链接。
    2024 年后部分新 ID 不再含明文 URL，此时返回 None。
    """
    match = re.search(r"/articles/([a-zA-Z0-9_-]+)", google_url)
    if not match:
        return None

    b64_str = match.group(1)
    # 补齐 base64 padding
    b64_str += "=" * (-len(b64_str) % 4)

    try:
        decoded_bytes = base64.b64decode(b64_str)
        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")

        # 从 protobuf 混合内容中提取 http(s) URL
        url_match = re.search(r"(https?://[^\s\x00-\x1f]+)", decoded_text)
        if url_match:
            raw_url = url_match.group(1)
            # 去除 protobuf 尾部控制字符
            raw_url = raw_url.rstrip("\x12\x08\x1a\x02\x08\x01\x00")
            if raw_url and ("://" in raw_url) and len(raw_url) > 15:
                return raw_url
    except Exception:
        pass

    return None


# ────────────────────────────────────────────────────────────
# 2. googlenewsdecoder 库解码（主力）
# ────────────────────────────────────────────────────────────

def _decode_with_library(
    google_url: str,
    interval: int,
    proxy: str | None,
) -> str | None:
    """通过 googlenewsdecoder 库解码（需网络请求）。

    兼容新旧版本：
    - 新版 (≥0.2.x) 返回 ``{"success": True, "decoded_url": ...}``
    - 旧版返回 ``{"status": True, "decoded_url": ...}``
    """
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        logger.debug("googlenewsdecoder 未安装，跳过库解码")
        return None

    try:
        kwargs: dict[str, object] = {"interval": interval}
        if proxy:
            kwargs["proxy"] = proxy
        result = gnewsdecoder(google_url, **kwargs)
    except Exception as exc:
        logger.debug("库解码异常: %s", exc)
        return None

    if not isinstance(result, dict):
        return None

    # 兼容 success / status
    success = result.get("success")
    if success is None:
        success = result.get("status")

    if success:
        # 兼容 decoded_url / url
        decoded = result.get("decoded_url") or result.get("url")
        if decoded:
            return str(decoded)

    return None


# ────────────────────────────────────────────────────────────
# 3. HTTP 重定向跟踪（备用，易被反爬）
# ────────────────────────────────────────────────────────────

def _decode_via_redirect(google_url: str, proxy: str | None) -> str | None:
    """通过 HTTP GET（allow_redirects=False）提取 Google News 重定向 Location。"""
    try:
        import requests
    except ImportError:
        return None

    try:
        kwargs: dict[str, object] = {
            "allow_redirects": False,
            "timeout": 10,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        }
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}

        resp = requests.get(google_url, **kwargs)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if location and "news.google.com" not in location:
                return location
    except Exception as exc:
        logger.debug("重定向跟踪异常: %s", exc)

    return None


# ────────────────────────────────────────────────────────────
# 对外接口
# ────────────────────────────────────────────────────────────

def decode_google_news_url(
    google_url: str,
    interval: int = 1,
    proxy: str | None = None,
) -> str | None:
    """把 Google News 链接解码为真实文章 URL（三级 fallback）。

    解码顺序：
      1. Base64 本地解码 — 零网络请求、最快、不怕封
      2. googlenewsdecoder 库 — 主力网络解码
      3. HTTP 重定向跟踪 — 最朴素的备用方案

    :return: 解码后的真实 URL；全部失败返回 None
    """
    if not google_url:
        return None

    # ── 1) Base64 本地解码（无网络）
    url = _decode_base64(google_url)
    if url:
        logger.debug("base64 解码成功: %s → %s", google_url[:60], url[:80])
        return url

    # ── 2) 库解码（网络）
    url = _decode_with_library(google_url, interval, proxy)
    if url:
        logger.debug("库解码成功: %s → %s", google_url[:60], url[:80])
        return url

    # ── 3) HTTP 重定向（网络）
    url = _decode_via_redirect(google_url, proxy)
    if url:
        logger.debug("重定向解码成功: %s → %s", google_url[:60], url[:80])
        return url

    logger.warning("解码失败（跳过该条）: 三级解码均失败 | url=%s", google_url[:120])
    return None
