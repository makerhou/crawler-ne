"""Playwright headless 浏览器抓取（用于绕过 JS 渲染 / 反爬）。

性能要点：
- **复用单例 browser + context**，避免每篇文章都启停浏览器（启动开销数秒，会拖垮 5 分钟周期）；
- 每次抓取只 new_page / close_page；
- 失败只记录日志并返回 None，由上层决定是否回退。

首次部署需额外安装浏览器内核：
    pip install playwright
    playwright install chromium
    # 若缺系统依赖（Debian/Ubuntu）：
    # playwright install-deps chromium
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("reuters-crawler")

# 降低被识别为自动化的概率 + 容器环境必需参数
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
]


class BrowserFetcher:
    """headless Chromium 抓取器（懒启动、可复用）。"""

    def __init__(
        self,
        user_agent: str = "",
        timeout_ms: int = 30_000,
        headless: bool = True,
        wait_selector: str = "",
        wait_ms: int = 1_000,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.wait_selector = wait_selector
        self.wait_ms = wait_ms
        # Playwright 只接受单一 proxy 配置，优先取 https
        self.proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or None

        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._import_checked = False
        self._available = False
        self._stealth_ok = False

    # ---------- 可用性 ----------
    def _check_available(self) -> bool:
        if not self._import_checked:
            self._import_checked = True
            try:
                import playwright.sync_api  # noqa: F401

                self._available = True
            except ImportError:
                logger.error(
                    "未安装 playwright，请执行: pip install playwright && playwright install chromium"
                )
                self._available = False
        return self._available

    @property
    def available(self) -> bool:
        return self._check_available()

    # ---------- 生命周期 ----------
    def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self.headless, "args": _LAUNCH_ARGS}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self._browser = self._pw.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {}
        if self.user_agent:
            context_kwargs["user_agent"] = self.user_agent
        self._context = self._browser.new_context(**context_kwargs)

        self._apply_stealth()
        logger.info("headless 浏览器已启动（headless=%s, stealth=%s）", self.headless, self._stealth_ok)
        return self._context

    def _apply_stealth(self) -> None:
        """应用反自动化检测（隐藏 navigator.webdriver 等指纹特征）。

        Reuters 使用 Akamai Bot Manager，headless Chromium 会被识别并返回挑战页，
        因此必须启用；未安装 playwright-stealth 时降级跳过（仅告警）。
        """
        try:
            from playwright_stealth import Stealth
        except ImportError:
            logger.warning(
                "未安装 playwright-stealth，无法隐藏自动化特征（可能被反爬拦截）。"
                "建议: pip install playwright-stealth"
            )
            self._stealth_ok = False
            return

        try:
            Stealth().apply_stealth_sync(self._context)
            self._stealth_ok = True
        except Exception as exc:
            logger.warning("应用 stealth 失败（继续运行）: %s", exc)
            self._stealth_ok = False

    def get_html(self, url: str) -> str | None:
        """用浏览器加载页面并返回渲染后的 HTML；失败返回 None。"""
        if not self.available or not url:
            return None

        try:
            context = self._ensure_context()
            page = context.new_page()
            try:
                page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")

                if self.wait_selector:
                    try:
                        page.wait_for_selector(self.wait_selector, timeout=self.wait_ms or 5_000)
                    except Exception as exc:
                        logger.debug("等待选择器超时（继续）: %s | %s", exc, url[:80])
                elif self.wait_ms:
                    page.wait_for_timeout(self.wait_ms)

                return page.content()
            finally:
                page.close()
        except Exception as exc:
            logger.warning("浏览器抓取失败: %s | url=%s", exc, url[:100])
            # 上下文可能已失效，下次重新创建
            self._safe_close()
            return None

    def _safe_close(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception as exc:
                logger.debug("关闭浏览器资源失败（忽略）: %s", exc)
        self._context = None
        self._browser = None
        self._pw = None

    def close(self) -> None:
        """释放浏览器资源（进程退出前调用）。"""
        logger.info("关闭 headless 浏览器")
        self._safe_close()
