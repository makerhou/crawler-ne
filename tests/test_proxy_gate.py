"""代理健康门禁测试（需求 11.11）：run_once 开工前探测代理，不可用则等待恢复，超时跳过本轮。

背景：切换 VPN 节点会重启 OpenVPN，期间整机代理流量瞬断；
中断窗口内空跑 = 整轮全失败 + 向反爬风控输送异常请求。

运行：
    cd crawler && python -m unittest tests.test_proxy_gate -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import scheduler
from src.config import Config

PROXY = {"https": "http://127.0.0.1:7928"}


def _config(proxies=None):
    cfg = Config()
    cfg.proxies = proxies
    cfg.fetch_mode = "requests"    # 跳过 nodriver/浏览器通道创建，保持测试轻量
    cfg.rotate_on_block = False    # 不创建 rotator
    cfg.proxy_wait_timeout = 0     # 不真实等待（wait_proxy_ready 已被 mock）
    return cfg


class TestRunOnceProxyGate(unittest.TestCase):
    def test_skips_round_when_proxy_never_recovers(self):
        """代理一直不可用 → 跳过本轮，绝不发起 RSS 抓取。"""
        with mock.patch.object(scheduler, "proxy_exit_ip", return_value=None), \
             mock.patch.object(scheduler, "wait_proxy_ready", return_value=None), \
             mock.patch.object(scheduler, "fetch_rss_entries") as rss:
            stats = scheduler.run_once(_config(PROXY), repo=None)
        rss.assert_not_called()
        self.assertEqual(stats["added"], 0)

    def test_proceeds_after_proxy_recovers(self):
        """初检失败但等待后恢复 → 正常开工。"""
        with mock.patch.object(scheduler, "proxy_exit_ip", return_value=None), \
             mock.patch.object(scheduler, "wait_proxy_ready", return_value="2.2.2.2"), \
             mock.patch.object(scheduler, "fetch_rss_entries", return_value=[]) as rss:
            scheduler.run_once(_config(PROXY), repo=None)
        rss.assert_called_once()

    def test_proceeds_immediately_when_proxy_healthy(self):
        """初检通过 → 不进入等待轮询，直接开工。"""
        with mock.patch.object(scheduler, "proxy_exit_ip", return_value="1.1.1.1"), \
             mock.patch.object(scheduler, "wait_proxy_ready") as waiter, \
             mock.patch.object(scheduler, "fetch_rss_entries", return_value=[]):
            scheduler.run_once(_config(PROXY), repo=None)
        waiter.assert_not_called()

    def test_no_gate_without_proxy(self):
        """未配置代理（海外直连部署）→ 不做任何探测，行为与旧版一致。"""
        with mock.patch.object(scheduler, "proxy_exit_ip") as probe, \
             mock.patch.object(scheduler, "fetch_rss_entries", return_value=[]):
            scheduler.run_once(_config(None), repo=None)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
