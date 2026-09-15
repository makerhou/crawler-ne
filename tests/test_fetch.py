"""抓取策略测试：requests / playwright / auto 三种模式（全部 mock，不依赖网络与浏览器内核）。

运行：
    cd crawler && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.article_parser import build_excerpt, extract_article, fetch_article_detail
from src.browser_fetcher import BrowserFetcher

SAMPLE_HTML = (
    "<html><head><title>Fed holds rates steady</title></head>"
    "<body><article><p>The Federal Reserve left rates unchanged.</p></article></body></html>"
)


def _result_with_content() -> dict:
    return {
        "title": "Fed holds rates steady",
        "content": "The Federal Reserve left rates unchanged.",
        "author": "Jane Doe",
        "publish_time": "2026-09-10",
    }


def _result_empty() -> dict:
    return {"title": None, "content": None, "author": None, "publish_time": None}


class TestExtractArticle(unittest.TestCase):
    """HTML → 结构化内容。"""

    def test_extracts_from_real_html(self):
        result = extract_article(SAMPLE_HTML)
        self.assertTrue(result["content"])
        self.assertIn("Federal Reserve", result["content"])

    def test_empty_html(self):
        self.assertEqual(extract_article("")["content"], None)


class TestFetchStrategy(unittest.TestCase):
    """三种抓取模式的分支行为。"""

    def _browser(self) -> mock.MagicMock:
        browser = mock.MagicMock(spec=BrowserFetcher)
        browser.get_html.return_value = SAMPLE_HTML
        return browser

    # ---- requests 模式 ----
    def test_requests_mode_success(self):
        with mock.patch(
            "src.article_parser.fetch_html_with_requests", return_value=SAMPLE_HTML
        ), mock.patch("src.article_parser.extract_article", return_value=_result_with_content()):
            result = fetch_article_detail("https://reuters.com/a", fetch_mode="requests")
        self.assertEqual(result["author"], "Jane Doe")

    def test_requests_mode_propagates_error(self):
        with mock.patch(
            "src.article_parser.fetch_html_with_requests", side_effect=RuntimeError("403")
        ):
            with self.assertRaises(RuntimeError):
                fetch_article_detail("https://reuters.com/a", fetch_mode="requests")

    def test_requests_mode_does_not_use_browser(self):
        browser = self._browser()
        with mock.patch(
            "src.article_parser.fetch_html_with_requests", return_value=SAMPLE_HTML
        ), mock.patch("src.article_parser.extract_article", return_value=_result_with_content()):
            fetch_article_detail("https://reuters.com/a", browser=browser, fetch_mode="requests")
        browser.get_html.assert_not_called()

    # ---- auto 模式 ----
    def test_auto_uses_browser_when_no_content(self):
        """curl_cffi 抽不到正文 → 浏览器兜底。"""
        browser = self._browser()
        with mock.patch(
            "src.article_parser.fetch_html_with_curl_cffi", return_value=SAMPLE_HTML
        ), mock.patch(
            "src.article_parser.extract_article", return_value=_result_with_content()
        ) as mock_extract:
            # 第一次（requests）返回空，第二次（浏览器）返回有内容
            mock_extract.side_effect = [_result_empty(), _result_with_content()]
            result = fetch_article_detail(
                "https://reuters.com/a", browser=browser, fetch_mode="auto"
            )
        browser.get_html.assert_called_once()
        self.assertEqual(result["content"], "The Federal Reserve left rates unchanged.")

    def test_auto_skips_browser_when_content_ok(self):
        """curl_cffi 已拿到正文 → 不再启动浏览器（省资源）。"""
        browser = self._browser()
        with mock.patch(
            "src.article_parser.fetch_html_with_curl_cffi", return_value=SAMPLE_HTML
        ), mock.patch("src.article_parser.extract_article", return_value=_result_with_content()):
            fetch_article_detail("https://reuters.com/a", browser=browser, fetch_mode="auto")
        browser.get_html.assert_not_called()

    def test_auto_uses_browser_when_requests_raises(self):
        """curl_cffi 直接失败 → 转浏览器。"""
        browser = self._browser()
        with mock.patch(
            "src.article_parser.fetch_html_with_curl_cffi", side_effect=RuntimeError("timeout")
        ), mock.patch("src.article_parser.extract_article", return_value=_result_with_content()):
            result = fetch_article_detail(
                "https://reuters.com/a", browser=browser, fetch_mode="auto"
            )
        browser.get_html.assert_called_once()
        self.assertTrue(result["content"])

    # ---- playwright 模式 ----
    def test_playwright_mode_always_uses_browser(self):
        browser = self._browser()
        with mock.patch("src.article_parser.extract_article", return_value=_result_with_content()):
            result = fetch_article_detail(
                "https://reuters.com/a", browser=browser, fetch_mode="playwright"
            )
        self.assertEqual(browser.get_html.call_count, 1)
        self.assertTrue(result["content"])

    def test_no_browser_injected_logs_and_returns(self):
        """需要浏览器但没注入 → 返回已有结果，不抛异常。"""
        result = fetch_article_detail(
            "https://reuters.com/a", browser=None, fetch_mode="playwright"
        )
        self.assertIsNone(result["content"])


class TestBrowserFetcher(unittest.TestCase):
    """浏览器抓取器（不实际启动内核）。"""

    def test_returns_none_when_unavailable(self):
        fetcher = BrowserFetcher()
        with mock.patch.object(
            BrowserFetcher, "available", new_callable=mock.PropertyMock, return_value=False
        ):
            self.assertIsNone(fetcher.get_html("https://reuters.com/a"))

    def test_returns_html_on_success(self):
        fetcher = BrowserFetcher()
        fake_page = mock.MagicMock()
        fake_page.content.return_value = "<html>rendered</html>"
        fake_context = mock.MagicMock()
        fake_context.new_page.return_value = fake_page
        fetcher._context = fake_context

        with mock.patch.object(
            BrowserFetcher, "available", new_callable=mock.PropertyMock, return_value=True
        ):
            html = fetcher.get_html("https://reuters.com/a")

        self.assertEqual(html, "<html>rendered</html>")
        fake_page.goto.assert_called_once()
        fake_page.close.assert_called_once()

    def test_returns_none_on_failure(self):
        fetcher = BrowserFetcher()
        fake_page = mock.MagicMock()
        fake_page.goto.side_effect = RuntimeError("navigation timeout")
        fake_context = mock.MagicMock()
        fake_context.new_page.return_value = fake_page
        fetcher._context = fake_context

        with mock.patch.object(
            BrowserFetcher, "available", new_callable=mock.PropertyMock, return_value=True
        ):
            html = fetcher.get_html("https://reuters.com/a")

        self.assertIsNone(html)

    def test_empty_url_returns_none(self):
        self.assertIsNone(BrowserFetcher().get_html(""))

    def test_launch_failure_disables_channel(self):
        """内核缺失时只尝试启动一次，之后该通道直接返回 None（避免每篇一条噪音日志）。"""
        fake_pw = mock.MagicMock()
        fake_pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
        fake_module = mock.MagicMock()
        fake_module.sync_playwright.return_value.start.return_value = fake_pw

        fetcher = BrowserFetcher()
        with mock.patch.dict(sys.modules, {"playwright.sync_api": fake_module}):
            self.assertIsNone(fetcher.get_html("https://reuters.com/a"))
            self.assertFalse(fetcher.available)
            # 第二次不再尝试启动
            self.assertIsNone(fetcher.get_html("https://reuters.com/b"))

        self.assertEqual(fake_module.sync_playwright.call_count, 1)


class TestExcerpt(unittest.TestCase):
    def test_excerpt_from_extracted_content(self):
        result = extract_article(SAMPLE_HTML)
        self.assertIsNotNone(build_excerpt(result["content"], 20))
        self.assertLessEqual(len(build_excerpt(result["content"], 20)), 20)


if __name__ == "__main__":
    unittest.main()
