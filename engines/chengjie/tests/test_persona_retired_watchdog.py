# -*- coding: utf-8 -*-
"""撤销设定「复活」巡检门禁（2026-08-04 P2 收尾）。

背景：Studio 保存路径已有落库前 retired_conflicts 检测，但直改
profiles_runtime.yaml 的写入方（agent 批量丰富 / 运维手改）绕过 API——
2026-08-02 的批量丰富正是把运营删过的猫内容写回了档案。本巡检以进程内存态
（热重载几秒内跟文件）兜底。门禁重点：该报的报（首报/指纹变化/到期重提）、
不该报的绝不报（关配置/干净档案/窗口内指纹不变/没报过的抖动恢复）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


_CONFLICTED = {
    "id": "lin_xiaoyu",
    "name": "小雨",
    "context": {"hobbies": ["撸学校后门的流浪猫"]},
    "boundaries": {"retired_facts": [
        {"text": "养猫（已删）", "added": "2026-08-03", "terms": ["猫"]},
    ]},
}
_CLEAN = {
    "id": "clean_p",
    "name": "干净",
    "context": {"hobbies": ["煮茶"]},
    "boundaries": {"retired_facts": [
        {"text": "养猫（已删）", "added": "2026-08-03", "terms": ["猫"]},
    ]},
}


def _wd(monkeypatch, profiles: Dict[str, dict], cfg_extra: Any = None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    fake_pm = SimpleNamespace(
        _profile_personas=dict(profiles),
        maybe_reload_runtime_profiles=lambda **k: False,
    )
    monkeypatch.setattr(
        "src.utils.persona_manager.PersonaManager.get_instance",
        classmethod(lambda cls: fake_pm))
    conf: Dict[str, Any] = {
        "health_watchdog": {"persona_retired_remind": dict(cfg_extra or {})}}
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=conf)
    w._retired_scan_ts = 0.0
    w._retired_fp = ""
    w._retired_alerted = False
    w._retired_last_remind = 0.0
    w.total_persona_retired_alerts = 0
    return w, bus, fake_pm


def test_first_alert_names_persona_and_term(monkeypatch):
    w, bus, _ = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED})
    w._check_persona_retired_conflicts(now=time.time())
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "persona_retired_alert"
    assert p["personas"] == ["lin_xiaoyu"]
    assert p["total"] == 1
    assert p["conflicts"]["lin_xiaoyu"][0]["term"] == "猫"
    assert p["conflicts"]["lin_xiaoyu"][0]["path"] == "context.hobbies[0]"
    assert p["reminder"] is False
    assert w.total_persona_retired_alerts == 1


def test_silent_when_clean_or_disabled(monkeypatch):
    w, bus, _ = _wd(monkeypatch, {"clean_p": _CLEAN})
    w._check_persona_retired_conflicts(now=time.time())
    assert bus.events == []                    # 干净且没报过 → 连恢复通知都不发
    w2, bus2, _ = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED},
                      {"enabled": False})
    w2._check_persona_retired_conflicts(now=time.time())
    assert bus2.events == []                   # 运营显式关 → 全静默


def test_unchanged_fingerprint_waits_for_remind_window(monkeypatch):
    w, bus, _ = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED},
                    {"interval_min": 5, "remind_min": 1440})
    t0 = time.time()
    w._check_persona_retired_conflicts(now=t0)
    assert len(bus.events) == 1
    # 6 分钟后再扫：指纹没变、24h 未到 → 不重发
    w._check_persona_retired_conflicts(now=t0 + 6 * 60)
    assert len(bus.events) == 1
    # 24h 后：重提（reminder=True）
    w._check_persona_retired_conflicts(now=t0 + 25 * 3600)
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_fingerprint_change_realerts_immediately(monkeypatch):
    w, bus, pm = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED},
                     {"interval_min": 5})
    t0 = time.time()
    w._check_persona_retired_conflicts(now=t0)
    assert len(bus.events) == 1
    # 新的复活点出现（另一个人设也冲突）→ 指纹变化，窗口内立即再报且不算重提
    pm._profile_personas["p2"] = {
        "id": "p2", "context": {"hobbies": ["猫咖打卡"]},
        "boundaries": {"retired_facts": [{"text": "x", "terms": ["猫咖"]}]},
    }
    w._check_persona_retired_conflicts(now=t0 + 6 * 60)
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is False
    assert set(bus.events[1][1]["personas"]) == {"lin_xiaoyu", "p2"}


def test_recovery_only_after_alerted(monkeypatch):
    w, bus, pm = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED},
                     {"interval_min": 5})
    t0 = time.time()
    w._check_persona_retired_conflicts(now=t0)
    assert len(bus.events) == 1
    # 运营清掉复活内容 → 下一轮补恢复通知，且状态复位
    pm._profile_personas["lin_xiaoyu"] = dict(_CLEAN, id="lin_xiaoyu")
    w._check_persona_retired_conflicts(now=t0 + 6 * 60)
    assert len(bus.events) == 2
    assert bus.events[1][1].get("recovered") is True
    assert w._retired_alerted is False and w._retired_fp == ""


def test_interval_throttles_scan(monkeypatch):
    w, bus, _ = _wd(monkeypatch, {"lin_xiaoyu": _CONFLICTED},
                    {"interval_min": 60})
    t0 = time.time()
    w._check_persona_retired_conflicts(now=t0)
    # 10 分钟后（< interval）：连扫都不扫
    w._check_persona_retired_conflicts(now=t0 + 600)
    assert len(bus.events) == 1


def test_pm_failure_is_silent(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _boom(cls):
        raise RuntimeError("pm down")

    monkeypatch.setattr(
        "src.utils.persona_manager.PersonaManager.get_instance",
        classmethod(_boom))
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(
        config={"health_watchdog": {"persona_retired_remind": {}}})
    w._retired_scan_ts = 0.0
    w._retired_fp = ""
    w._retired_alerted = False
    w._retired_last_remind = 0.0
    w.total_persona_retired_alerts = 0
    w._check_persona_retired_conflicts(now=time.time())
    assert bus.events == []
