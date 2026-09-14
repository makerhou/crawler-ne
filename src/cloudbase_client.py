"""CloudBase OpenAPI 客户端（Python 复刻版）。

背景：CloudBase **没有官方 Python 服务端 SDK**（PyPI 无 cloudbase / tcb 包），
其服务端 SDK 仅覆盖 Node.js / Java / PHP / Go。本项目爬虫用 Python，
因此按其 Node SDK（@cloudbase/node-sdk）的协议精确复刻：

  1. 网关域名：https://{envId}.api.tcloudbasegateway.com
     （非中国大陆区域为 .api.intl.tcloudbasegateway.com）
  2. 鉴权：标准腾讯云 **TC3-HMAC-SHA256** 签名（service='tcb'）
     - 签名算法经与官方 SDK 逐位对比验证一致
     - ⚠️ 关键：Authorization 末尾必须追加 `, Timestamp={秒级时间戳}`
  3. 换取 token：POST /auth/v1/token/clientCredential {grant_type:'client_credentials'}
  4. 访问数据库：/v1/rdb/rest/{table}（PostgREST 风格）
     请求头：Authorization: Bearer {token}
            X-Db-Instance / Accept-Profile / Content-Profile

相比直连 MySQL 的优势：无需公网地址、无需 IP 白名单、无需数据库账号密码，
只用 CloudBase 的 secretId/secretKey 鉴权，与 server 侧访问方式完全一致。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger("reuters-crawler")

# 中国大陆区域（其它区域走 intl 网关）
ZONE_CHINA = {
    "ap-shanghai",
    "ap-guangzhou",
    "ap-shenzhen-fsi",
    "ap-shanghai-fsi",
    "ap-nanjing",
    "ap-beijing",
    "ap-chengdu",
    "ap-chongqing",
    "ap-hongkong",
}

ALGORITHM = "TC3-HMAC-SHA256"
SERVICE = "tcb"
USER_AGENT = "tcb-python-sdk/1.0"


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hmac(key: bytes | str, data: str) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


class CloudBaseError(RuntimeError):
    """CloudBase OpenAPI 调用失败。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"CloudBase API HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class CloudBaseClient:
    """CloudBase OpenAPI 客户端（含 token 自动获取与缓存）。"""

    def __init__(
        self,
        env_id: str,
        secret_id: str,
        secret_key: str,
        region: str = "ap-shanghai",
        db_instance: str = "",
        db_name: str = "",
        timeout: int = 30,
        proxies: dict[str, str] | None = None,
    ) -> None:
        if not (env_id and secret_id and secret_key):
            raise ValueError("缺少 CLOUDBASE_ENV_ID / SECRETID / SECRETKEY")

        self.env_id = env_id
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.db_instance = db_instance or "default"
        self.db_name = db_name or env_id
        self.timeout = timeout
        self.proxies = proxies

        suffix = "api.tcloudbasegateway.com"
        if region not in ZONE_CHINA:
            suffix = "api.intl.tcloudbasegateway.com"
        self.host = f"{env_id}.{suffix}"

        self._token = ""
        self._token_expire_at = 0.0

    # ---------- 签名 ----------
    def _build_auth(self, method: str, path: str, headers: dict[str, str], payload: str) -> dict[str, str]:
        """生成带签名的请求头（TC3-HMAC-SHA256）。"""
        ts = int(time.time()) - 1  # 与官方 SDK 一致：减 1 秒
        signed = dict(headers)
        signed["host"] = self.host
        signed["user-agent"] = USER_AGENT
        signed["x-client-timestamp"] = str(ts)
        signed["x-sdk-version"] = USER_AGENT
        signed.setdefault("x-tcb-source", ",unknown")
        if self.region:
            signed["x-tcb-region"] = self.region

        signed_headers = ";".join(sorted(signed))
        canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
        canonical_request = "\n".join(
            [
                method.upper(),
                path,
                "",
                canonical_headers,
                signed_headers,
                _sha256_hex(payload),
            ]
        )

        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        scope = f"{date}/{SERVICE}/tc3_request"
        string_to_sign = "\n".join(
            [ALGORITHM, str(ts), scope, _sha256_hex(canonical_request)]
        )

        secret_date = _hmac(f"TC3{self.secret_key}", date)
        secret_service = _hmac(secret_date, SERVICE)
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # ⚠️ 官方 SDK 在 Authorization 末尾追加 Timestamp，缺失会 401
        signed["Authorization"] = (
            f"{ALGORITHM} Credential={self.secret_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}, Timestamp={ts}"
        )
        return signed

    # ---------- token ----------
    def _fetch_token(self) -> str:
        path = "/auth/v1/token/clientCredential"
        payload = json.dumps({"grant_type": "client_credentials"}, separators=(",", ":"))
        headers = self._build_auth("POST", path, {"content-type": "application/json"}, payload)

        resp = requests.post(
            f"https://{self.host}{path}",
            headers=headers,
            data=payload.encode("utf-8"),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if resp.status_code != 200:
            raise CloudBaseError(resp.status_code, resp.text)

        data = resp.json()
        token = data.get("access_token") or ""
        if not token:
            raise CloudBaseError(resp.status_code, f"响应无 access_token: {resp.text[:200]}")

        expires_in = int(data.get("expires_in") or 7200)
        # 提前 5 分钟刷新
        self._token_expire_at = time.time() + max(60, expires_in - 300)
        self._token = token
        logger.info("CloudBase token 获取成功（有效期 %ds）", expires_in)
        return token

    @property
    def token(self) -> str:
        if self._token and time.time() < self._token_expire_at:
            return self._token
        return self._fetch_token()

    # ---------- 通用请求 ----------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: list | dict | None = None,
        extra_headers: dict[str, str] | None = None,
        sign: bool = False,
    ) -> requests.Response:
        """发起请求。

        :param sign: True 时用 secretId/secretKey 签名（换取 token 等接口）；
                     False 时用 Bearer token（访问 rdb 等）
        """
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""

        if sign:
            headers = self._build_auth(
                method, path, {"content-type": "application/json"}, payload
            )
        else:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
        if extra_headers:
            headers.update(extra_headers)

        return requests.request(
            method.upper(),
            f"https://{self.host}{path}",
            headers=headers,
            params=params,
            data=payload.encode("utf-8") if payload else None,
            timeout=self.timeout,
            proxies=self.proxies,
        )

    # ---------- rdb（PostgREST 风格）----------
    def _rdb_headers(self, prefer: str = "") -> dict[str, str]:
        headers = {
            "X-Db-Instance": self.db_instance,
            "Accept-Profile": self.db_name,
            "Content-Profile": self.db_name,
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def rdb_select(
        self,
        table: str,
        select: str = "*",
        filters: dict[str, str] | None = None,
        limit: int | None = None,
        order: str = "",
    ) -> list[dict]:
        """查询记录。filters 为 {列: PostgREST 过滤表达式}，如 {'url': 'eq.https://x'}。"""
        params: dict[str, Any] = {"select": select}
        for col, expr in (filters or {}).items():
            params[col] = expr
        if limit:
            params["limit"] = limit
        if order:
            params["order"] = order

        resp = self.request(
            "GET", f"/v1/rdb/rest/{table}", params=params, extra_headers=self._rdb_headers()
        )
        if resp.status_code != 200:
            raise CloudBaseError(resp.status_code, resp.text)
        return resp.json()

    def rdb_insert(
        self,
        table: str,
        rows: dict | list[dict],
        *,
        upsert: bool = False,
        returning: bool = True,
    ) -> list[dict]:
        """插入记录；upsert=True 时遇唯一冲突则更新（merge-duplicates）。"""
        prefer_parts = []
        if upsert:
            prefer_parts.append("resolution=merge-duplicates")
        if returning:
            prefer_parts.append("return=representation")
        prefer = ",".join(prefer_parts)

        resp = self.request(
            "POST",
            f"/v1/rdb/rest/{table}",
            body=rows,
            extra_headers=self._rdb_headers(prefer),
        )
        # 409：唯一冲突（upsert 未开启时）
        if resp.status_code not in (200, 201, 204):
            raise CloudBaseError(resp.status_code, resp.text)
        if returning and resp.text.strip():
            try:
                return resp.json()
            except ValueError:
                return []
        return []

    def rdb_update(
        self, table: str, updates: dict, filters: dict[str, str], returning: bool = False
    ) -> list[dict]:
        """按条件更新记录。"""
        prefer = "return=representation" if returning else ""
        resp = self.request(
            "PATCH",
            f"/v1/rdb/rest/{table}",
            params=dict(filters),
            body=updates,
            extra_headers=self._rdb_headers(prefer),
        )
        if resp.status_code not in (200, 201, 204):
            raise CloudBaseError(resp.status_code, resp.text)
        if returning and resp.text.strip():
            try:
                return resp.json()
            except ValueError:
                return []
        return []
