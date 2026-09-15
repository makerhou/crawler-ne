"""调度：每 5 分钟执行一轮抓取，循环运行。

单轮流程：拉 RSS → 解码 → 去重 → 抓正文 → 入库 → 触发 LLM → 写日志。
单条失败不中断整轮；整轮失败记录日志后等待下一轮。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any

from .article_parser import build_excerpt, fetch_article_detail, strip_html
from .browser_fetcher import BrowserFetcher
from .config import Config
from .repository import ArticleRepository
from .nodriver_fetcher import NodriverFetcher
from .rss_fetcher import fetch_rss_entries
from .url_decoder import decode_google_news_url
from .user_agents import UserAgentPool

logger = logging.getLogger("reuters-crawler")


def create_ua_pool(config: Config) -> UserAgentPool | None:
    """按配置创建 UA 身份池；`off` 时返回 None（使用固定 USER_AGENT）。"""
    if config.ua_rotation == "off":
        return None
    return UserAgentPool(strategy=config.ua_rotation)


def create_nodriver(config: Config, ua_pool: UserAgentPool | None) -> NodriverFetcher | None:
    """创建 nodriver 反检测抓取器（仅在 fetch_mode 需要时）。

    nodriver 需 Python 3.10+；不可用时 `available` 为 False，通道会自动跳过。
    """
    if config.fetch_mode not in ("auto", "nodriver"):
        return None

    fetcher = NodriverFetcher(
        ua_pool=ua_pool or UserAgentPool(),
        proxies=config.proxies,
        proxy_pool=config.proxy_pool,
        headless=config.nodriver_headless,
        wait_seconds=config.nodriver_wait_seconds,
        max_switch=config.nodriver_max_switch,
        request_interval=config.nodriver_request_interval,
        # 开启时把被拦的 HTML 落到 {LOG_DIR}/blocked/，用于定位挑战页类型
        dump_dir=os.path.join(config.log_dir, "blocked") if config.nodriver_dump_blocked else "",
        fail_fast_threshold=config.nodriver_fail_fast_threshold,
    )

    # 创建时自检一次：环境不满足则整个通道跳过，避免每篇文章都白试一次
    if not fetcher.available:
        logger.warning(
            "nodriver 通道不可用（需 Python 3.10+ 且系统装有 Chrome），已跳过；"
            "其余通道不受影响"
        )
        return None
    return fetcher


def _log_dry_run(index: int, title: str, url: str, detail: dict, publish_time: str, content: str) -> None:
    """dry-run 模式：打印挖掘到的文章内容（不入库）。"""
    logger.info("-" * 70)
    logger.info("[%d] 标题  : %s", index, title[:100])
    logger.info("     URL  : %s", url[:120])
    logger.info("     作者  : %s", detail.get("author") or "-")
    logger.info("     时间  : %s", publish_time or "-")
    logger.info("     正文  : %d 字", len(content))
    logger.info("     摘要  : %s", content[:200].replace("\n", " "))


def run_once(
    config: Config,
    repo: ArticleRepository | None = None,
    browser: BrowserFetcher | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """执行一轮抓取，返回统计 {"added", "skipped", "failed"}。

    :param repo: 为 None 时不查重/不入库（诊断用）
    :param dry_run: 只打印挖掘结果，不写数据库（需配合 repo=None 或独立使用）
    """
    started = time.time()
    stats = {"added": 0, "skipped": 0, "failed": 0}

    ua_pool = create_ua_pool(config)
    nodriver = create_nodriver(config, ua_pool)
    entries = fetch_rss_entries(
        rss_url=config.rss_url,
        timeout=config.http_timeout,
        user_agent=config.user_agent,
        proxies=config.proxies,
        ua_pool=ua_pool,
    )
    logger.info("RSS 拉取完成，条目数=%d", len(entries))

    # 1) 解码 + 去重
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        real_url = decode_google_news_url(
            entry["google_url"],
            interval=config.decode_interval,
            proxy=(config.proxies or {}).get("https") if config.proxies else None,
        )
        if not real_url:
            stats["failed"] += 1
            continue

        if repo is not None and repo.find_article_id_by_url(real_url):
            stats["skipped"] += 1
            continue

        candidates.append({**entry, "url": real_url})
        if len(candidates) >= config.max_per_round:
            logger.info("达到单轮上限 %d，停止处理剩余条目", config.max_per_round)
            break

    logger.info(
        "待入库=%d（跳过已存在 %d，解码失败 %d）",
        len(candidates),
        stats["skipped"],
        stats["failed"],
    )

    # 2) 抓正文 + 入库
    for candidate in candidates:
        url = candidate["url"]
        try:
            detail = fetch_article_detail(
                url,
                timeout=config.http_timeout,
                user_agent=config.user_agent,
                proxies=config.proxies,
                browser=browser,
                fetch_mode=config.fetch_mode,
                ua_pool=ua_pool,
                impersonate=config.impersonate,
                nodriver=nodriver,
            )
        except Exception as exc:
            stats["failed"] += 1
            logger.warning("正文抓取失败（跳过）: %s | url=%s", exc, url[:100])
            continue

        content = detail.get("content") or ""
        strategy = "trafilatura"

        if not content:
            # 降级：站点反爬导致抓不到正文时，用 RSS 摘要入库（保证有数据，后续可补抓）
            fallback = strip_html(candidate.get("summary"))
            if config.allow_rss_fallback and fallback:
                content = fallback
                strategy = "rss_fallback"
                stats["degraded"] = stats.get("degraded", 0) + 1
                logger.warning("抓不到正文，降级使用 RSS 摘要: %s", url[:100])
            else:
                stats["failed"] += 1
                logger.warning("正文为空（跳过）: %s", url[:100])
                continue

        publish_time = candidate.get("published") or detail.get("publish_time") or ""
        title = detail.get("title") or candidate.get("title") or ""

        if dry_run or repo is None:
            stats["added"] += 1
            _log_dry_run(stats["added"], title, url, detail, publish_time, content)
            continue

        article_id = repo.insert_article(
            {
                "uuid": str(uuid.uuid4()),
                "category": "reuters",
                "title": title,
                "summary": candidate.get("summary") or build_excerpt(content, 200) or "",
                "url": url,
                "content": content,
                "long_excerpt": build_excerpt(content, 500),
                "publish_time": str(publish_time),
                "content_length": len(content),
                "extraction_strategy": strategy,
                "from_platform": "reuters",
                "data_source": "google_news_rss",
                "extract_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        author = detail.get("author")
        if author:
            author_id = repo.upsert_author(str(author), str(uuid.uuid4()))
            repo.link_article_author(article_id, author_id)

        if config.trigger_llm:
            repo.upsert_analysis_task(article_id)

        stats["added"] += 1
        logger.info("入库成功 id=%d title=%s", article_id, title[:60])

    duration_ms = int((time.time() - started) * 1000)
    logger.info(
        "本轮完成：新增=%d 跳过=%d 失败=%d 耗时=%dms",
        stats["added"],
        stats["skipped"],
        stats["failed"],
        duration_ms,
    )
    if repo is not None:
        repo.write_crawler_log(
            level="info",
            action="crawl",
            message=f"Reuters 抓取: 新增 {stats['added']}, 跳过 {stats['skipped']}, 失败 {stats['failed']}",
            is_success=True,
            added=stats["added"],
            skipped=stats["skipped"],
            failed=stats["failed"],
            duration_ms=duration_ms,
        )
    return stats


def create_browser(config: Config) -> BrowserFetcher | None:
    """按配置创建浏览器实例；`requests` 模式返回 None。"""
    if config.fetch_mode not in ("auto", "playwright"):
        return None
    return BrowserFetcher(
        user_agent=config.user_agent,
        timeout_ms=config.browser_timeout_ms,
        headless=config.browser_headless,
        wait_selector=config.browser_wait_selector,
        wait_ms=config.browser_wait_ms,
        proxies=config.proxies,
    )


def run_forever(config: Config, repo: ArticleRepository, stop_event=None) -> None:
    """循环执行，直到 stop_event 被设置（用于 systemd 优雅退出）。

    浏览器实例全程复用（每篇都启停会严重拖慢单轮耗时）。
    """
    logger.info("爬虫启动: %r | 抓取模式=%s", config, config.fetch_mode)

    browser = create_browser(config)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("收到停止信号，退出循环")
                break

            try:
                run_once(config, repo, browser)
            except Exception as exc:
                # 整轮失败：记录原因，等待下一轮（systemd 保证进程存活）
                logger.exception("本轮执行失败: %s", exc)
                try:
                    repo.write_crawler_log(
                        level="error",
                        action="crawl",
                        message=f"本轮执行失败: {exc}",
                        is_success=False,
                    )
                except Exception:
                    logger.exception("写失败日志时再次异常（忽略）")

            if stop_event is not None and stop_event.wait(config.interval_seconds):
                logger.info("收到停止信号，退出循环")
                break
            elif stop_event is None:
                time.sleep(config.interval_seconds)
    finally:
        if browser is not None:
            browser.close()
