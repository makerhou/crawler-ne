"""浏览器身份池（UA + 配套请求头 + TLS 指纹标识）。

反爬要点：
1. 单一 UA 被封即全线失效 → 多身份轮换；
2. 只换 UA 但头不一致反而更可疑 → 每个身份配套完整浏览器头
   （Accept / Accept-Language / sec-ch-ua / sec-fetch-*）；
3. **UA 与 TLS 指纹必须一致** → 每个身份带 `impersonate`，供 curl_cffi 模拟对应
   浏览器的 TLS/JA3 指纹（这是绕过 Cloudflare 的关键，requests 无法做到）。
"""

from __future__ import annotations

import itertools
import random

# 每个 profile 模拟一款真实浏览器：UA、配套头、curl_cffi 的 TLS 指纹标识三者一致
BROWSER_PROFILES: list[dict[str, str]] = [
    {
        "name": "chrome124-win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome124",
    },
    {
        "name": "chrome131-mac",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"macOS"',
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome131",
    },
    {
        "name": "edge124-win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
        ),
        "sec_ch_ua": '"Microsoft Edge";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome124",
    },
    {
        "name": "firefox133-win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
        ),
        "sec_ch_ua": "",  # Firefox 不发送 sec-ch-ua
        "sec_ch_ua_platform": "",
        "accept_language": "en-US,en;q=0.5",
        "impersonate": "firefox133",
    },
    {
        "name": "safari17-mac",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        "sec_ch_ua": "",  # Safari 不发送 sec-ch-ua
        "sec_ch_ua_platform": "",
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "safari17_0",
    },
]

_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
_RSS_ACCEPT = "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7"


class UserAgentPool:
    """浏览器身份池，支持轮询与随机两种轮换策略。"""

    def __init__(self, strategy: str = "round_robin", profiles: list[dict[str, str]] | None = None):
        self.profiles = profiles or BROWSER_PROFILES
        self.strategy = strategy
        self._cycle = itertools.cycle(self.profiles)

    def next_profile(self) -> dict[str, str]:
        if self.strategy == "random":
            return random.choice(self.profiles)
        return next(self._cycle)

    def build_headers(self, kind: str = "html", profile: dict[str, str] | None = None) -> dict[str, str]:
        """构造完整浏览器请求头。

        :param kind: "html"（抓正文）或 "rss"（抓 RSS）
        """
        profile = profile or self.next_profile()
        accept = _RSS_ACCEPT if kind == "rss" else _HTML_ACCEPT

        headers = {
            "User-Agent": profile["user_agent"],
            "Accept": accept,
            "Accept-Language": profile.get("accept_language", "en-US,en;q=0.9"),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }

        # 导航类请求的 sec-fetch 头（与真实浏览器一致）
        if kind == "html":
            headers.update(
                {
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                }
            )

        sec_ch_ua = profile.get("sec_ch_ua", "")
        if sec_ch_ua:
            headers["sec-ch-ua"] = sec_ch_ua
            headers["sec-ch-ua-mobile"] = "?0"
            platform = profile.get("sec_ch_ua_platform", "")
            if platform:
                headers["sec-ch-ua-platform"] = platform

        return headers

    def __len__(self) -> int:
        return len(self.profiles)
