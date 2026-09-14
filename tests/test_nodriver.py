"""nodriver 反检测通道测试（mock，不启动真实浏览器、不依赖 Python 3.10+）。

运行：
    cd crawler && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.nodriver_fetcher import BLOCK_SIGNATURES, NodriverFetcher, is_blocked
from src.user_agents import UserAgentPool

BLOCKED_HTML = "<html><head><title>reuters.com</title></head><body>var dd={'rt':'i','cid':'x'}</body></html>"
GOOD_HTML = "<html><body><article>" + ("word " * 8000) + "</article></body></html>"


class TestIsBlocked(unittest.TestCase):
    """DataDome / 挑战页识别。"""

    def test_none_is_blocked(self):
        self.assertTrue(is_blocked(None))

    def test_datadome_signature(self):
        self.assertTrue(is_blocked(BLOCKED_HTML))

    def test_enable_javascript_page(self):
        self.assertTrue(is_blocked("<html>Please enable JavaScript to continue</html>"))

    def test_tiny_page_blocked(self):
        self.assertTrue(is_blocked("<html>tiny</html>"))

    def test_normal_article_not_blocked(self):
        self.assertFalse(is_blocked(GOOD_HTML))

    def test_signatures_are_lowercase(self):
        """签名须为小写（匹配时对 html 做 lower）。"""
        for sig in BLOCK_SIGNATURES:
            self.assertEqual(sig, sig.lower())


class TestNodriverFetcher(unittest.TestCase):
    def setUp(self):
        self.pool = UserAgentPool()
        self.fetcher = NodriverFetcher(
            ua_pool=self.pool,
            proxies=None,
            headless=True,
            wait_seconds=0,
            max_switch=3,
            request_interval=0,  # 测试无需冷却，避免拖慢
        )

    def test_browser_args_use_current_ua(self):
        profile = self.pool.next_profile()
        args = self.fetcher._browser_args(profile)
        self.assertIn(f'--user-agent={profile["user_agent"]}', args)
        self.assertIn("--no-sandbox", args)

    def test_browser_args_include_proxy(self):
        fetcher = NodriverFetcher(
            ua_pool=self.pool,
            proxies={"https": "socks5://1.2.3.4:1080"},
            request_interval=0,
        )
        args = fetcher._browser_args(self.pool.next_profile())
        self.assertIn("--proxy-server=socks5://1.2.3.4:1080", args)

    def test_switches_identity_after_blocked(self):
        """第一次被拦 → 自动换身份重试 → 成功。"""
        self.fetcher._fetch_once = mock.AsyncMock(side_effect=[BLOCKED_HTML, GOOD_HTML])
        html = self.fetcher.get_html("https://reuters.com/a")
        self.assertEqual(html, GOOD_HTML)
        self.assertEqual(self.fetcher._fetch_once.call_count, 2)
        # 两次使用的身份不同
        p1 = self.fetcher._fetch_once.call_args_list[0][0][1]
        p2 = self.fetcher._fetch_once.call_args_list[1][0][1]
        self.assertNotEqual(p1["name"], p2["name"])

    def test_returns_none_after_max_switch(self):
        """持续被拦 → 切换 max_switch 次后放弃。"""
        self.fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)
        self.assertIsNone(self.fetcher.get_html("https://reuters.com/a"))
        self.assertEqual(self.fetcher._fetch_once.call_count, 3)

    def test_first_success_no_retry(self):
        self.fetcher._fetch_once = mock.AsyncMock(return_value=GOOD_HTML)
        html = self.fetcher.get_html("https://reuters.com/a")
        self.assertEqual(html, GOOD_HTML)
        self.assertEqual(self.fetcher._fetch_once.call_count, 1)

    def test_exception_then_success(self):
        """浏览器异常 → 换身份再试 → 成功。"""
        self.fetcher._fetch_once = mock.AsyncMock(
            side_effect=[RuntimeError("browser crash"), GOOD_HTML]
        )
        html = self.fetcher.get_html("https://reuters.com/a")
        self.assertEqual(html, GOOD_HTML)

    def test_all_exceptions_returns_none(self):
        self.fetcher._fetch_once = mock.AsyncMock(side_effect=RuntimeError("crash"))
        self.assertIsNone(self.fetcher.get_html("https://reuters.com/a"))

    def test_empty_url(self):
        self.assertIsNone(self.fetcher.get_html(""))

    def test_available_false_when_import_fails(self):
        """nodriver 未安装/不兼容时，available 应为 False（通道跳过而非崩溃）。"""
        with mock.patch.dict(sys.modules, {"nodriver": None}):
            self.assertFalse(self.fetcher.available)


if __name__ == "__main__":
    unittest.main()
