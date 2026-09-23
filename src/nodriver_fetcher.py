"""nodriver 反检测抓取通道（对抗 DataDome / Cloudflare）。

为什么需要它：
- CloudFront + DataDome 的挑战要求**执行 JS**，纯 HTTP（requests/curl_cffi）原理上过不去；
- Playwright 启动的浏览器会注入 `navigator.webdriver` 等自动化痕迹，被 DataDome 识别；
- nodriver 走纯 CDP，**不加载 webdriver、不注入自动化属性、每次全新 profile**，实测可突破。

三个关键能力：
1. **UA/身份自动切换**：每次请求（含重试）从身份池取下一个身份；
2. **被检测后自动换身份重试**：识别拦截页后，关闭浏览器、换新身份重启再试；
3. **命中 DataDome 强特征时换出口 IP**（需配置节点面板）：DataDome 封禁发生在 IP 层，
   换身份无效 —— 此时调 NodeRotator 切换 VPN 节点，用新出口 IP 重新尝试。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

from .user_agents import UserAgentPool

if TYPE_CHECKING:  # 仅类型注解需要，避免运行时多余导入
    from .node_rotator import NodeRotator

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


def is_datadome(html: str | None) -> bool:
    """判断是否为 DataDome 拦截页（强特征）。

    与 is_blocked 的区别：is_blocked 还会因「内容过小」误判正常短页，
    而这里只认 DataDome 专属签名 —— 用于决策「是否换出口 IP 重试」：
    DataDome 封禁发生在 IP 层，换 UA/身份无效，只有换 IP 才有意义。
    """
    if not html:
        return False
    lowered = html.lower()
    return (
        "var dd=" in lowered
        or "datadome" in lowered
        or "captcha-delivery.com" in lowered
    )


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
        # 命中 DataDome 强特征时用于切换出口 IP（None = 不启用换 IP）
        rotator: "NodeRotator | None" = None,
        # 单篇最多换几个 IP（0 = 只换身份，不换 IP）
        max_ip_switch: int = 3,
    ) -> None:
        self.ua_pool = ua_pool or UserAgentPool()
        self.proxies = proxies
        self.headless = headless
        self.wait_seconds = wait_seconds
        self.max_switch = max(1, max_switch)
        self.rotator = rotator
        self.max_ip_switch = max(0, max_ip_switch)
        # 每次抓取后的冷却：实测密集请求会让 IP 被 DataDome 快速拉黑
        self.request_interval = max(0.0, request_interval)
        self._last_profile_name: str = ""

        # 持久化 event loop：nodriver 内部维护全局 loop 状态，
        # 反复 asyncio.run() 创建/销毁 loop 会导致第 2 次起报
        # "Cannot run the event loop while another loop is running"。
        # 解决方案：所有 nodriver 调用共用同一个 loop（跑在专用守护线程里）。
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="nodriver-loop"
        )
        self._loop_thread.start()

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
        """用指定身份启动浏览器抓一次。

        代理降级：若配置了代理但浏览器连接失败（代理没跑），自动去掉代理重试一次。
        """
        import nodriver as uc

        chrome_path = self._find_chrome()
        browser_args = self._browser_args(profile)

        start_kwargs: dict = dict(
            headless=self.headless,
            # root 用户必须关沙箱，否则 Chromium 拒绝启动（报错 "Failed to connect to browser"）。
            # nodriver 0.50.x 参数名是 sandbox（默认 True）：
            #   sandbox=False → 内部添加 --no-sandbox → root 下才能启动
            sandbox=False,
            browser_args=browser_args,
        )
        if chrome_path:
            start_kwargs["browser_executable_path"] = chrome_path

        try:
            browser = await uc.start(**start_kwargs)
        except Exception as exc:
            # 代理不通是 "Failed to connect to browser" 的常见根因：
            # Chrome 带 --proxy-server 启动后，若代理没跑，CDP 连接会超时。
            # 降级：去掉代理参数重试一次（直连）。
            has_proxy = any(a.startswith("--proxy-server=") for a in browser_args)
            if not has_proxy:
                raise
            logger.warning(
                "浏览器带代理启动失败（%s），降级为直连重试", exc
            )
            direct_args = [a for a in browser_args if not a.startswith("--proxy-server=")]
            start_kwargs["browser_args"] = direct_args
            browser = await uc.start(**start_kwargs)

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
        """在持久化 event loop 中执行协程，彻底避开 loop 冲突。

        ⚠️ 历史教训：

        1. 复用 ``uc.loop()`` 单例 loop：第 1 篇成功后 loop 被 nodriver 后台线程
           绑定，第 2 篇起报 ``Cannot run the event loop while another loop is running``。
        2. 直接 ``asyncio.run()``：每次创建/销毁 loop，nodriver 内部全局状态
           指向已关闭的 loop，第 2 次起同样报错。
        3. ``ThreadPoolExecutor`` + ``asyncio.run()``：新线程有新 loop，但 nodriver
           内部仍可能引用旧 loop 的全局状态。

        当前方案（v3）：
        - __init__ 时创建**持久化 event loop** + 专用守护线程；
        - 所有 nodriver 调用通过 ``run_coroutine_threadsafe`` 提交到该 loop；
        - loop 永不关闭（直到进程退出），nodriver 内部状态始终一致。

        参数必须是「协程工厂」（每次调用返回新协程），而非协程对象。
        """
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result()

    def close(self) -> None:
        """关闭持久化 event loop（进程退出前调用）。"""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    def _can_switch_ip(self, ip_switches: int) -> bool:
        """是否还能切换出口 IP（需已配置节点面板且未达次数上限）。"""
        return (
            self.rotator is not None
            and self.rotator.enabled
            and ip_switches < self.max_ip_switch
        )

    def get_html(self, url: str) -> str | None:
        """同步抓取；命中 DataDome 强特征或浏览器连续异常则换出口 IP 重试，全部失败返回 None。

        重试策略（已确认）：
        - 命中 **DataDome 强特征**（var dd= / datadome / captcha-delivery）→ 换出口 IP
          重试：DataDome 封禁发生在 IP 层，换身份无效（已实测），最多换 max_ip_switch 次；
        - **浏览器连续异常**（启动失败等）→ 也换出口 IP：代理不通/IP 被封是常见根因；
        - 其他拦截（挑战页、内容过小）→ 仅换身份重试，最多 max_switch 次；
        - 每换到一个新 IP，重新走一轮「换身份重试」。
        全部失败返回 None，由上层决定是否降级入库。
        """
        if not url:
            return None

        ip_switches = 0
        while True:
            # 在当前出口 IP 下换身份重试
            need_ip_switch = False
            consecutive_errors = 0
            switch_reason = ""
            for attempt in range(1, self.max_switch + 1):
                profile = self.ua_pool.next_profile()
                self._last_profile_name = profile["name"]

                try:
                    # 传工厂而非协程对象：重试时才能拿到全新协程
                    html = self._run_coroutine(lambda: self._fetch_once(url, profile))
                    consecutive_errors = 0  # 成功拿到响应，重置连续异常计数
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "nodriver 抓取异常（%d/%d，身份=%s）: %s",
                        attempt,
                        self.max_switch,
                        profile["name"],
                        exc,
                    )
                    # 所有尝试都是异常 → 很可能是代理/IP 层问题，触发换 IP
                    if (
                        consecutive_errors >= self.max_switch
                        and self._can_switch_ip(ip_switches)
                    ):
                        need_ip_switch = True
                        switch_reason = (
                            f"浏览器连续 {consecutive_errors} 次异常（可能代理/IP 不通）"
                        )
                        break
                    continue
                finally:
                    # 冷却：避免密集请求导致 IP 被 DataDome 拉黑
                    if self.request_interval:
                        time.sleep(self.request_interval)

                if not is_blocked(html):
                    logger.info(
                        "nodriver 抓取成功（第 %d 次，身份=%s，%d 字节，已换 IP %d 次）",
                        attempt,
                        profile["name"],
                        len(html),
                        ip_switches,
                    )
                    return html

                # 仅「DataDome 强特征 + 确实能换 IP」时才中断换身份、改走换 IP；
                # 未配置面板/次数用尽时继续换身份重试（保留原有兜底行为）
                if is_datadome(html) and self._can_switch_ip(ip_switches):
                    need_ip_switch = True
                    switch_reason = "命中 DataDome"
                    break

                logger.warning(
                    "第 %d/%d 次被反爬拦截（身份=%s，%d 字节）→ 切换身份重试",
                    attempt,
                    self.max_switch,
                    profile["name"],
                    len(html),
                )

            if not need_ip_switch:
                break

            ip_switches += 1
            logger.warning(
                "%s → 第 %d/%d 次切换出口 IP 重试",
                switch_reason,
                ip_switches,
                self.max_ip_switch,
            )
            if not self.rotator.switch():
                logger.warning("切换出口 IP 失败，放弃本篇")
                break

        logger.error(
            "nodriver 重试完毕仍被拦截（换 IP %d 次 × 每轮换身份 %d 次）: %s",
            ip_switches,
            self.max_switch,
            url[:100],
        )
        return None
