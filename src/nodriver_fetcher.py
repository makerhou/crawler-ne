"""nodriver 反检测抓取通道（对抗 DataDome / Cloudflare）。

为什么需要它：
- CloudFront + DataDome 的挑战要求**执行 JS**，纯 HTTP（requests/curl_cffi）原理上过不去；
- Playwright 启动的浏览器会注入 `navigator.webdriver` 等自动化痕迹，被 DataDome 识别；
- nodriver 走纯 CDP，**不加载 webdriver、不注入自动化属性、每次全新 profile**，实测可突破。

两个关键能力：
1. **UA/身份自动切换**：每次请求（含重试）从身份池取下一个身份；
2. **被检测后自动换身份重试**：识别 DataDome 拦截页后，关闭浏览器、换新身份重启再试。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .user_agents import UserAgentPool

logger = logging.getLogger("reuters-crawler")

# DataDome / CloudFront 等拦截页特征（小写匹配）
BLOCK_SIGNATURES = (
    "var dd={'rt'",  # DataDome 标志
    "datadome",
    "please enable javascript",
    "enable javascript and cookies to continue",
    "checking your browser",
    "access denied",
    "attention required",
)

# 正常新闻页远大于此；小于该值基本可判定为挑战页/空页
MIN_VALID_BYTES = 20_000


def is_blocked(html: str | None, min_bytes: int = MIN_VALID_BYTES) -> bool:
    """判断响应是否为反爬拦截页。

    判定逻辑：命中拦截签名 → 拦截；内容过小（likely 挑战页）→ 拦截。
    """
    if not html:
        return True

    lowered = html.lower()
    if any(sig in lowered for sig in BLOCK_SIGNATURES):
        return True

    if len(html) < min_bytes:
        logger.debug("响应过小（%d 字节），判定为拦截页", len(html))
        return True

    return False


class NodriverFetcher:
    """反检测浏览器抓取器（同步入口，内部 asyncio）。"""

    def __init__(
        self,
        ua_pool: UserAgentPool | None = None,
        proxies: dict[str, str] | None = None,
        # 实测 DataDome 能识别 headless 模式（headless=True 必被拦），故默认有头；
        # 服务器无显示器时用 xvfb-run 启动即可。
        headless: bool = False,
        wait_seconds: float = 8.0,
        max_switch: int = 3,
        request_interval: float = 5.0,
    ) -> None:
        self.ua_pool = ua_pool or UserAgentPool()
        self.proxies = proxies
        self.headless = headless
        self.wait_seconds = wait_seconds
        self.max_switch = max(1, max_switch)
        # 每次抓取后的冷却：实测密集请求会让 IP 被 DataDome 快速拉黑
        self.request_interval = max(0.0, request_interval)
        self._last_profile_name: str = ""

    @property
    def available(self) -> bool:
        """nodriver 是否可用（需 Python 3.10+ 且系统装有 Chrome）。"""
        try:
            import nodriver  # noqa: F401
        except Exception as exc:
            logger.warning(
                "nodriver 不可用（%s）：需 Python 3.10+，该通道将跳过", type(exc).__name__
            )
            return False

        chrome = self._find_chrome()
        if not chrome:
            logger.warning(
                "未找到 Chrome 浏览器，nodriver 通道将跳过。"
                "Debian/Ubuntu 安装：sudo apt install -y ./google-chrome-stable_current_amd64.deb"
            )
            return False

        logger.debug("nodriver 可用，浏览器: %s", chrome)
        return True

    @staticmethod
    def _find_chrome() -> str | None:
        """查找系统 Chrome/Chromium 可执行文件。"""
        import shutil

        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def _proxy_server(self) -> str | None:
        if not self.proxies:
            return None
        return self.proxies.get("https") or self.proxies.get("http")

    def _browser_args(self, profile: dict[str, str]) -> list[str]:
        args = [
            f'--user-agent={profile["user_agent"]}',
            "--no-sandbox",
            # /dev/shm 在小内存机器上通常只有 64MB，不禁用会导致 Chrome 崩溃
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            # ---- 省内存（针对 1~2GB 小内存 VPS）----
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--disable-default-apps",
            "--no-first-run",
            "--mute-audio",
            f'--lang={profile.get("accept_language", "en-US").split(",")[0]}',
        ]
        proxy = self._proxy_server()
        if proxy:
            args.append(f"--proxy-server={proxy}")
        return args

    async def _fetch_once(self, url: str, profile: dict[str, str]) -> str:
        """用指定身份启动浏览器抓一次。"""
        import nodriver as uc

        browser = await uc.start(
            headless=self.headless,
            browser_args=self._browser_args(profile),
        )
        try:
            page = await browser.get(url)
            # 给挑战页 JS 执行与跳转的时间
            await page.sleep(self.wait_seconds)
            return await page.get_content()
        finally:
            try:
                browser.stop()
            except Exception as exc:  # 关闭失败不影响已获取的 HTML
                logger.debug("关闭浏览器失败（忽略）: %s", exc)

    def _run_coroutine(self, coro):
        """执行协程：优先用 nodriver 的单例 loop，不可用时回退 asyncio.run。

        不用 `asyncio.run` 的原因：反复调用会不断新建并关闭 loop，产生
        "Loop ... is closed" 告警；nodriver 自带单例 loop，更稳定。

        回退场景：Python < 3.10 时 nodriver 无法导入（会抛 TypeError/SyntaxError），
        此时退化为标准 asyncio，保证其余逻辑（含测试）不受影响。
        """
        try:
            import nodriver as uc

            loop = uc.loop()
            return loop.run_until_complete(coro)
        except Exception as exc:  # nodriver 不可用（低版本 Python 等）
            logger.debug("nodriver loop 不可用，回退 asyncio.run: %s", type(exc).__name__)
            return asyncio.run(coro)

    def get_html(self, url: str) -> str | None:
        """同步抓取；检测到拦截则自动换身份重试，全部失败返回 None。"""
        if not url:
            return None

        for attempt in range(1, self.max_switch + 1):
            profile = self.ua_pool.next_profile()
            self._last_profile_name = profile["name"]

            try:
                html = self._run_coroutine(self._fetch_once(url, profile))
            except Exception as exc:
                logger.warning(
                    "nodriver 抓取异常（%d/%d，身份=%s）: %s",
                    attempt,
                    self.max_switch,
                    profile["name"],
                    exc,
                )
                continue
            finally:
                # 冷却：避免密集请求导致 IP 被 DataDome 拉黑
                if self.request_interval:
                    time.sleep(self.request_interval)

            if not is_blocked(html):
                logger.info(
                    "nodriver 抓取成功（第 %d 次，身份=%s，%d 字节）",
                    attempt,
                    profile["name"],
                    len(html),
                )
                return html

            logger.warning(
                "第 %d/%d 次被反爬拦截（身份=%s，%d 字节）→ 切换身份重试",
                attempt,
                self.max_switch,
                profile["name"],
                len(html),
            )

        logger.error("nodriver 已切换 %d 次身份仍被拦截: %s", self.max_switch, url[:100])
        return None
