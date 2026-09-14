"""Google News RSS 拉取与解析。

Google News RSS 的条目结构：
    <item>
      <title>文章标题 - Reuters</title>
      <link>https://news.google.com/rss/articles/CBMi...</link>   ← 需解码
      <pubDate>Mon, 10 Sep 2026 10:00:00 GMT</pubDate>
      <source url="...">Reuters</source>
      <description>...</description>
    </item>
"""

from __future__ import annotations

import logging
import re
import time

import feedparser
import requests

logger = logging.getLogger("reuters-crawler")

# 标题里 Google News 常追加的 " - 来源名" 后缀
_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,40}$")


def clean_title(raw_title: str) -> str:
    """去掉标题末尾的 ' - Reuters' 之类来源后缀。"""
    title = (raw_title or "").strip()
    if not title:
        return ""
    cleaned = _TITLE_SUFFIX_RE.sub("", title).strip()
    # 防御：清理后为空则保留原标题
    return cleaned or title


def fetch_rss_entries(
    rss_url: str,
    timeout: int = 30,
    user_agent: str = "",
    proxies: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    ua_pool: "UserAgentPool | None" = None,
) -> list[dict[str, str]]:
    """拉取 RSS 并返回条目列表（内置重试 + UA 轮换）。

    Google News 对代理 IP 存在**偶发 503 限流**（实测：同样请求头有时 200 有时 503），
    因此必须重试；同时「只带 User-Agent 不带 Accept」必然 503，故固定带完整请求头。
    传入 `ua_pool` 后每次尝试都会轮换身份，降低被识别概率。

    :raises requests.RequestException: 重试耗尽后仍失败
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        # 注意：Google News RSS 对「只带 User-Agent、不带 Accept」的请求会返回 503
        if ua_pool is not None:
            headers = ua_pool.build_headers("rss")
        else:
            headers = {
                "Accept": "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7",
                "Accept-Language": "en-US,en;q=0.9",
            }
            if user_agent:
                headers["User-Agent"] = user_agent

        try:
            response = requests.get(
                rss_url,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
            )
            response.raise_for_status()
            return _parse_feed(response.content)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                wait = backoff**attempt
                logger.warning(
                    "RSS 拉取失败（%d/%d）: %s，%.0fs 后重试", attempt, retries, exc, wait
                )
                time.sleep(wait)
            else:
                logger.error("RSS 拉取失败，已重试 %d 次: %s", retries, exc)

    raise last_error  # type: ignore[misc]


def _parse_feed(content: bytes) -> list[dict[str, str]]:
    """解析 RSS 内容为条目列表。"""
    feed = feedparser.parse(content)
    entries: list[dict[str, str]] = []

    for item in feed.entries:
        link = getattr(item, "link", "") or ""
        if not link:
            continue
        entries.append(
            {
                "title": clean_title(getattr(item, "title", "") or ""),
                "google_url": link,
                "published": getattr(item, "published", "") or getattr(item, "pubDate", "") or "",
                "summary": getattr(item, "summary", "") or "",
            }
        )

    return entries
