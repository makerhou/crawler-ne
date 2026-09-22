"""单元测试：不依赖网络与数据库，可离线运行。

运行：
    cd crawler
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.article_parser import build_excerpt
from src.cloudbase_client import CloudBaseError
from src.config import Config
from src.nodriver_fetcher import NodriverFetcher
from src.repository import ArticleRepository
from src.rss_fetcher import clean_title
from src.url_decoder import decode_google_news_url, _decode_base64, _decode_via_redirect


class TestCleanTitle(unittest.TestCase):
    """RSS 标题清洗：去掉 ' - 来源' 后缀。"""

    def test_removes_source_suffix(self):
        self.assertEqual(clean_title("Fed holds rates steady - Reuters"), "Fed holds rates steady")

    def test_keeps_plain_title(self):
        self.assertEqual(clean_title("Oil prices rise"), "Oil prices rise")

    def test_empty_title(self):
        self.assertEqual(clean_title(""), "")

    def test_keeps_inner_dash(self):
        self.assertEqual(clean_title("US-EU deal struck - Reuters"), "US-EU deal struck")

    def test_none_like_input(self):
        self.assertEqual(clean_title("   "), "")


class TestBuildExcerpt(unittest.TestCase):
    """正文摘要片段生成。"""

    def test_none_input(self):
        self.assertIsNone(build_excerpt(None))

    def test_short_text_unchanged(self):
        self.assertEqual(build_excerpt("hello"), "hello")

    def test_long_text_truncated(self):
        text = "a" * 1000
        self.assertEqual(len(build_excerpt(text)), 500)

    def test_custom_limit(self):
        self.assertEqual(len(build_excerpt("b" * 100, limit=30)), 30)


class TestConfig(unittest.TestCase):
    """配置加载与校验。"""

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_defaults(self):
        for key in ("INTERVAL_SECONDS", "MAX_PER_ROUND", "DECODE_INTERVAL", "TRIGGER_LLM"):
            os.environ.pop(key, None)
        config = Config()
        self.assertEqual(config.interval_seconds, 300)
        self.assertEqual(config.max_per_round, 20)
        self.assertEqual(config.decode_interval, 1)
        self.assertTrue(config.trigger_llm)

    def test_env_override(self):
        os.environ["INTERVAL_SECONDS"] = "60"
        os.environ["MAX_PER_ROUND"] = "5"
        os.environ["TRIGGER_LLM"] = "false"
        config = Config()
        self.assertEqual(config.interval_seconds, 60)
        self.assertEqual(config.max_per_round, 5)
        self.assertFalse(config.trigger_llm)

    def test_invalid_int_falls_back_to_default(self):
        os.environ["INTERVAL_SECONDS"] = "not-a-number"
        self.assertEqual(Config().interval_seconds, 300)

    def test_validate_raises_when_missing(self):
        os.environ.pop("DB_USER", None)
        os.environ.pop("DB_NAME", None)
        os.environ["DB_USER"] = ""
        os.environ["DB_NAME"] = ""
        with self.assertRaises(ValueError):
            Config().validate()

    def test_proxies_parsed(self):
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:8080"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8080"
        proxies = Config().proxies
        self.assertIsNotNone(proxies)
        self.assertEqual(proxies["http"], "http://127.0.0.1:8080")


class TestDecodeGoogleNewsUrl(unittest.TestCase):
    """解码封装：三级 fallback（base64 → 库 → 重定向），均不抛错。"""

    def setUp(self):
        # 所有测试默认关闭重定向 fallback，避免单测走真实网络
        self._redirect_patcher = mock.patch(
            "src.url_decoder._decode_via_redirect", return_value=None
        )
        self._redirect_patcher.start()

    def tearDown(self):
        self._redirect_patcher.stop()

    def test_empty_url(self):
        self.assertIsNone(decode_google_news_url(""))

    def test_success(self):
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "status": True,
            "decoded_url": "https://www.reuters.com/article/123",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertEqual(result, "https://www.reuters.com/article/123")

    def test_failure_status(self):
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {"status": False, "message": "rate limited"}
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertIsNone(result)

    def test_exception_is_swallowed(self):
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.side_effect = RuntimeError("network down")
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertIsNone(result)

    def test_proxy_passed_through(self):
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "status": True,
            "decoded_url": "https://www.reuters.com/article/456",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            decode_google_news_url("https://news.google.com/rss/articles/xyz", proxy="http://p:8080")
        _, kwargs = fake_module.gnewsdecoder.call_args
        self.assertEqual(kwargs["proxy"], "http://p:8080")

    # ---- 需求 11.9：googlenewsdecoder 新版返回 success 键，旧版返回 status 键 ----

    def test_success_with_success_key(self):
        """新版库用 success=True：必须能取到 URL（修复前会被误判为失败）。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "success": True,
            "decoded_url": "https://www.reuters.com/article/789",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertEqual(result, "https://www.reuters.com/article/789")

    def test_failure_with_success_key(self):
        """新版库 success=False（真失败）→ None。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "success": False,
            "message": "Failed to fetch data attributes from Google News.",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertIsNone(result)

    def test_success_false_not_fallback_to_status(self):
        """success=False 时不回退 status：即便带 status=True 也判失败。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "success": False,
            "status": True,
            "decoded_url": "https://www.reuters.com/article/should-not-use",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertIsNone(result)

    def test_legacy_status_key_still_works(self):
        """旧版库只有 status 键（无 success）→ 兼容成功。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "status": True,
            "decoded_url": "https://www.reuters.com/article/legacy",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertEqual(result, "https://www.reuters.com/article/legacy")

    def test_failure_message_placeholder_when_missing(self):
        """三级解码均失败 → 日志打"三级解码均失败"，不再出现 None。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {"success": False}
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            with self.assertLogs("reuters-crawler", level="WARNING") as cm:
                result = decode_google_news_url("https://news.google.com/rss/articles/xyz")
        self.assertIsNone(result)
        self.assertNotIn("None", cm.output[0])
        self.assertIn("三级解码均失败", cm.output[0])

    # ---- 三级 fallback：base64 本地解码 ----

    def test_base64_decode_real_url(self):
        """含明文 URL 的 base64 URL → 本地直接解出，无需网络。"""
        # 手工构造：base64("https://www.reuters.com/test-art") 加 protobuf 前缀
        import base64 as b64
        raw = b"https://www.reuters.com/test-art"
        # protobuf tag: \x08\x01\x12\x20 (field1=1, field2 len-prefixed) + length byte
        prefix = b"\x08\x01\x12" + bytes([len(raw)])
        encoded = b64.urlsafe_b64encode(prefix + raw).decode().rstrip("=")
        url = f"https://news.google.com/rss/articles/{encoded}"
        result = _decode_base64(url)
        self.assertEqual(result, "https://www.reuters.com/test-art")

    def test_base64_decode_returns_none_for_garbage(self):
        """base64 内容不含 URL → 返回 None（不报错）。"""
        self.assertIsNone(_decode_base64("https://news.google.com/rss/articles/xyz"))

    def test_base64_wins_over_library(self):
        """base64 能解出时，不会调用库（零网络请求优先）。"""
        import base64 as b64
        raw = b"https://example.com/article-1"
        prefix = b"\x08\x01\x12" + bytes([len(raw)])
        encoded = b64.urlsafe_b64encode(prefix + raw).decode().rstrip("=")
        url = f"https://news.google.com/rss/articles/{encoded}"

        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {
            "success": True,
            "decoded_url": "https://should-not-be-used.com",
        }
        with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}):
            result = decode_google_news_url(url)
        # base64 解码优先，库不应被调用
        self.assertEqual(result, "https://example.com/article-1")
        fake_module.gnewsdecoder.assert_not_called()

    # ---- 三级 fallback：HTTP 重定向 ----

    def test_redirect_fallback_on_library_failure(self):
        """库解码失败 → 自动尝试 HTTP 重定向。"""
        fake_module = mock.MagicMock()
        fake_module.gnewsdecoder.return_value = {"success": False, "message": "parse error"}

        # 临时放开 redirect mock
        self._redirect_patcher.stop()
        try:
            fake_resp = mock.MagicMock()
            fake_resp.status_code = 302
            fake_resp.headers = {"Location": "https://www.reuters.com/redirect-target"}

            with mock.patch.dict(sys.modules, {"googlenewsdecoder": fake_module}), \
                 mock.patch("requests.get", return_value=fake_resp):
                result = decode_google_news_url("https://news.google.com/rss/articles/abc123")
        finally:
            self._redirect_patcher.start()

        self.assertEqual(result, "https://www.reuters.com/redirect-target")


class TestRepositoryOpenidCompat(unittest.TestCase):
    """CloudBase 表 `_openid` 兼容：先带值写入，若报列不存在则自动去掉并记住该表。"""

    def _make_repo(self) -> ArticleRepository:
        config = Config()
        config.cloudbase_env_id = "test-env"
        config.cloudbase_secret_id = "sid"
        config.cloudbase_secret_key = "skey"
        config.cloudbase_openid = "system"
        repo = ArticleRepository(config)
        repo.client = mock.MagicMock()  # 不真实联网
        return repo

    def test_insert_includes_openid(self):
        repo = self._make_repo()
        repo._insert_row("t_articles", {"title": "t"}, returning=True)
        sent = repo.client.rdb_insert.call_args[0][1]
        self.assertEqual(sent["_openid"], "system")
        self.assertEqual(sent["title"], "t")

    def test_retry_without_openid_when_column_missing(self):
        repo = self._make_repo()
        repo.client.rdb_insert.side_effect = [
            CloudBaseError(400, '{"message":"column _openid does not exist"}'),
            [{"id": 1}],
        ]
        repo._insert_row("t_articles", {"title": "t"}, returning=True)
        # 第二次调用不再携带 _openid，并记住该表
        second = repo.client.rdb_insert.call_args_list[1][0][1]
        self.assertNotIn("_openid", second)
        self.assertIn("t_articles", repo._no_openid_tables)

    def test_skips_openid_for_known_table(self):
        repo = self._make_repo()
        repo._no_openid_tables.add("t_articles")
        repo._insert_row("t_articles", {"title": "t"}, returning=True)
        sent = repo.client.rdb_insert.call_args[0][1]
        self.assertNotIn("_openid", sent)

    def test_other_errors_propagate(self):
        repo = self._make_repo()
        repo.client.rdb_insert.side_effect = CloudBaseError(500, "boom")
        with self.assertRaises(CloudBaseError):
            repo._insert_row("t_articles", {"title": "t"}, returning=True)


async def _dummy_coro(value: str) -> str:
    """供 _run_coroutine 测试使用的协程。"""
    return value


class TestNodriverCoroutineNoReuse(unittest.TestCase):
    """_run_coroutine 每次用 asyncio.run() 创建全新 event loop。

    回归背景：旧实现复用 nodriver 单例 loop（uc.loop()），第 1 篇成功后
    loop 处于「表面停止但内部仍被绑定」状态，第 2 篇起全部报
    ``Cannot run the event loop while another loop is running``。

    当前实现每次 ``asyncio.run()`` 创建全新 loop，彻底避开单例 loop 的坑。
    """

    def test_success_returns_result(self):
        """正常执行返回协程结果。"""
        fetcher = NodriverFetcher()
        result = fetcher._run_coroutine(lambda: _dummy_coro("ok"))
        self.assertEqual(result, "ok")

    def test_error_propagates(self):
        """协程中异常直接向上抛，不被吞掉。"""
        async def _failing_coro():
            raise RuntimeError("真实抓取失败")

        fetcher = NodriverFetcher()
        with self.assertRaises(RuntimeError) as ctx:
            fetcher._run_coroutine(lambda: _failing_coro())
        self.assertIn("真实抓取失败", str(ctx.exception))

    def test_multiple_calls_succeed(self):
        """连续多次调用均能成功（回归：旧实现第 2 次起必失败）。"""
        fetcher = NodriverFetcher()
        for i in range(3):
            result = fetcher._run_coroutine(lambda: _dummy_coro(f"ok-{i}"))
            self.assertEqual(result, f"ok-{i}")


if __name__ == "__main__":
    unittest.main()
