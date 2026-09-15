"""nodriver 反检测通道测试（mock，不启动真实浏览器、不依赖 Python 3.10+）。

运行：
    cd crawler && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
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


class TestFailFast(unittest.TestCase):
    """连续失败熔断：IP 层封禁时换身份是无效功，应尽早止损。

    计数按「文章」计：同一篇内换 N 次身份只算 1 次失败。
    """

    def _make(self, threshold: int) -> NodriverFetcher:
        return NodriverFetcher(
            ua_pool=UserAgentPool(),
            headless=True,
            wait_seconds=0,
            max_switch=3,
            request_interval=0,
            fail_fast_threshold=threshold,
        )

    def test_trips_after_threshold_articles(self):
        """连续 2 篇全被拦 → 熔断，第 3 篇不再启动浏览器。"""
        fetcher = self._make(threshold=2)
        fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

        self.assertIsNone(fetcher.get_html("https://reuters.com/1"))
        self.assertFalse(fetcher.tripped)
        self.assertIsNone(fetcher.get_html("https://reuters.com/2"))
        self.assertTrue(fetcher.tripped)

        # 熔断后直接返回 None，不再启动浏览器（调用次数保持在 2 篇 × 3 次 = 6）
        self.assertEqual(fetcher._fetch_once.call_count, 6)
        self.assertIsNone(fetcher.get_html("https://reuters.com/3"))
        self.assertEqual(fetcher._fetch_once.call_count, 6)

    def test_intra_article_switch_counts_once(self):
        """一篇内换 3 次身份只算 1 次失败 → threshold=2 时第 1 篇不熔断。"""
        fetcher = self._make(threshold=2)
        fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

        fetcher.get_html("https://reuters.com/1")
        self.assertEqual(fetcher._fetch_once.call_count, 3)
        self.assertFalse(fetcher.tripped)

    def test_success_resets_counter(self):
        """失败→成功→失败：成功会清零计数，不触发熔断。"""
        fetcher = self._make(threshold=2)
        fetcher._fetch_once = mock.AsyncMock(
            side_effect=[
                BLOCKED_HTML, BLOCKED_HTML, BLOCKED_HTML,  # 篇1：换满身份仍被拦
                GOOD_HTML,                                  # 篇2：成功 → 清零
                BLOCKED_HTML, BLOCKED_HTML, BLOCKED_HTML,   # 篇3：重新开始计 1 次失败
            ]
        )

        self.assertIsNone(fetcher.get_html("https://reuters.com/1"))  # 3 次被拦
        self.assertIsNotNone(fetcher.get_html("https://reuters.com/2"))  # 成功
        self.assertIsNone(fetcher.get_html("https://reuters.com/3"))  # 1 次即失败（遇异常前先被拦）
        self.assertFalse(fetcher.tripped)

    def test_threshold_zero_never_trips(self):
        """threshold=0（关闭熔断）：每篇都正常重试。"""
        fetcher = self._make(threshold=0)
        fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

        for i in range(4):
            self.assertIsNone(fetcher.get_html(f"https://reuters.com/{i}"))
        self.assertFalse(fetcher.tripped)
        self.assertEqual(fetcher._fetch_once.call_count, 12)

    def test_reset_clears_trip(self):
        """reset() 后恢复探测（新一轮/换 IP 后可用）。"""
        fetcher = self._make(threshold=1)
        fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

        fetcher.get_html("https://reuters.com/1")
        self.assertTrue(fetcher.tripped)

        fetcher.reset()
        self.assertFalse(fetcher.tripped)
        fetcher.get_html("https://reuters.com/2")
        self.assertEqual(fetcher._fetch_once.call_count, 6)

    def test_exceptions_also_count_as_failure(self):
        """浏览器异常（非拦截页）同样计入熔断。"""
        fetcher = self._make(threshold=1)
        fetcher._fetch_once = mock.AsyncMock(side_effect=RuntimeError("crash"))

        self.assertIsNone(fetcher.get_html("https://reuters.com/1"))
        self.assertTrue(fetcher.tripped)


class TestDumpBlocked(unittest.TestCase):
    """拦截页落盘诊断（NODRIVER_DUMP_BLOCKED）。"""

    def _make(self, dump_dir: str) -> NodriverFetcher:
        return NodriverFetcher(
            ua_pool=UserAgentPool(),
            headless=True,
            wait_seconds=0,
            max_switch=3,
            request_interval=0,
            dump_dir=dump_dir,
        )

    def test_dumps_every_blocked_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = self._make(dump_dir=os.path.join(tmp, "blocked"))
            fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

            self.assertIsNone(fetcher.get_html("https://reuters.com/x"))

            files = os.listdir(os.path.join(tmp, "blocked"))
            # 3 次身份切换 → 3 份样本（便于对比不同身份的响应差异）
            self.assertEqual(len(files), 3)
            body = Path(os.path.join(tmp, "blocked", files[0])).read_text(encoding="utf-8")
            self.assertIn("https://reuters.com/x", body)  # 带来源 URL 注释
            self.assertIn("var dd=", body)  # DataDome 挑战页特征原样保留

    def test_filename_contains_identity_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = self._make(dump_dir=os.path.join(tmp, "blocked"))
            fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

            fetcher.get_html("https://reuters.com/x")
            names = os.listdir(os.path.join(tmp, "blocked"))
            self.assertTrue(all(n.endswith(".html") for n in names))
            # 身份名 + 字节数均在文件名中
            self.assertTrue(any(f"{len(BLOCKED_HTML)}B" in n for n in names))

    def test_no_dump_when_dir_empty(self):
        """未开启（dump_dir 为空）时不落盘、不创建目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = self._make(dump_dir="")
            fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

            self.assertIsNone(fetcher.get_html("https://reuters.com/x"))
            self.assertEqual(os.listdir(tmp), [])

    def test_dump_failure_does_not_break_fetch(self):
        """落盘失败（路径不可写）只忽略，不影响主流程。"""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-dir")
            Path(blocker).write_text("x", encoding="utf-8")  # 让 mkdir 失败

            fetcher = self._make(dump_dir=blocker)
            fetcher._fetch_once = mock.AsyncMock(return_value=BLOCKED_HTML)

            self.assertIsNone(fetcher.get_html("https://reuters.com/x"))

    def test_successful_fetch_not_dumped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = self._make(dump_dir=os.path.join(tmp, "blocked"))
            fetcher._fetch_once = mock.AsyncMock(return_value=GOOD_HTML)

            self.assertIsNotNone(fetcher.get_html("https://reuters.com/x"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "blocked")))


if __name__ == "__main__":
    unittest.main()
