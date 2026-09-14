"""本地端到端链路验证（**不需要外网**）。

用本地 HTML 模拟 Reuters 文章页，走完整链路：
    RSS 条目(mock) → 解码(mock，直接返回本地 file:// URL) → **真实浏览器抓取** → trafilatura 抽取 → 输出结果

验证除「Google 网络可达性」以外的全部逻辑是否可用。
浏览器用 file:// 协议加载本地页面，因此不依赖外网。

运行：
    cd crawler && python -m unittest tests.test_pipeline_local -v
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.browser_fetcher import BrowserFetcher
from src.config import Config
from src.scheduler import run_once

ARTICLE_1 = """<html><head><title>Fed holds rates steady as inflation cools</title>
<meta name="author" content="Jane Doe"></head><body><article>
<h1>Fed holds rates steady as inflation cools</h1>
<p>The Federal Reserve left interest rates unchanged on Wednesday, signalling patience as
inflation continued to ease and the labour market showed signs of cooling.</p>
<p>Policymakers said they would monitor incoming data before making further adjustments.</p>
</article></body></html>"""

ARTICLE_2 = """<html><head><title>Oil prices climb on supply concerns</title>
<meta name="author" content="John Smith"></head><body><article>
<h1>Oil prices climb on supply concerns</h1>
<p>Crude futures rose more than two percent on Thursday after fresh supply disruptions
renewed concerns about tight global inventories heading into winter.</p>
</article></body></html>"""


class TestLocalPipeline(unittest.TestCase):
    """本地链路：验证 RSS→解码→浏览器抓取→抽取 全流程（无外网）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crawler-pipeline-")
        self.file1 = pathlib.Path(self.tmp) / "a1.html"
        self.file2 = pathlib.Path(self.tmp) / "a2.html"
        self.file1.write_text(ARTICLE_1, encoding="utf-8")
        self.file2.write_text(ARTICLE_2, encoding="utf-8")

        self.config = Config()
        self.config.fetch_mode = "playwright"
        self.config.max_per_round = 10
        self.config.browser_wait_ms = 300

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pipeline_extracts_both_articles(self):
        entries = [
            {
                "title": "Fed holds rates steady as inflation cools - Reuters",
                "google_url": "https://news.google.com/rss/articles/fake-1",
                "published": "Thu, 10 Sep 2026 08:00:00 GMT",
                "summary": "",
            },
            {
                "title": "Oil prices climb on supply concerns - Reuters",
                "google_url": "https://news.google.com/rss/articles/fake-2",
                "published": "Thu, 10 Sep 2026 09:00:00 GMT",
                "summary": "",
            },
        ]
        # 解码直接映射到本地文件（跳过 Google 网络请求）
        decoded = {
            "https://news.google.com/rss/articles/fake-1": f"file://{self.file1}",
            "https://news.google.com/rss/articles/fake-2": f"file://{self.file2}",
        }

        browser = BrowserFetcher(headless=True, wait_ms=300, timeout_ms=20_000)
        try:
            with mock.patch(
                "src.scheduler.fetch_rss_entries", return_value=entries
            ), mock.patch(
                "src.scheduler.decode_google_news_url", side_effect=lambda url, **kw: decoded.get(url)
            ):
                stats = run_once(self.config, None, browser, dry_run=True)
        finally:
            browser.close()

        self.assertEqual(stats["added"], 2, f"应成功挖掘 2 篇，实际: {stats}")
        self.assertEqual(stats["failed"], 0)

    def test_pipeline_skips_when_no_content(self):
        """正文抽不到的页面应被跳过（计为 failed），不写入。"""
        empty_file = pathlib.Path(self.tmp) / "empty.html"
        empty_file.write_text("<html><body></body></html>", encoding="utf-8")

        entries = [
            {
                "title": "Empty page",
                "google_url": "https://news.google.com/rss/articles/empty",
                "published": "",
                "summary": "",
            }
        ]

        browser = BrowserFetcher(headless=True, wait_ms=200, timeout_ms=15_000)
        try:
            with mock.patch(
                "src.scheduler.fetch_rss_entries", return_value=entries
            ), mock.patch(
                "src.scheduler.decode_google_news_url", return_value=f"file://{empty_file}"
            ):
                stats = run_once(self.config, None, browser, dry_run=True)
        finally:
            browser.close()

        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["failed"], 1)


if __name__ == "__main__":
    unittest.main()
