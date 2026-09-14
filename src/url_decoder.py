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

    if isinstance(result, dict) and result.get("status"):
        decoded = result.get("decoded_url")
        if decoded:
            return str(decoded)
        logger.warning("解码返回空 URL: %s", google_url[:80])
        return None

    message = result.get("message") if isinstance(result, dict) else "未知错误"
    logger.warning("解码失败（跳过该条）: %s | url=%s", message, google_url[:80])
    return None
