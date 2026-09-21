"""dry-run 落本地 JSON 与正文图片抽取的单元测试（离线）。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.article_parser import extract_images
from src.scheduler import _log_dry_run, _save_dry_run_article, _slugify


class TestSlugify(unittest.TestCase):
    def test_keep_alnum(self):
        self.assertEqual(_slugify("Hello World!"), "Hello_World")

    def test_strip_edges(self):
        self.assertEqual(_slugify("  Foo: Bar  "), "Foo_Bar")

    def test_empty(self):
        self.assertEqual(_slugify(""), "article")

    def test_truncate_to_50(self):
        self.assertEqual(len(_slugify("a" * 200)), 50)


class TestExtractImages(unittest.TestCase):
    HTML = (
        "<html><body>"
        "<article>"
        "<p>body</p>"
        '<img src="/img/a.jpg" />'
        '<img data-src="https://cdn.reuters.com/b.png" />'
        '<img srcset="https://cdn.reuters.com/c.jpg 2x, https://cdn.reuters.com/c2.jpg 1x" />'
        '<img src="https://reuters.com/logo.png" />'
        "</article>"
        '<div class="ad"><img src="/ad/x.jpg" /></div>'
        "</body></html>"
    )
    BASE = "https://reuters.com/news/123"

    def test_extracts_absolute_and_srcset(self):
        imgs = extract_images(self.HTML, self.BASE)
        self.assertEqual(len(imgs), 3)
        self.assertIn("https://reuters.com/img/a.jpg", imgs)
        self.assertIn("https://cdn.reuters.com/b.png", imgs)
        self.assertIn("https://cdn.reuters.com/c.jpg", imgs)  # srcset 取首个

    def test_filters_logo_and_outside_article(self):
        imgs = extract_images(self.HTML, self.BASE)
        self.assertNotIn("https://reuters.com/logo.png", imgs)   # 含 logo 被过滤
        self.assertNotIn("https://reuters.com/ad/x.jpg", imgs)   # article 外不取

    def test_empty_html(self):
        self.assertEqual(extract_images("", self.BASE), [])

    def test_dedup(self):
        html = (
            "<article>"
            '<img src="https://x.com/1.jpg" />'
            '<img src="https://x.com/1.jpg" />'
            "</article>"
        )
        self.assertEqual(extract_images(html, self.BASE), ["https://x.com/1.jpg"])


class TestSaveDryRunArticle(unittest.TestCase):
    def test_writes_full_content(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            content = "正文" * 1000
            path = _save_dry_run_article(
                out_dir, 1, "Test Title!", "https://reuters.com/x",
                "Jane Doe", "2026-09-21", content, "trafilatura",
            )
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "01_Test_Title.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["content"], content)
            self.assertEqual(data["content_length"], len(content))
            self.assertEqual(data["author"], "Jane Doe")
            self.assertEqual(data["extraction_strategy"], "trafilatura")
            self.assertEqual(data["publish_time"], "2026-09-21")
            self.assertEqual(data["index"], 1)
            self.assertEqual(data["images"], [])
            self.assertIsNotNone(data["saved_at"])

    def test_writes_images(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            imgs = ["https://x.com/1.jpg", "https://x.com/2.jpg"]
            path = _save_dry_run_article(
                out_dir, 2, "P", "https://reuters.com/p", None, "", "c", "trafilatura",
                images=imgs,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["images"], imgs)


class TestLogDryRun(unittest.TestCase):
    def test_writes_file_and_logs_when_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            with self.assertLogs("reuters-crawler", level="INFO") as cm:
                _log_dry_run(
                    1, "Title X", "https://example.com",
                    {"author": "A", "images": ["https://x.com/a.jpg"]},
                    "2026-09-21", "全文内容" * 100,
                    strategy="trafilatura", out_dir=out_dir,
                )
            self.assertTrue((out_dir / "01_Title_X.json").exists())
            self.assertTrue(any("已落盘" in line for line in cm.output))
            self.assertTrue(any("图片" in line for line in cm.output))

    def test_no_file_when_out_dir_none(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs("reuters-crawler", level="INFO"):
                _log_dry_run(
                    1, "Title", "https://example.com", {"images": []}, "", "x",
                    out_dir=None,
                )
            self.assertEqual(len(list(Path(d).glob("*.json"))), 0)


if __name__ == "__main__":
    unittest.main()
