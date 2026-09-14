"""数据访问层 —— 基于 CloudBase OpenAPI（不直连 MySQL）。

与 server 侧（@cloudbase/node-sdk 的 app.rdb()）使用同一套鉴权与访问方式：
secretId/secretKey → 换取 access_token → 访问 /v1/rdb/rest/{table}（PostgREST 风格）。

这样无需数据库公网地址、无需 IP 白名单、无需数据库账号密码。

`_openid` 兼容：CloudBase 部分表含 NOT NULL 的 `_openid` 列。
REST 接口无法像 SQL 那样查 INFORMATION_SCHEMA，因此采用
「先带 _openid 写入，若报列不存在则自动去掉并记住该表」的降级策略。
"""

from __future__ import annotations

import logging
from typing import Any

from .cloudbase_client import CloudBaseClient, CloudBaseError
from .config import Config

logger = logging.getLogger("reuters-crawler")

# 报这些关键字说明表没有 _openid 列
_NO_OPENID_HINTS = ("_openid", "42703", "does not exist", "unknown column", "column")


class ArticleRepository:
    """文章/作者/任务/日志的读写（CloudBase OpenAPI）。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = CloudBaseClient(
            env_id=config.cloudbase_env_id,
            secret_id=config.cloudbase_secret_id,
            secret_key=config.cloudbase_secret_key,
            region=config.cloudbase_region,
            db_instance=config.cloudbase_rdb_instance,
            db_name=config.cloudbase_rdb_database,
            timeout=config.http_timeout,
            # 注意：CloudBase 是国内服务，直连即可，**不走** HTTP_PROXY
            # （该代理是给爬虫访问 Google/Reuters 用的，走它会变慢甚至失败）
            proxies=None,
        )
        # 已确认「没有 _openid 列」的表，避免每次重试
        self._no_openid_tables: set[str] = set()

    def close(self) -> None:
        """CloudBase 走 HTTP，无需关闭连接（保留接口以兼容调用方）。"""

    # ---------- _openid 兼容 ----------
    def _insert_row(self, table: str, data: dict[str, Any], **kwargs) -> list[dict]:
        """插入一行，自动处理 _openid 列是否存在。"""
        openid = self.config.cloudbase_openid
        if openid and table not in self._no_openid_tables:
            try:
                return self.client.rdb_insert(table, {**data, "_openid": openid}, **kwargs)
            except CloudBaseError as exc:
                body = (exc.body or "").lower()
                if any(hint in body for hint in _NO_OPENID_HINTS):
                    logger.info("表 %s 无 _openid 列，后续写入不再携带", table)
                    self._no_openid_tables.add(table)
                else:
                    raise
        return self.client.rdb_insert(table, data, **kwargs)

    # ---------- 文章 ----------
    def find_article_id_by_url(self, url: str) -> int | None:
        """按真实 URL 判重（t_articles.url 为唯一键）。"""
        rows = self.client.rdb_select(
            "t_articles", select="id", filters={"url": f"eq.{url}"}, limit=1
        )
        if not rows:
            return None
        return int(rows[0]["id"])

    def insert_article(self, article: dict) -> int:
        """写入文章，返回新记录 id。"""
        rows = self._insert_row("t_articles", dict(article), returning=True)
        if rows and isinstance(rows, list) and rows[0].get("id") is not None:
            return int(rows[0]["id"])
        raise RuntimeError("插入文章未返回 id，请检查表结构与返回配置")

    # ---------- 作者 ----------
    def upsert_author(self, name: str, uuid_value: str) -> int:
        """按姓名查找作者，不存在则新建，返回 author id。"""
        rows = self.client.rdb_select(
            "t_authors", select="id", filters={"name": f"eq.{name}"}, limit=1
        )
        if rows:
            return int(rows[0]["id"])

        inserted = self._insert_row(
            "t_authors", {"name": name, "uuid": uuid_value}, returning=True
        )
        if inserted and inserted[0].get("id") is not None:
            return int(inserted[0]["id"])

        # 未返回 id 时回查一次（并发插入等场景）
        rows = self.client.rdb_select(
            "t_authors", select="id", filters={"name": f"eq.{name}"}, limit=1
        )
        return int(rows[0]["id"]) if rows else 0

    def link_article_author(self, article_id: int, author_id: int) -> None:
        """写入文章-作者关联（已存在则忽略）。"""
        existing = self.client.rdb_select(
            "t_article_authors",
            select="article_id",
            filters={"article_id": f"eq.{article_id}", "author_id": f"eq.{author_id}"},
            limit=1,
        )
        if existing:
            return
        self._insert_row(
            "t_article_authors",
            {"article_id": article_id, "author_id": author_id},
            returning=False,
        )

    # ---------- 触发 LLM ----------
    def upsert_analysis_task(self, article_id: int) -> None:
        """写入 pending 任务（article_id 唯一，冲突则更新为待处理）。"""
        self._insert_row(
            "t_analysis_task",
            {"article_id": article_id, "status": "pending", "retry_count": 0},
            upsert=True,
            returning=False,
        )

    # ---------- 日志 ----------
    def write_crawler_log(
        self,
        level: str,
        action: str,
        message: str,
        is_success: bool,
        added: int = 0,
        skipped: int = 0,
        failed: int = 0,
        duration_ms: int = 0,
        articles_added: int | None = None,
        articles_failed: int | None = None,
    ) -> None:
        data = {
            "platform": "reuters",
            "level": level,
            "action": action,
            "message": message,
            "is_success": 1 if is_success else 0,
            "articles_added": articles_added if articles_added is not None else added,
            "articles_failed": articles_failed if articles_failed is not None else failed,
            "duration_ms": duration_ms,
        }
        try:
            self._insert_row("t_crawler_logs", data, returning=False)
        except Exception as exc:
            logger.warning("写 t_crawler_logs 失败（不影响主流程）: %s", exc)
