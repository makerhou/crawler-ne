"""VPN 节点轮换器：命中 DataDome 时切换出口节点，实现「换 IP 重试」。

为什么需要它：
- DataDome 的封禁发生在**出口 IP 层**，换 UA/身份重试已被实测证明无效；
- 出口 IP 由服务器本地代理（如 127.0.0.1:7928）提供，节点由面板控制，
  因此「换 IP」= 调面板 API 切换 VPN 节点。

面板接口（aimilivpn / vpn-gate）：
- GET  /api/nodes      节点列表（含 ip_type / quality / ping / sessions / probe_status）
- POST /api/test_node  实时检测节点是否可用
- POST /api/connect    切换节点

三个关键设计（都来自实测踩坑）：
1. **访问面板默认不走爬虫代理**：面板控制着代理本身，切换瞬间代理会瞬断，
   若访问面板也走该代理会「自锁」；确需代理时用 NODE_PANEL_PROXY 覆盖。
2. **切换前先预检节点可用**（test_node）：实测节点池 96 个中仅 7 个是已验证
   `available`、88 个 `not_checked`，还有失效节点（`ERR_OVPN_AUTH_FAILED`
   免费节点已失效）—— 盲目切换极易切到死节点导致代理中断。预检不通过就
   换下一个候选（上限 MAX_NODE_CANDIDATES）。
3. **切换后验证出口 IP 确实变化**（经爬虫代理访问 ipify），未变化视为失败，
   避免「切了但没生效」导致白等一整轮重试。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger("reuters-crawler")

# 出口 IP 探测服务（返回 {"ip": "x.x.x.x"}）
EXIT_IP_URL = "https://api.ipify.org?format=json"
# 优选：住宅/移动 ASN
PREFERRED_IP_TYPES = ("residential", "mobile")
# 优选：未被反爬库标记为代理的节点（quality=normal）
PREFERRED_QUALITY = "normal"
# test_node 返回的节点探测状态
PROBE_AVAILABLE = "available"
PROBE_UNAVAILABLE = "unavailable"


class NodeRotator:
    """通过面板 API 切换 VPN 节点，实现出口 IP 轮换。"""

    def __init__(
        self,
        panel_url: str = "",
        session: str = "",
        proxies: dict[str, str] | None = None,
        panel_proxy: str = "",
        timeout: float = 15.0,
        cache_ttl: float = 300.0,
        # OpenVPN 重连耗时（面板实测 ~4.6s），切换后需等待代理恢复稳定
        wait_seconds: float = 5.0,
        # 切换前是否先用 test_node 预检节点可用（免费节点失效率高，强烈建议开启）
        precheck: bool = True,
        # 单次换 IP 最多试几个候选节点（每个 test_node ≈ 4.6s）
        max_candidates: int = 5,
    ) -> None:
        self.panel_url = (panel_url or "").rstrip("/")
        self.session = (session or "").strip()
        # 爬虫代理：仅用于「验证出口 IP」（出口由该代理提供）
        self.proxies = proxies or None
        # 访问面板默认直连（防自锁）；配置 NODE_PANEL_PROXY 时才走代理
        self.panel_proxies: dict[str, str] | None = None
        if panel_proxy:
            self.panel_proxies = {"http": panel_proxy, "https": panel_proxy}
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.wait_seconds = wait_seconds
        self.precheck = precheck
        self.max_candidates = max(1, max_candidates)
        self._nodes: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0
        self._used_ids: set[str] = set()
        self._failed_ids: set[str] = set()

    @property
    def enabled(self) -> bool:
        """面板地址与 session 都配置时才可用。"""
        return bool(self.panel_url and self.session)

    # ---------- 面板 API ----------
    def fetch_nodes(self, force: bool = False) -> list[dict[str, Any]]:
        """拉取节点列表（带缓存，避免每次切换都拉全量 JSON）。"""
        now = time.time()
        if not force and self._nodes is not None and now - self._fetched_at < self.cache_ttl:
            return self._nodes

        if not self.enabled:
            return []

        try:
            resp = requests.get(
                f"{self.panel_url}/api/nodes",
                cookies={"session": self.session},
                headers={"Accept": "*/*"},
                timeout=self.timeout,
                proxies=self.panel_proxies,
            )
            resp.raise_for_status()
            nodes = (resp.json() or {}).get("nodes") or []
        except Exception as exc:
            logger.warning("拉取节点列表失败: %s", exc)
            return self._nodes or []

        self._nodes = nodes
        self._fetched_at = now
        logger.info("节点列表已更新，共 %d 个节点", len(nodes))
        return nodes

    def select_node(self) -> dict[str, Any] | None:
        """挑一个候选节点。

        优选顺序：
        1. 已验证可用（`probe_status=available`）优先；排除已知失效（`unavailable`）；
        2. 住宅/移动 ASN + 未被标记为代理（`quality=normal`）优先；
        3. 同档按 sessions（在用人数，越少越干净）、ping 升序。
        已用过 / 预检失败的节点会被跳过（全部耗尽时逐步放开）。
        """
        nodes = self.fetch_nodes()

        def pickable(exclude_used: bool, exclude_failed: bool) -> list[dict[str, Any]]:
            result = []
            for n in nodes:
                nid = n.get("id")
                if not nid:
                    continue
                if exclude_failed and nid in self._failed_ids:
                    continue
                if exclude_used and nid in self._used_ids:
                    continue
                result.append(n)
            return result

        candidates = pickable(exclude_used=True, exclude_failed=True)
        if not candidates:
            # 节点已全部用过 → 放开「已用」，但继续排除「预检失败」的死节点
            self._used_ids.clear()
            candidates = pickable(exclude_used=False, exclude_failed=True)
        if not candidates:
            # 连失效记录也耗尽 → 全部重置再试一轮
            logger.info("所有节点均已尝试，重置失效记录")
            self._failed_ids.clear()
            candidates = pickable(exclude_used=False, exclude_failed=False)
        if not candidates:
            return None

        # 排除列表里已明确失效的节点（除非没得选）
        healthy = [n for n in candidates if n.get("probe_status") != PROBE_UNAVAILABLE]
        if healthy:
            candidates = healthy

        def score(node: dict[str, Any]) -> tuple:
            probe_rank = 0 if node.get("probe_status") == PROBE_AVAILABLE else 1
            good_type = node.get("ip_type") in PREFERRED_IP_TYPES
            good_quality = node.get("quality") == PREFERRED_QUALITY
            if good_type and good_quality:
                tq_rank = 0
            elif good_type:
                tq_rank = 1
            else:
                tq_rank = 2
            return (
                probe_rank,
                tq_rank,
                int(node.get("sessions") or 0),
                int(node.get("ping") or 9999),
            )

        candidates.sort(key=score)
        return candidates[0]

    def test_node(self, node_id: str) -> bool:
        """实时检测节点是否可用（切换前的预检）。

        判定：HTTP 200 且 `ok` 为真，且 `node.probe_status` 不是 `unavailable`。
        `probe_status` 缺失时以 `ok` 为准（兼容不同面板版本）。
        """
        if not self.enabled or not node_id:
            return False

        try:
            resp = requests.post(
                f"{self.panel_url}/api/test_node",
                json={"id": node_id},
                cookies={"session": self.session},
                headers={"Accept": "*/*", "Content-Type": "application/json"},
                timeout=self.timeout,
                proxies=self.panel_proxies,
            )
            if resp.status_code != 200:
                logger.info("节点 %s 预检：HTTP %s", node_id, resp.status_code)
                return False

            payload = resp.json() or {}
            if not payload.get("ok"):
                logger.info("节点 %s 预检：面板返回 ok=false", node_id)
                return False

            node = payload.get("node") or {}
            status = node.get("probe_status")
            if status == PROBE_UNAVAILABLE:
                logger.info(
                    "节点 %s 预检不可用: %s", node_id, node.get("probe_message") or "无原因"
                )
                return False

            logger.info("节点 %s 预检可用（probe_status=%s）", node_id, status or "未提供")
            return True
        except Exception as exc:
            logger.warning("节点 %s 预检异常: %s", node_id, exc)
            return False

    def connect(self, node_id: str) -> bool:
        """切换（连接）到指定节点。"""
        if not self.enabled or not node_id:
            return False

        try:
            resp = requests.post(
                f"{self.panel_url}/api/connect",
                json={"id": node_id},
                cookies={"session": self.session},
                headers={"Accept": "*/*", "Content-Type": "application/json"},
                timeout=self.timeout,
                proxies=self.panel_proxies,
            )
            ok = resp.status_code == 200
            logger.info("切换节点 %s → HTTP %s", node_id, resp.status_code)
            return ok
        except Exception as exc:
            logger.warning("切换节点 %s 失败: %s", node_id, exc)
            return False

    # ---------- 出口 IP ----------
    def current_exit_ip(self) -> str | None:
        """经爬虫代理探测当前出口 IP；探测失败（代理不可出网）返回 None。"""
        try:
            resp = requests.get(EXIT_IP_URL, timeout=self.timeout, proxies=self.proxies)
            resp.raise_for_status()
            return (resp.json() or {}).get("ip")
        except Exception as exc:
            logger.debug("探测出口 IP 失败: %s", exc)
            return None

    # ---------- 对外主入口 ----------
    def switch(self) -> str | None:
        """切换到一个「已确认可用」的新节点，返回新的出口 IP；失败返回 None。

        流程：探测当前代理健康 → 逐个取候选节点 → test_node 预检 → connect 切换
        → 等待重连 → 验证出口 IP 确实变化。任一步失败就换下一个候选。
        """
        if not self.enabled:
            logger.debug("未配置面板，跳过换 IP")
            return None

        old_ip = self.current_exit_ip()
        if not old_ip:
            logger.warning(
                "切换前健康检查：当前代理探测不到出口 IP（代理可能已断），尝试换节点恢复"
            )

        for i in range(1, self.max_candidates + 1):
            node = self.select_node()
            if not node:
                logger.warning("无可切换节点")
                return None

            node_id = node["id"]
            self._used_ids.add(node_id)

            # 预检：确认该节点当前可用后再切换（避免切到死节点导致代理中断）
            if self.precheck and not self.test_node(node_id):
                logger.warning(
                    "节点 %s 预检不可用 → 换下一个候选（%d/%d）",
                    node_id,
                    i,
                    self.max_candidates,
                )
                self._failed_ids.add(node_id)
                continue

            if not self.connect(node_id):
                self._failed_ids.add(node_id)
                continue

            # 等 OpenVPN 重连完成，代理恢复稳定后再验证
            time.sleep(self.wait_seconds)
            new_ip = self.current_exit_ip()
            if not new_ip:
                logger.warning("切换 %s 后探测不到出口 IP → 换下一个候选", node_id)
                self._failed_ids.add(node_id)
                continue
            if old_ip and new_ip == old_ip:
                logger.warning("切换 %s 后出口 IP 未变化 → 换下一个候选", node_id)
                self._failed_ids.add(node_id)
                continue

            logger.info("出口 IP 已切换: %s → %s（节点 %s）", old_ip, new_ip, node_id)
            return new_ip

        logger.warning("已尝试 %d 个候选节点仍未能成功换 IP", self.max_candidates)
        return None

    def reset(self) -> None:
        """清空轮换记录、失效记录与缓存（新一轮开始时调用）。"""
        self._used_ids.clear()
        self._failed_ids.clear()
        self._nodes = None
        self._fetched_at = 0.0
