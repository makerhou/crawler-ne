"""Google News 链接解码（googlenewsdecoder）。

库用法：
    from googlenewsdecoder import gnewsdecoder
    result = gnewsdecoder(google_url, interval=1)   # 可选 proxy=...
    result -> {"status": bool, "decoded_url": str, "message": str}

注意：每次解码都会请求 Google，必须靠 interval 控制节奏，否则容易 429。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("reuters-crawler")


def decode_google_news_url(
    google_url: str,
    interval: int = 1,
    proxy: str | None = None,
) -> str | None:
    """把 Google News 链接解码为真实文章 URL。

    :return: 解码后的真实 URL；失败返回 None（失败仅记录日志，不抛异常，
             以便调用方跳过该条继续处理其它条目）
    """
    if not google_url:
        return None

    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        logger.error("未安装 googlenewsdecoder，请执行 pip install -r requirements.txt")
        return None

    try:
        kwargs: dict[str, object] = {"interval": interval}
        if proxy:
            kwargs["proxy"] = proxy
        result = gnewsdecoder(google_url, **kwargs)
    except Exception as exc:  # 解码库内部异常不应中断整轮任务
        logger.warning("解码异常（跳过该条）: %s | url=%s", exc, google_url[:80])
        return None

    # ⚠️ googlenewsdecoder 各版本返回的成功键不同：新版用 "success"，
    # 旧版用 "status"（见需求 11.9）。只认 "status" 会把新版**解码成功**的条目
    # 误判为失败（且成功返回无 "message" 键 → 日志打出 None），导致整轮条目被
    # 全部跳过。此处两者兼容：success 优先，缺失时回退 status。
    if isinstance(result, dict):
        success = result.get("success")
        if success is None:
            success = result.get("status")
        if success:
            decoded = result.get("decoded_url")
            if decoded:
                return str(decoded)
            logger.warning("解码返回空 URL: %s", google_url[:80])
            return None

    message = "未知错误"
    if isinstance(result, dict):
        # 失败但库未给 message 时用占位文案，避免日志打出 None 难以区分原因
        message = result.get("message") or "（库未返回错误信息）"
    logger.warning("解码失败（跳过该条）: %s | url=%s", message, google_url[:80])
    return None
