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
        # 跨线程提交协程时的阻塞超时：浏览器启动 + 等待挑战页通常 < 60s，
        # 给足余量防止个别卡死请求把整轮挂住
        self._coroutine_timeout: float = 180.0
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
            # root 用户必须关沙箱，否则 Chromium 拒绝启动（报错 "Failed to connect to browser"）。
            # 注意：nodriver 接口参数是 `sandbox=False`（不是 no_sandbox=True，那是提示文案）。
            sandbox=False,
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

    def _run_coroutine(self, coro_factory):
        """执行协程工厂：优先复用 nodriver 的单例 loop，不可用/已在运行时妥善回退。

        ⚠️ 这里有两个必须避开的坑，都与「换身份重试」直接相关：

        1. **协程不可复用**：参数必须是「协程工厂」（每次调用返回**新的**协程），
           而不是协程对象。协程一旦被 await 过就不能再次 await，否则抛
           `RuntimeError: cannot reuse already awaited coroutine`。
           旧实现在 `loop.run_until_complete(coro)` 抛异常后复用同一个协程回退，
           真实错误被掩盖 —— 表现为换 3 次身份全部报同一个 coroutine 错误。

        2. **单例 loop 可能已经在运行**：nodriver 内部会把自己的 loop 跑在后台线程。
           第 1 次 `run_until_complete` 之后该 loop 进入 running 状态，此时再对它
           `run_until_complete` 会抛
           `RuntimeError: Cannot run the event loop while another loop is running`
           —— 表现为**第 1 篇之后所有 nodriver 请求瞬间全失败、换身份也无效**，
           日志上极像"IP 被封禁"，实则是基础设施错误，浏览器压根没启动过。

           解决：loop 正在运行 → 用 `asyncio.run_coroutine_threadsafe` 跨线程提交并
           阻塞取结果；loop 为空/已关闭（如 Python < 3.10 导致 nodriver 不可用）
           → 用 `asyncio.run` 新建 loop。
        """
        loop = None
        try:
            import nodriver as uc

            loop = uc.loop()
            if loop is not None and loop.is_closed():
                logger.debug("nodriver 单例 loop 已关闭，改用 asyncio.run")
                loop = None
        except Exception as exc:  # nodriver 未安装 / Python 版本不满足
            logger.debug("nodriver 不可用，回退 asyncio.run: %s", type(exc).__name__)
            loop = None

        if loop is None:
            return asyncio.run(coro_factory())

        if loop.is_running():
            # loop 已在别的线程跑着：跨线程提交，阻塞等待结果（带超时防挂死）
            future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
            return future.result(timeout=self._coroutine_timeout)

        return loop.run_until_complete(coro_factory())

    def get_html(self, url: str) -> str | None:
        """同步抓取；检测到拦截则自动换身份重试，全部失败返回 None。"""
        if not url:
            return None

        for attempt in range(1, self.max_switch + 1):
            profile = self.ua_pool.next_profile()
            self._last_profile_name = profile["name"]

            try:
                # 传工厂而非协程对象：重试时才能拿到全新协程
                html = self._run_coroutine(lambda: self._fetch_once(url, profile))
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
