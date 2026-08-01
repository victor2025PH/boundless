# -*- coding: utf-8 -*-
"""LAN GPU 主机宕机升级提醒（`_check_lan_gpu_hosts`）门禁。

2026-08-01 实锤：176（5090 主力，嵌入/视觉/兜底 LLM/本地 MT 所在）整机下线约
两小时——各链路静默转移备点、业务不断，但**零告警**：故障转移网太称职反而掩盖了
「本地算力冗余已归零」这个必须有人知道的事实。本套用例钉住：

  - 目标收集：按主机去重、只认私网、fallback 未启用不收、云端配置空清单；
  - 升级语义：首见只记时点（抖动窗）→ 超阈值首提 → 间隔重提 → 恢复通知只发给
    告过警的（抖动恢复不发，防噪）；
  - 每主机独立状态与限流键（176 与 140 同时出事要各自能报）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw

T0 = 1_754_000_000.0


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _cfg_with_lan(**remind) -> Dict[str, Any]:
    return {
        "ai": {"embedding_base_urls": [
            "http://192.168.0.176:11434", "http://192.168.0.140:11434"]},
        "health_watchdog": {"lan_gpu_remind": dict({"enabled": True}, **remind)},
    }


def _wd(monkeypatch, cfg: Dict[str, Any], down: Dict[str, bool]):
    """down: root_url → 是否宕机（探针替身按表回答）。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _fake_probe(root: str, *, timeout: float = 3.0) -> Dict[str, Any]:
        if down.get(root, False):
            return {"url": root, "reachable": False, "error": "timed out"}
        return {"url": root, "reachable": True, "latency_ms": 3}

    monkeypatch.setattr(hw, "probe_lan_gpu_host", _fake_probe)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=cfg)
    w._lan_gpu_state = {}
    w.total_lan_gpu_reminders = 0
    return w, bus


# ── 目标收集（纯函数）────────────────────────────────────────────


def test_targets_collect_dedupe_private_only():
    cfg = {
        "ai": {
            "embedding_base_urls": ["http://192.168.0.176:11434",
                                    "http://192.168.0.140:11434"],
            "fallback": {"enabled": True,
                         "base_url": "http://192.168.0.176:11434/v1"},
        },
        "vision": {"base_urls": ["http://192.168.0.176:11434"]},
        "translation": {"engines": {"ollama_mt": {
            "base_urls": "http://192.168.0.140:11434, https://api.openai.com"}}},
    }
    out = hw.lan_gpu_probe_targets(cfg)
    # 176 出现三次、140 两次 → 各留一个根；公网 openai 被私网闸门滤掉
    assert out == ["http://192.168.0.176:11434", "http://192.168.0.140:11434"]


def test_targets_empty_for_cloud_only_config():
    assert hw.lan_gpu_probe_targets(
        {"ai": {"base_url": "https://api.deepseek.com"}}) == []
    assert hw.lan_gpu_probe_targets({}) == []
    assert hw.lan_gpu_probe_targets(None) == []          # 防御：非 dict 不炸


def test_targets_fallback_disabled_not_collected():
    cfg = {"ai": {"fallback": {"enabled": False,
                               "base_url": "http://192.168.0.176:11434"}}}
    assert hw.lan_gpu_probe_targets(cfg) == []


def test_targets_strip_v1_suffix_and_keep_port():
    cfg = {"ai": {"embedding_base_url": "http://10.0.0.5:11434/v1"}}
    assert hw.lan_gpu_probe_targets(cfg) == ["http://10.0.0.5:11434"]


# ── 升级语义 ─────────────────────────────────────────────────────


def test_first_sight_records_without_alert(monkeypatch):
    """首见不可达只记时点（给网络抖动一个窗口），绝不当场轰人。"""
    w, bus = _wd(monkeypatch, _cfg_with_lan(),
                 {"http://192.168.0.176:11434": True})
    w._check_lan_gpu_hosts(now=T0)
    assert bus.events == []
    assert w._lan_gpu_state["http://192.168.0.176:11434"]["down_since"] == T0


def test_alert_after_threshold_then_reminder_interval(monkeypatch, caplog):
    import logging as _logging
    down = {"http://192.168.0.176:11434": True}
    w, bus = _wd(monkeypatch, _cfg_with_lan(after_min=30, interval_min=240), down)
    w._check_lan_gpu_hosts(now=T0)                       # 首见记时
    with caplog.at_level(_logging.WARNING, logger="src.inbox.health_watchdog"):
        w._check_lan_gpu_hosts(now=T0 + 31 * 60)         # 超 30min → 首提
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "lan_gpu_alert"
    assert p["host"] == "192.168.0.176:11434"
    assert p["reminder"] is False
    assert p["down_minutes"] >= 31
    assert p["rate_key"] == "lan_gpu:192.168.0.176:11434"
    # 告警必须留**持久痕迹**：webhook 可能 0 通道、SSE 只对在线页面直播——
    # 日志行是本机唯一保证在的通道（2026-08-01 首次实弹验收时发现的盲区）
    assert any("LAN GPU 主机不可达" in r.message for r in caplog.records), \
        "告警只发总线没落日志＝0 通道部署下无任何持久痕迹"

    w._check_lan_gpu_hosts(now=T0 + 2 * 3600)            # 间隔内不重提
    assert len(bus.events) == 1
    w._check_lan_gpu_hosts(now=T0 + 31 * 60 + 241 * 60)  # 过重提间隔 → 重提
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True
    assert w.total_lan_gpu_reminders == 2


def test_recovery_notice_only_if_alerted(monkeypatch):
    root = "http://192.168.0.176:11434"
    down = {root: True}
    w, bus = _wd(monkeypatch, _cfg_with_lan(after_min=30), down)
    w._check_lan_gpu_hosts(now=T0)
    w._check_lan_gpu_hosts(now=T0 + 31 * 60)             # 首提
    down[root] = False
    w._check_lan_gpu_hosts(now=T0 + 40 * 60)             # 恢复 → 通知 + 清零
    assert len(bus.events) == 2
    assert bus.events[1][1].get("recovered") is True
    assert w._lan_gpu_state[root]["down_since"] == 0.0
    assert w._lan_gpu_state[root]["alerted"] == 0.0


def test_jitter_recovery_stays_silent(monkeypatch):
    """没告过警的抖动（首见后很快恢复）不发恢复通知——防噪。"""
    root = "http://192.168.0.176:11434"
    down = {root: True}
    w, bus = _wd(monkeypatch, _cfg_with_lan(), down)
    w._check_lan_gpu_hosts(now=T0)                       # 首见
    down[root] = False
    w._check_lan_gpu_hosts(now=T0 + 5 * 60)              # 5min 后恢复
    assert bus.events == []


def test_hosts_tracked_independently(monkeypatch):
    """176 宕、140 好 → 只有 176 报；限流键带各自主机。"""
    down = {"http://192.168.0.176:11434": True,
            "http://192.168.0.140:11434": False}
    w, bus = _wd(monkeypatch, _cfg_with_lan(after_min=30), down)
    w._check_lan_gpu_hosts(now=T0)
    w._check_lan_gpu_hosts(now=T0 + 31 * 60)
    assert len(bus.events) == 1
    assert bus.events[0][1]["host"] == "192.168.0.176:11434"


def test_disabled_config_is_silent(monkeypatch):
    w, bus = _wd(monkeypatch, _cfg_with_lan(enabled=False),
                 {"http://192.168.0.176:11434": True})
    w._check_lan_gpu_hosts(now=T0)
    w._check_lan_gpu_hosts(now=T0 + 3600)
    assert bus.events == []
    assert w._lan_gpu_state == {}
