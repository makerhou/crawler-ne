"""节点轮换器测试（mock 面板，不发起真实网络请求）。

运行：
    cd crawler && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.node_rotator import NodeRotator

# 节点样例：覆盖「住宅+干净」「住宅+被标代理」「移动」「未知」四类
NODES = [
    {"id": "JP_a", "ip_type": "residential", "quality": "proxy", "ping": 10, "sessions": 50},
    {"id": "US_b", "ip_type": "residential", "quality": "normal", "ping": 20, "sessions": 0},
    {"id": "KR_c", "ip_type": "mobile", "quality": "mobile", "ping": 30, "sessions": 5},
    {"id": "XX_d", "ip_type": "unknown", "quality": "proxy", "ping": 5, "sessions": 1},
]


class _Resp:
    """极简 requests.Response 替身。"""

    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestEnabled(unittest.TestCase):
    """未配置面板时必须安全关闭，不能影响主流程。"""

    def test_disabled_without_url_or_session(self):
        self.assertFalse(NodeRotator().enabled)
        self.assertFalse(NodeRotator(panel_url="http://p/8787").enabled)
        self.assertFalse(NodeRotator(session="abc").enabled)

    def test_enabled_with_both(self):
        self.assertTrue(NodeRotator(panel_url="http://p/8787", session="abc").enabled)

    def test_switch_returns_none_when_disabled(self):
        self.assertIsNone(NodeRotator().switch())


class TestFetchNodes(unittest.TestCase):
    def test_parses_payload(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s")
        with mock.patch("src.node_rotator.requests") as req:
            req.get.return_value = _Resp({"nodes": NODES})
            nodes = rot.fetch_nodes(force=True)
        self.assertEqual(len(nodes), 4)

    def test_cache_avoids_repeated_calls(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s", cache_ttl=300)
        with mock.patch("src.node_rotator.requests") as req:
            req.get.return_value = _Resp({"nodes": NODES})
            rot.fetch_nodes(force=True)
            rot.fetch_nodes()
            self.assertEqual(req.get.call_count, 1)

    def test_failure_returns_empty_not_crash(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s")
        with mock.patch("src.node_rotator.requests") as req:
            req.get.side_effect = RuntimeError("panel down")
            self.assertEqual(rot.fetch_nodes(force=True), [])


class TestSelectNode(unittest.TestCase):
    def setUp(self):
        self.rot = NodeRotator(panel_url="http://p/8787", session="s")
        self.rot.fetch_nodes = mock.Mock(return_value=NODES)

    def test_prefers_clean_residential(self):
        """住宅 + 未被标记代理(quality=normal) 优先。"""
        self.assertEqual(self.rot.select_node()["id"], "US_b")

    def test_excludes_used_nodes(self):
        first = self.rot.select_node()
        self.rot._used_ids.add(first["id"])
        second = self.rot.select_node()
        self.assertNotEqual(first["id"], second["id"])

    def test_resets_after_all_used(self):
        """节点池轮换一轮后重置，保证仍能继续换。"""
        for node in NODES:
            self.rot._used_ids.add(node["id"])
        self.assertIsNotNone(self.rot.select_node())

    def test_returns_none_when_no_nodes(self):
        self.rot.fetch_nodes = mock.Mock(return_value=[])
        self.assertIsNone(self.rot.select_node())


# 带探测状态的节点：已验证可用 / 未检测 / 已失效
NODES_WITH_PROBE = [
    {"id": "JP_ok", "ip_type": "residential", "quality": "normal",
     "ping": 30, "sessions": 9, "probe_status": "available"},
    {"id": "US_untested", "ip_type": "residential", "quality": "normal",
     "ping": 10, "sessions": 0, "probe_status": "not_checked"},
    {"id": "KR_dead", "ip_type": "residential", "quality": "normal",
     "ping": 5, "sessions": 0, "probe_status": "unavailable"},
]


class TestTestNode(unittest.TestCase):
    """切换前的节点预检（test_node）。"""

    def _rot(self):
        return NodeRotator(panel_url="http://p/8787", session="s")

    def test_available_node_passes(self):
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp(
                {"ok": True, "node": {"id": "JP_ok", "probe_status": "available"}}
            )
            self.assertTrue(rot.test_node("JP_ok"))

    def test_unavailable_node_fails(self):
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp(
                {"ok": True, "node": {"id": "KR_dead", "probe_status": "unavailable",
                                      "probe_message": "OpenVPN 身份验证失败"}}
            )
            self.assertFalse(rot.test_node("KR_dead"))

    def test_ok_false_fails(self):
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({"ok": False})
            self.assertFalse(rot.test_node("X"))

    def test_non_200_fails(self):
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({}, status=500)
            self.assertFalse(rot.test_node("X"))

    def test_exception_fails(self):
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.side_effect = RuntimeError("timeout")
            self.assertFalse(rot.test_node("X"))

    def test_missing_probe_status_falls_back_to_ok(self):
        """probe_status 缺失时以 ok 为准（兼容不同面板版本）。"""
        rot = self._rot()
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({"ok": True, "node": {"id": "X"}})
            self.assertTrue(rot.test_node("X"))

    def test_disabled_rotator_returns_false(self):
        self.assertFalse(NodeRotator().test_node("X"))


class TestSelectNodeWithProbe(unittest.TestCase):
    """优选已验证可用节点、排除已知失效节点。"""

    def setUp(self):
        self.rot = NodeRotator(panel_url="http://p/8787", session="s")
        self.rot.fetch_nodes = mock.Mock(return_value=NODES_WITH_PROBE)

    def test_prefers_verified_available(self):
        self.assertEqual(self.rot.select_node()["id"], "JP_ok")

    def test_excludes_known_unavailable(self):
        """即便失效节点 ping 更低，也不应被选中。"""
        picked = {self.rot.select_node()["id"] for _ in range(3)}
        self.assertNotIn("KR_dead", picked)

    def test_falls_back_when_only_unavailable(self):
        """只剩失效节点时仍返回候选（由后续预检判定），不返回 None。"""
        self.rot.fetch_nodes = mock.Mock(return_value=[dict(NODES_WITH_PROBE[2])])
        self.assertIsNotNone(self.rot.select_node())


class TestConnect(unittest.TestCase):
    def test_posts_id_and_session(self):
        rot = NodeRotator(panel_url="http://p/8787", session="sess")
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({"ok": True})
            ok = rot.connect("US_b")
        self.assertTrue(ok)
        kwargs = req.post.call_args.kwargs
        self.assertEqual(kwargs["json"], {"id": "US_b"})
        self.assertEqual(kwargs["cookies"], {"session": "sess"})

    def test_non_200_is_failure(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s")
        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({}, status=500)
            self.assertFalse(rot.connect("US_b"))

    def test_exception_is_failure(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s")
        with mock.patch("src.node_rotator.requests") as req:
            req.post.side_effect = RuntimeError("timeout")
            self.assertFalse(rot.connect("US_b"))


class TestExitIp(unittest.TestCase):
    def test_uses_crawler_proxy(self):
        """探测出口 IP 必须走爬虫代理（出口由代理提供）。"""
        proxies = {"https": "http://127.0.0.1:7928"}
        rot = NodeRotator(panel_url="http://p/8787", session="s", proxies=proxies)
        with mock.patch("src.node_rotator.requests") as req:
            req.get.return_value = _Resp({"ip": "9.9.9.9"})
            self.assertEqual(rot.current_exit_ip(), "9.9.9.9")
        self.assertEqual(req.get.call_args.kwargs.get("proxies"), proxies)

    def test_returns_none_on_failure(self):
        rot = NodeRotator(panel_url="http://p/8787", session="s")
        with mock.patch("src.node_rotator.requests") as req:
            req.get.side_effect = RuntimeError("no network")
            self.assertIsNone(rot.current_exit_ip())


class TestSwitch(unittest.TestCase):
    def setUp(self):
        self.rot = NodeRotator(
            panel_url="http://p/8787", session="s", wait_seconds=0
        )
        self.rot.fetch_nodes = mock.Mock(return_value=NODES)
        # 默认预检通过（各用例按需覆盖），避免真实请求面板
        self.rot.test_node = mock.Mock(return_value=True)

    def test_success_when_ip_changes(self):
        ips = ["1.1.1.1"] + ["2.2.2.2"] * 5
        with mock.patch.object(self.rot, "current_exit_ip", side_effect=ips):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.assertEqual(self.rot.switch(), "2.2.2.2")

    def test_fails_when_ip_unchanged(self):
        """切换后出口 IP 始终未变 → 判定失败（避免白等一整轮）。"""
        with mock.patch.object(self.rot, "current_exit_ip", return_value="1.1.1.1"):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.assertIsNone(self.rot.switch())

    def test_fails_when_no_exit_ip(self):
        with mock.patch.object(self.rot, "current_exit_ip", return_value=None):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.assertIsNone(self.rot.switch())

    def test_fails_when_connect_fails(self):
        with mock.patch.object(self.rot, "connect", return_value=False):
            self.assertIsNone(self.rot.switch())

    def test_fails_when_no_node(self):
        self.rot.fetch_nodes = mock.Mock(return_value=[])
        self.assertIsNone(self.rot.switch())

    def test_marks_node_as_used(self):
        ips = ["1.1.1.1"] + ["2.2.2.2"] * 5
        with mock.patch.object(self.rot, "current_exit_ip", side_effect=ips):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.rot.switch()
        self.assertEqual(self.rot._used_ids, {"US_b"})

    # ---------- 切换前预检（本次新增）----------
    def test_precheck_failure_skips_to_next_candidate(self):
        """预检不通过 → 换下一个候选，直到命中可用节点。"""
        self.rot.test_node = mock.Mock(side_effect=[False, True])
        ips = ["1.1.1.1"] + ["2.2.2.2"] * 5
        with mock.patch.object(self.rot, "current_exit_ip", side_effect=ips):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.assertEqual(self.rot.switch(), "2.2.2.2")
        self.assertEqual(self.rot.test_node.call_count, 2)
        self.assertTrue(len(self.rot._failed_ids) >= 1)

    def test_all_candidates_fail_precheck(self):
        """所有候选都预检失败 → 放弃换 IP。"""
        self.rot.test_node = mock.Mock(return_value=False)
        self.assertIsNone(self.rot.switch())

    def test_respects_max_candidates(self):
        """候选数达到上限后停止尝试。"""
        self.rot.test_node = mock.Mock(return_value=False)
        self.rot.switch()
        self.assertEqual(self.rot.test_node.call_count, self.rot.max_candidates)

    def test_precheck_disabled_skips_test(self):
        """关闭预检时不做 test_node，直接 connect。"""
        rot = NodeRotator(
            panel_url="http://p/8787", session="s", wait_seconds=0, precheck=False
        )
        rot.fetch_nodes = mock.Mock(return_value=NODES)
        rot.test_node = mock.Mock(return_value=True)
        ips = ["1.1.1.1"] + ["2.2.2.2"] * 5
        with mock.patch.object(rot, "current_exit_ip", side_effect=ips):
            with mock.patch.object(rot, "connect", return_value=True):
                self.assertEqual(rot.switch(), "2.2.2.2")
        rot.test_node.assert_not_called()

    def test_proxy_down_before_switch_still_attempts(self):
        """切换前探不到出口 IP（代理已断）→ 仍尝试换节点恢复。"""
        ips = [None, "2.2.2.2", "2.2.2.2", "2.2.2.2", "2.2.2.2"]
        with mock.patch.object(self.rot, "current_exit_ip", side_effect=ips):
            with mock.patch.object(self.rot, "connect", return_value=True):
                self.assertEqual(self.rot.switch(), "2.2.2.2")


class TestPanelProxyIsolation(unittest.TestCase):
    """访问面板不得走爬虫代理：面板控制代理，切换瞬间走代理会自锁。"""

    def test_panel_calls_do_not_use_crawler_proxy(self):
        rot = NodeRotator(
            panel_url="http://p/8787",
            session="s",
            proxies={"https": "http://127.0.0.1:7928"},
        )
        with mock.patch("src.node_rotator.requests") as req:
            req.get.return_value = _Resp({"nodes": NODES})
            rot.fetch_nodes(force=True)
            self.assertIsNone(req.get.call_args.kwargs.get("proxies"))

        with mock.patch("src.node_rotator.requests") as req:
            req.post.return_value = _Resp({"ok": True})
            rot.connect("US_b")
            self.assertIsNone(req.post.call_args.kwargs.get("proxies"))

    def test_panel_proxy_can_be_overridden(self):
        rot = NodeRotator(
            panel_url="http://p/8787",
            session="s",
            proxies={"https": "http://127.0.0.1:7928"},
            panel_proxy="http://127.0.0.1:1087",
        )
        with mock.patch("src.node_rotator.requests") as req:
            req.get.return_value = _Resp({"nodes": NODES})
            rot.fetch_nodes(force=True)
            self.assertEqual(
                req.get.call_args.kwargs.get("proxies"),
                {"http": "http://127.0.0.1:1087", "https": "http://127.0.0.1:1087"},
            )


if __name__ == "__main__":
    unittest.main()
