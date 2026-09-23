"""调度：每 5 分钟执行一轮抓取，循环运行。

单轮流程：拉 RSS → 解码 → 去重 → 抓正文 → 入库 → 触发 LLM → 写日志。
单条失败不中断整轮；整轮失败记录日志后等待下一轮。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .article_parser import build_excerpt, fetch_article_detail, strip_html
from .browser_fetcher import BrowserFetcher
from .config import Config
from .repository import ArticleRepository
from .nodriver_fetcher import NodriverFetcher
from .node_rotator import NodeRotator
from .rss_fetcher import fetch_rss_entries
from .url_decoder import decode_google_news_url
from .user_agents import UserAgentPool

logger = logging.getLogger("reuters-crawler")


def create_ua_pool(config: Config) -> UserAgentPool | None:
    """按配置创建 UA 身份池；`off` 时返回 None（使用固定 USER_AGENT）。"""
    if config.ua_rotation == "off":
        return None
    return UserAgentPool(strategy=config.ua_rotation)


def create_rotator(config: Config) -> NodeRotator | None:
    """创建节点轮换器（命中 DataDome 时换出口 IP）；未配置面板则返回 None。

    未配置时自动换 IP 能力关闭，保持原有的「单代理 + 换身份重试」行为。
    """
    if not config.rotate_on_block:
        return None

    rotator = NodeRotator(
        panel_url=config.node_panel_url,
        session=config.node_panel_session,
        proxies=config.proxies,
        panel_proxy=config.node_panel_proxy,
        precheck=config.node_precheck,
        max_candidates=config.max_node_candidates,
    )
    if not rotator.enabled:
        logger.info(
            "未配置 NODE_PANEL_URL/NODE_PANEL_SESSION，自动换 IP 关闭"
            "（保持单代理 + 换身份重试）"
        )
        return None
    return rotator


def create_nodriver(
    config: Config,
    ua_pool: UserAgentPool | None,
    rotator: NodeRotator | None = None,
) -> NodriverFetcher | None:
    """创建 nodriver 反检测抓取器（仅在 fetch_mode 需要时）。

    nodriver 需 Python 3.10+；不可用时 `available` 为 False，通道会自动跳过。

    `rotator` 由调度层统一创建后传入，与 scheduler 共享同一个实例（换 IP 时
    nodriver 内部和 scheduler 外层使用同一个轮换器，避免状态不一致）。
    """
    if config.fetch_mode not in ("auto", "nodriver"):
        return None

    fetcher = NodriverFetcher(
        ua_pool=ua_pool or UserAgentPool(),
        proxies=config.proxies,
        headless=config.nodriver_headless,
        wait_seconds=config.nodriver_wait_seconds,
        max_switch=config.nodriver_max_switch,
        request_interval=config.nodriver_request_interval,
        rotator=rotator,
        max_ip_switch=config.max_ip_switch,
    )

    # 创建时自检一次：环境不满足则整个通道跳过，避免每篇文章都白试一次
    if not fetcher.available:
        logger.warning(
            "nodriver 通道不可用（需 Python 3.10+ 且系统装有 Chrome），已跳过；"
            "其余通道不受影响"
        )
        return None
    return fetcher


def _slugify(text: str, max_len: int = 50) -> str:
    """生成文件名安全的 slug（仅保留字母/数字/下划线/连字符）。"""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return slug[:max_len] or "article"


def _save_dry_run_article(
    out_dir: Path,
    index: int,
    title: str,
    url: str,
    author: str | None,
    publish_time: str,
    content: str,
    strategy: str,
    images: list[dict[str, Any]] | None = None,
) -> Path:
    """把单篇 dry-run 结果写成 JSON（含全文 + 图片位置列表），返回文件路径。"""
    safe = _slugify(title)
    path = out_dir / f"{index:02d}_{safe}.json"
    payload = {
        "index": index,
        "title": title,
        "url": url,
        "author": author,
        "publish_time": publish_time or None,
        "content_length": len(content),
        "extraction_strategy": strategy,
        "images": images or [],
        "content": content,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _log_dry_run(
    index: int,
    title: str,
    url: str,
    detail: dict,
    publish_time: str,
    content: str,
    strategy: str = "trafilatura",
    out_dir: Path | None = None,
) -> None:
    """dry-run 模式：打印挖掘到的文章内容（不入库），可选落本地 JSON 便于核对全文。"""
    logger.info("-" * 70)
    logger.info("[%d] 标题  : %s", index, title[:100])
    logger.info("     URL  : %s", url[:120])
    logger.info("     作者  : %s", detail.get("author") or "-")
    logger.info("     时间  : %s", publish_time or "-")
    logger.info("     策略  : %s", strategy)
    logger.info("     正文  : %d 字", len(content))
    logger.info("     摘要  : %s", content[:200].replace("\n", " "))

    images = detail.get("images") or []
    logger.info("     图片  : %d 张", len(images))
    for img in images[:3]:
        logger.info("       - %s (pos=%s)", img.get("url", "")[:150], img.get("position"))

    if out_dir is not None:
        path = _save_dry_run_article(
            out_dir, index, title, url, detail.get("author"), publish_time, content, strategy,
            images=images,
        )
        logger.info("     已落盘: %s", path)


def run_once(
    config: Config,
    repo: ArticleRepository | None = None,
    browser: BrowserFetcher | None = None,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    nodriver: NodriverFetcher | None = None,
) -> dict[str, int]:
    """执行一轮抓取，返回统计 {"added", "skipped", "failed"}。

    :param repo: 为 None 时不查重/不入库（诊断用）
    :param dry_run: 只打印挖掘结果，不写数据库（需配合 repo=None 或独立使用）
    :param stop_event: 收到退出信号时立即中止当前轮次
    :param nodriver: 外部传入时复用（持久化 event loop），为 None 时内部创建
    """
    started = time.time()
    stats = {"added": 0, "skipped": 0, "failed": 0}

    # dry-run 时每轮建独立目录，把完整文章落本地 JSON 便于核对（不入库）
    out_dir: Path | None = None
    if dry_run:
        out_dir = Path("data") / f"dryrun_{datetime.now():%Y%m%d_%H%M%S}"
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("dry-run 落盘目录: %s", out_dir)

    ua_pool = create_ua_pool(config)
    rotator = create_rotator(config)
    _nodriver_owned = nodriver is None
    if _nodriver_owned:
        nodriver = create_nodriver(config, ua_pool, rotator=rotator)
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
        if stop_event is not None and stop_event.is_set():
            logger.info("收到停止信号，中止解码")
            break
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
        if stop_event is not None and stop_event.is_set():
            logger.info("收到停止信号，中止抓取")
            break
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
                rotator=rotator,
                max_ip_switch=config.max_ip_switch,
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
            _log_dry_run(
                stats["added"], title, url, detail, publish_time, content,
                strategy=strategy, out_dir=out_dir,
            )
            continue

        article_id = repo.insert_article(
            {
                "uuid": str(uuid.uuid4()),
                "category": "reuters",
                "title": title,
                "summary": candidate.get("summary") or build_excerpt(content, 200) or "",
                "url": url,
                "content": content,
                # CloudBase MySQL 的 JSON 列要求传入 JSON 字符串（而非对象/数组），
                # 否则后端会把数组展开成多列 → SQL 1241 (Operand should contain 1 column(s))
                "images": json.dumps(detail.get("images") or []),
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

    # 内部创建的 nodriver 需在本轮结束时关闭（外部传入的由调用方管理）
    if _nodriver_owned and nodriver is not None:
        nodriver.close()

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

    浏览器 / nodriver 实例全程复用（每篇都启停会严重拖慢单轮耗时；
    nodriver 的持久化 event loop 也必须跨轮次保持，否则内部状态冲突）。
    """
    logger.info("爬虫启动: %r | 抓取模式=%s", config, config.fetch_mode)

    browser = create_browser(config)
    ua_pool = create_ua_pool(config)
    rotator = create_rotator(config)
    nodriver = create_nodriver(config, ua_pool, rotator=rotator)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("收到停止信号，退出循环")
                break

            try:
                run_once(config, repo, browser, stop_event=stop_event, nodriver=nodriver)
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
        if nodriver is not None:
            nodriver.close()
        if browser is not None:
            browser.close()
