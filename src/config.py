"""配置加载：优先读 .env，其次读进程环境变量。

不引入 python-dotenv，避免额外依赖；这里实现一个最小的 .env 解析器。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_RSS_URL = (
    "https://news.google.com/rss/search?q=site%3Areuters.com+markets+OR+business"
    "+OR+stocks+OR+economy+OR+fed+OR+earnings&hl=en-US&gl=US&ceid=US%3Aen"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def load_env_file(env_path: str | Path = ".env") -> None:
    """读取 .env 并写入 os.environ（已存在的变量不会被覆盖）。"""
    path = Path(env_path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fp:
        for raw_line in fp:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            # 去掉包裹的引号
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Config:
    """爬虫运行配置。"""

    def __init__(self) -> None:
        # 数据库：走 CloudBase OpenAPI（不直连 MySQL）
        # 优势：无需公网地址、无需 IP 白名单、无需数据库账号密码，
        #       只用 secretId/secretKey 鉴权，与 server 侧访问方式一致。
        self.cloudbase_env_id: str = os.environ.get("CLOUDBASE_ENV_ID", "")
        self.cloudbase_secret_id: str = os.environ.get("CLOUDBASE_SECRETID", "")
        self.cloudbase_secret_key: str = os.environ.get("CLOUDBASE_SECRETKEY", "")
        self.cloudbase_region: str = os.environ.get("CLOUDBASE_REGION", "ap-shanghai")
        self.cloudbase_rdb_instance: str = os.environ.get("CLOUDBASE_RDB_INSTANCE", "")
        self.cloudbase_rdb_database: str = os.environ.get("CLOUDBASE_RDB_DATABASE", "")
        # CloudBase 部分表含 NOT NULL 的 _openid 列，写入时需补值
        self.cloudbase_openid: str = os.environ.get("CLOUDBASE_OPENID", "system")

        # RSS
        self.rss_url: str = os.environ.get("RSS_URL", DEFAULT_RSS_URL)

        # 调度
        self.interval_seconds: int = _get_int("INTERVAL_SECONDS", 300)
        self.max_per_round: int = _get_int("MAX_PER_ROUND", 20)

        # 解码
        self.decode_interval: int = _get_int("DECODE_INTERVAL", 1)

        # 抓取
        self.http_timeout: int = _get_int("HTTP_TIMEOUT", 30)
        self.user_agent: str = os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)

        # 代理（可选）
        self.proxies: dict[str, str] | None = None
        http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if http_proxy or https_proxy:
            self.proxies = {}
            if http_proxy:
                self.proxies["http"] = http_proxy
            if https_proxy:
                self.proxies["https"] = https_proxy

        # 日志
        self.log_dir: str = os.environ.get("LOG_DIR", "./logs")
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

        # 抓取策略：
        #   requests   = 普通 HTTP（无 TLS 伪装，易被 Cloudflare 识别）
        #   curl_cffi  = 模拟真实浏览器 TLS/JA3 指纹（过 Cloudflare 的关键）
        #   playwright = headless 浏览器（最稳但最慢）
        #   auto（推荐）= curl_cffi 快通道，失败或抽不到正文再回退 playwright
        self.fetch_mode: str = os.environ.get("FETCH_MODE", "auto").strip().lower()
        if self.fetch_mode not in {"requests", "curl_cffi", "playwright", "nodriver", "auto"}:
            self.fetch_mode = "auto"

        # nodriver 反检测通道（对抗 DataDome；需 Python 3.10+，不可用时自动跳过）
        # 实测：DataDome 能识别 headless 模式（headless=True 必被拦），故默认 False。
        # 服务器无显示器时用 xvfb 运行：xvfb-run -a python main.py
        self.nodriver_headless: bool = _get_bool("NODRIVER_HEADLESS", False)
        self.nodriver_wait_seconds: float = float(os.environ.get("NODRIVER_WAIT_SECONDS", "8"))
        # 被反爬识别后最多切换多少次身份重试
        self.nodriver_max_switch: int = _get_int("NODRIVER_MAX_SWITCH", 3)
        # 每次抓取后的冷却秒数：密集请求会让 IP 被 DataDome 快速拉黑（实测）
        self.nodriver_request_interval: float = float(
            os.environ.get("NODRIVER_REQUEST_INTERVAL", "5")
        )

        # UA 轮换：round_robin / random / off（off 则使用固定 USER_AGENT）
        self.ua_rotation: str = os.environ.get("UA_ROTATION", "round_robin").strip().lower()
        if self.ua_rotation not in {"round_robin", "random", "off"}:
            self.ua_rotation = "round_robin"

        # curl_cffi 的 TLS 指纹标识（留空则跟随 UA 身份自动匹配）
        self.impersonate: str = os.environ.get("IMPERSONATE", "")

        # headless 浏览器
        self.browser_headless: bool = _get_bool("BROWSER_HEADLESS", True)
        self.browser_timeout_ms: int = _get_int("BROWSER_TIMEOUT_MS", 30_000)
        # 可选：等待某个选择器出现再取 HTML（应对纯 JS 渲染页）；留空则按 wait_ms 固定等待
        self.browser_wait_selector: str = os.environ.get("BROWSER_WAIT_SELECTOR", "")
        self.browser_wait_ms: int = _get_int("BROWSER_WAIT_MS", 1_000)

        # 降级：站点反爬导致抓不到正文时，用 RSS 摘要入库（保证有数据，后续可补抓）
        self.allow_rss_fallback: bool = _get_bool("ALLOW_RSS_FALLBACK", True)

        # 注：LLM 分析能力已迁移到独立的 Go 服务 llm-analysis-server，
        # 相关配置（DASHSCOPE_*、ANALYZER_*、ANALYSIS_*_PROMPT）请在该服务的 .env 中配置。
        # 本服务仅保留 trigger_llm 用于写入待分析任务。

        # 下游
        self.trigger_llm: bool = _get_bool("TRIGGER_LLM", True)

    def validate(self, require_db: bool = True) -> None:
        """校验必填配置，缺失时抛错（便于 systemd 启动时快速暴露问题）。

        :param require_db: dry-run（只抓取不入库）时可传 False
        """
        missing = []
        if require_db:
            if not self.cloudbase_env_id:
                missing.append("CLOUDBASE_ENV_ID")
            if not self.cloudbase_secret_id:
                missing.append("CLOUDBASE_SECRETID")
            if not self.cloudbase_secret_key:
                missing.append("CLOUDBASE_SECRETKEY")
        if not self.rss_url:
            missing.append("RSS_URL")
        if missing:
            raise ValueError(f"缺少必填配置: {', '.join(missing)}（请检查 .env）")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"Config(cloudbase_env={self.cloudbase_env_id}, "
            f"interval={self.interval_seconds}s, max_per_round={self.max_per_round}, "
            f"trigger_llm={self.trigger_llm})"
        )
