"""nodriver 反检测抓取通道（对抗 DataDome / Cloudflare）。

为什么需要它：
- CloudFront + DataDome 的挑战要求**执行 JS**，纯 HTTP（requests/curl_cffi）原理上过不去；
- Playwright 启动的浏览器会注入 `navigator.webdriver` 等自动化痕迹，被 DataDome 识别；
- nodriver 走纯 CDP，**不加载 webdriver、不注入自动化属性、每次全新 profile**，实测可突破。

两个关键能力：
1. **UA/身份自动切换**：每次请求（含重试）从身份池取下一个身份；
2. **代理池轮换（突破 DataDome 关键）**：PROXY_POOL 配置多个代理，命中 DataDome 拦截页后
   关闭浏览器、换下一个出口 IP 重启再试（DataDome 根因是出口 IP 被标记，换 UA 无效）；
3. **命中 DataDome 强特征即早退**：无更多代理时不空跑身份重试，直接放弃本篇走降级。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path

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


def is_datadome(html: str | None) -> bool:
    """判断是否为 DataDome 拦截页（强特征，命中即说明是 IP 层封禁，换 UA 无效）。

    与 is_blocked 区分：is_blocked 还会因「内容过小」误判正常短页，
    而 DataDome 特征（var dd= / datadome / captcha-delivery.com）是专属签名，
    用于决策「放弃换身份、改换代理 IP」。
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
        # 代理池：多个代理 URL 轮流使用，被 DataDome 拦截时换下一个 IP 重试
        proxy_pool: list[str] | None = None,
        # 实测 DataDome 能识别 headless 模式（headless=True 必被拦），故默认有头；
        # 服务器无显示器时用 xvfb-run 启动即可。
        headless: bool = False,
        wait_seconds: float = 8.0,
        max_switch: int = 3,
        request_interval: float = 5.0,
        dump_dir: str = "",
        fail_fast_threshold: int = 0,
    ) -> None:
        self.ua_pool = ua_pool or UserAgentPool()
        self.proxies = proxies
        self.proxy_pool = proxy_pool or []
        self.headless = headless
        self.wait_seconds = wait_seconds
        self.max_switch = max(1, max_switch)
        # 每次抓取后的冷却：实测密集请求会让 IP 被 DataDome 快速拉黑
        self.request_interval = max(0.0, request_interval)
        # 被判定拦截时的 HTML 落盘目录（空串 = 不落盘）
        self.dump_dir = dump_dir
        # 连续多少篇全被拦后熔断本轮（0 = 不熔断）
        self.fail_fast_threshold = max(0, fail_fast_threshold)
        self._consecutive_failures = 0
        self._tripped = False
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

    def _proxy_for(self, attempt: int) -> str | None:
        """按重试次数从代理池轮转取代理；无池则返回 None（直连）。"""
        if not self.proxy_pool:
            return None
        return self.proxy_pool[(attempt - 1) % len(self.proxy_pool)]

    def _has_more_proxies(self, attempt: int) -> bool:
        """是否还有下一个不同的代理可换（命中 DataDome 时决定是否早退）。"""
        return len(self.proxy_pool) > 1 and attempt < len(self.proxy_pool)

    def _browser_args(self, profile: dict[str, str], proxy: str | None) -> list[str]:
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
        if proxy:
            args.append(f"--proxy-server={proxy}")
        return args

    async def _fetch_once(self, url: str, profile: dict[str, str], proxy: str | None) -> str:
        """用指定身份 + 指定代理启动浏览器抓一次。"""
        import nodriver as uc

        browser = await uc.start(
            headless=self.headless,
            browser_args=self._browser_args(profile, proxy),
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
        """执行协程工厂：优先用 nodriver 的单例 loop，不可用时回退 asyncio.run。

        ⚠️ 参数是「协程工厂」（每次调用返回**新的**协程），而不是协程对象：
        协程一旦被 await 过就不能再次 await，否则抛
        `RuntimeError: cannot reuse already awaited coroutine`。
        旧实现在 `loop.run_until_complete(coro)` 抛异常后复用同一个协程回退，
        导致真实错误被掩盖 —— 表现为换 3 次身份全部报同一个 coroutine 错误。

        现在只在「nodriver 不可用」时回退（此时协程尚未执行，回退安全），
        执行期间的异常直接向上抛，保留真实失败原因。

        不用 `asyncio.run` 的原因：反复调用会不断新建并关闭 loop，产生
        "Loop ... is closed" 告警；nodriver 自带单例 loop，更稳定。

        回退场景：Python < 3.10 时 nodriver 无法导入（会抛 TypeError/SyntaxError），
        此时退化为标准 asyncio，保证其余逻辑（含测试）不受影响。
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
        return loop.run_until_complete(coro_factory())

    def get_html(self, url: str) -> str | None:
        """同步抓取；命中 DataDome 则换下一个代理重试，全部失败返回 None。

        重试策略：每次重试换「代理（取自 PROXY_POOL）+ 身份」；命中 DataDome 强特征时
        若池里还有更多代理就切 IP 重试，否则放弃本篇（同 IP 换身份是无效功，只会拖长单轮）。
        熔断（`fail_fast_threshold > 0`）：连续 N 篇文章「换满代理/身份仍被拦」时，
        判定为整体出口 IP 被封，本轮剩余文章直接跳过该通道。
        计数按「文章」计，同一篇内的多次切换只算 1 次失败。
        """
        if not url:
            return None

        if self._tripped:
            logger.debug("nodriver 已熔断，跳过该通道 | url=%s", url[:80])
            return None

        for attempt in range(1, self.max_switch + 1):
            profile = self.ua_pool.next_profile()
            proxy = self._proxy_for(attempt)
            self._last_profile_name = profile["name"]

            try:
                # 传工厂而非协程对象：重试时才能拿到全新协程（含换代理/身份）
                html = self._run_coroutine(lambda: self._fetch_once(url, profile, proxy))
            except Exception as exc:
                logger.warning(
                    "nodriver 抓取异常（%d/%d，身份=%s，代理=%s）: %s",
                    attempt,
                    self.max_switch,
                    profile["name"],
                    proxy,
                    exc,
                )
                continue
            finally:
                # 冷却：避免密集请求导致 IP 被 DataDome 拉黑
                if self.request_interval:
                    time.sleep(self.request_interval)

            if not is_blocked(html):
                logger.info(
                    "nodriver 抓取成功（第 %d 次，身份=%s，代理=%s，%d 字节）",
                    attempt,
                    profile["name"],
                    proxy,
                    len(html),
                )
                self._consecutive_failures = 0
                return html

            self._dump_blocked(html, profile, url)

            # 命中 DataDome 强特征 → 根因是出口 IP 被标记，换 UA/身份无效；
            # 直接换下一个代理（若池里有更多 IP），否则放弃本篇（避免同 IP 空跑）。
            if is_datadome(html):
                if self._has_more_proxies(attempt):
                    logger.warning(
                        "DataDome 拦截（第 %d/%d 次，身份=%s，代理=%s）→ 切换代理重试",
                        attempt,
                        self.max_switch,
                        profile["name"],
                        proxy,
                    )
                else:
                    logger.warning(
                        "DataDome 拦截（第 %d/%d 次，身份=%s，代理=%s）→ 无更多代理，"
                        "放弃本篇（换身份无效，需配置 PROXY_POOL 住宅代理）",
                        attempt,
                        self.max_switch,
                        profile["name"],
                        proxy,
                    )
                    break
                continue

            logger.warning(
                "第 %d/%d 次被反爬拦截（身份=%s，代理=%s，%d 字节）→ 切换身份重试",
                attempt,
                self.max_switch,
                profile["name"],
                proxy,
                len(html),
            )

        logger.error("nodriver 已切换 %d 次仍被拦截: %s", self.max_switch, url[:100])
        self._register_failure()
        return None

    # ---------- 熔断 ----------
    def _register_failure(self) -> None:
        """记录一次「整篇失败」并按需熔断。"""
        self._consecutive_failures += 1
        if not self.fail_fast_threshold:
            return
        if self._consecutive_failures < self.fail_fast_threshold:
            return

        self._tripped = True
        logger.warning(
            "nodriver 连续 %d 篇被拦，判定为 IP 层封禁（换身份无效）→ 本轮剩余文章跳过该通道。"
            "如需恢复请换出口 IP 或配置代理",
            self._consecutive_failures,
        )

    def reset(self) -> None:
        """解除熔断并清零计数（新一轮开始时调用）。"""
        self._consecutive_failures = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        """是否已熔断（供上层观测）。"""
        return self._tripped

    # ---------- 诊断 ----------
    def _dump_blocked(self, html: str, profile: dict[str, str], url: str) -> None:
        """把被判定为拦截页的 HTML 落盘，便于定位挑战页类型。

        文件名含时间/身份/字节数，便于对比不同身份的响应差异。
        落盘失败（目录不可写等）只记 debug，不影响主流程。
        """
        if not self.dump_dir:
            return

        try:
            directory = Path(self.dump_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}_{profile['name']}_{len(html)}B_{random.randint(1000, 9999)}.html"
            target = directory / name
            target.write_text(
                f"<!-- url={url} -->\n{html}",
                encoding="utf-8",
                errors="replace",
            )
            logger.info("已保存拦截页样本: %s", target)
        except Exception as exc:  # 诊断功能不得影响抓取主流程
            logger.debug("保存拦截页样本失败（忽略）: %s", exc)
