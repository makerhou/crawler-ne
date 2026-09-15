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
from src.url_decoder import decode_google_news_url


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
    """解码封装：成功/失败/异常均不抛错，失败返回 None。"""

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
    """协程不得被重复 await。

    回归背景：旧实现在 `loop.run_until_complete(coro)` 抛异常后，
    用**同一个协程对象**回退 `asyncio.run(coro)`，于是报
    "cannot reuse already awaited coroutine"，真实错误被掩盖，
    表现为换 3 次身份全部失败且错误信息一模一样。
    """

    def test_execution_error_propagates_without_reuse(self):
        """执行中异常：直接向上抛，不拿旧协程重试。"""
        fetcher = NodriverFetcher()
        fake_loop = mock.MagicMock()
        fake_loop.is_closed.return_value = False
        fake_loop.run_until_complete.side_effect = RuntimeError("真实抓取失败")

        uc_module = mock.MagicMock()
        uc_module.loop.return_value = fake_loop

        created: list = []

        def factory():
            coro = _dummy_coro("html")
            created.append(coro)
            return coro

        with mock.patch.dict(sys.modules, {"nodriver": uc_module}):
            with self.assertRaises(RuntimeError) as ctx:
                fetcher._run_coroutine(factory)

        self.assertIn("真实抓取失败", str(ctx.exception))
        # 只创建过 1 个协程：说明没有拿已 await 的协程去回退
        self.assertEqual(len(created), 1)
        self.assertEqual(fake_loop.run_until_complete.call_count, 1)

        # mock 不会真正 await 协程，手动关闭以免 "was never awaited" 警告
        for coro in created:
            coro.close()

    def test_loop_unavailable_falls_back_with_fresh_coroutine(self):
        """nodriver 不可用时回退 asyncio.run（协程尚未执行，安全）。"""
        fetcher = NodriverFetcher()
        uc_module = mock.MagicMock()
        uc_module.loop.side_effect = TypeError("需要 Python 3.10+")

        with mock.patch.dict(sys.modules, {"nodriver": uc_module}):
            result = fetcher._run_coroutine(lambda: _dummy_coro("ok"))

        self.assertEqual(result, "ok")

    def test_closed_loop_falls_back_to_asyncio_run(self):
        """单例 loop 已关闭时改用 asyncio.run，避免 'Loop is closed'。"""
        fetcher = NodriverFetcher()
        fake_loop = mock.MagicMock()
        fake_loop.is_closed.return_value = True

        uc_module = mock.MagicMock()
        uc_module.loop.return_value = fake_loop

        with mock.patch.dict(sys.modules, {"nodriver": uc_module}):
            result = fetcher._run_coroutine(lambda: _dummy_coro("ok"))

        self.assertEqual(result, "ok")
        fake_loop.run_until_complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
