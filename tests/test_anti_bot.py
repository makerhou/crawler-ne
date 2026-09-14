"""反爬增强测试：UA 身份池 + curl_cffi 通道（mock，不依赖网络）。

运行：
    cd crawler && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.article_parser import fetch_html_with_curl_cffi
from src.user_agents import BROWSER_PROFILES, UserAgentPool


class TestUserAgentPool(unittest.TestCase):
    """UA 身份池轮换与请求头一致性。"""

    def test_round_robin_cycles(self):
        pool = UserAgentPool(strategy="round_robin")
        names = [pool.next_profile()["name"] for _ in range(len(BROWSER_PROFILES) + 1)]
        # 轮完一圈后回到第一个
        self.assertEqual(names[0], names[-1])
        # 一圈之内互不重复
        self.assertEqual(len(set(names[:-1])), len(BROWSER_PROFILES))

    def test_random_strategy_stays_in_pool(self):
        pool = UserAgentPool(strategy="random")
        valid = {p["name"] for p in BROWSER_PROFILES}
        names = {pool.next_profile()["name"] for _ in range(30)}
        self.assertTrue(names <= valid)

    def test_html_headers_have_sec_fetch(self):
        headers = UserAgentPool().build_headers("html")
        self.assertIn("User-Agent", headers)
        self.assertEqual(headers["Sec-Fetch-Mode"], "navigate")
        self.assertIn("sec-ch-ua", headers)  # 默认第一个是 Chrome 系

    def test_rss_headers_no_sec_fetch(self):
        headers = UserAgentPool().build_headers("rss")
        self.assertNotIn("Sec-Fetch-Mode", headers)
        self.assertIn("application/rss+xml", headers["Accept"])

    def test_every_profile_has_impersonate(self):
        """每个身份都要有 TLS 指纹标识，保证 UA 与指纹一致。"""
        for profile in BROWSER_PROFILES:
            self.assertTrue(profile.get("impersonate"), f"{profile['name']} 缺少 impersonate")

    def test_firefox_headers_without_sec_ch_ua(self):
        """Firefox 不发送 sec-ch-ua，头必须与其一致，否则更可疑。"""
        pool = UserAgentPool()
        profile = next(p for p in pool.profiles if "firefox" in p["name"])
        headers = pool.build_headers("html", profile)
        self.assertNotIn("sec-ch-ua", headers)
        self.assertIn("Firefox", headers["User-Agent"])

    def test_ua_matches_impersonate_family(self):
        """UA 写的浏览器应与 impersonate 指纹同族（Chrome/Edge→chrome，Firefox→firefox）。"""
        for profile in BROWSER_PROFILES:
            ua = profile["user_agent"]
            imp = profile["impersonate"]
            if "Firefox" in ua:
                self.assertTrue(imp.startswith("firefox"), f"{profile['name']} 指纹不匹配")
            elif "Safari" in ua and "Chrome" not in ua:
                self.assertTrue(imp.startswith("safari"), f"{profile['name']} 指纹不匹配")
            else:
                self.assertTrue(imp.startswith("chrome"), f"{profile['name']} 指纹不匹配")


class TestCurlCffiChannel(unittest.TestCase):
    """curl_cffi 抓取通道（mock libcurl）。"""

    def _patch_curl(self, text="<html>ok</html>"):
        fake_response = mock.MagicMock()
        fake_response.text = text
        fake_response.raise_for_status = mock.MagicMock()
        fake_requests = mock.MagicMock()
        fake_requests.get.return_value = fake_response
        return mock.patch.dict(
            sys.modules, {"curl_cffi": mock.MagicMock(requests=fake_requests)}
        ), fake_requests

    def test_success(self):
        patcher, fake_requests = self._patch_curl("<html>hello</html>")
        with patcher:
            html = fetch_html_with_curl_cffi("https://x.com", impersonate="chrome124")
        self.assertEqual(html, "<html>hello</html>")
        _, kwargs = fake_requests.get.call_args
        self.assertEqual(kwargs["impersonate"], "chrome124")

    def test_missing_library_raises_import_error(self):
        with mock.patch.dict(sys.modules, {"curl_cffi": None}):
            with self.assertRaises(ImportError):
                fetch_html_with_curl_cffi("https://x.com")

    def test_error_propagates(self):
        fake_requests = mock.MagicMock()
        fake_requests.get.side_effect = RuntimeError("TLS connect error")
        with mock.patch.dict(
            sys.modules, {"curl_cffi": mock.MagicMock(requests=fake_requests)}
        ):
            with self.assertRaises(RuntimeError):
                fetch_html_with_curl_cffi("https://x.com")


if __name__ == "__main__":
    unittest.main()
