# -*- coding: utf-8 -*-
"""#150 A3 门禁：语音连发从「只告警」升级「熔断降级文字」。

证据：钧机 JZPBAC 21:03-21:09——两台内测机的自家 AI 号互当真人客户，语音一条接
一条（4 条/分钟），voice_burst_guard 21:06:31 WARNING count=4 却一条没少发。

钉住的口径：
- 第 threshold_n 条（默认 3）当场开路并降级本条；冷却期内一律降级；到期自动恢复。
- 告警阈值（>3）与熔断阈值（>=3）分离，正常 2 条分条不触发熔断。
- 配置可关（burst_alert.enabled / circuit.enabled）；任何异常放行。
- A/B 两条发送线都接了决策入口（静态钉，防单边接线）。
"""
import inspect

from src.client import voice_burst_guard as vbg
from src.client.voice_burst_guard import VoiceBurstGuard, voice_degrade_reason


def _fresh(monkeypatch):
    g = VoiceBurstGuard()
    monkeypatch.setattr(vbg, "_SINGLETON", g)
    return g


def test_third_voice_in_window_trips_and_degrades(monkeypatch):
    g = _fresh(monkeypatch)
    t0 = 1_000_000.0
    # 前两条正常语音（分条上限内）→ 放行
    assert voice_degrade_reason("c1", now=t0) == ""
    g.record("c1", now=t0)
    assert voice_degrade_reason("c1", now=t0 + 10) == ""
    g.record("c1", now=t0 + 10)
    # 第 3 条 → 开路，本条降级
    assert voice_degrade_reason("c1", now=t0 + 20) == "burst_circuit_tripped"
    # 冷却期内继续降级
    assert voice_degrade_reason("c1", now=t0 + 100) == "burst_circuit_open"
    # 冷却到期（默认 300s）自动恢复
    assert voice_degrade_reason("c1", now=t0 + 20 + 301) == ""
    snap = g.snapshot()
    assert snap["circuit_opens"] == 1 and snap["degraded_to_text"] == 2


def test_two_bubbles_do_not_trip(monkeypatch):
    """正常分条 2 条是设计内行为，绝不熔断。"""
    g = _fresh(monkeypatch)
    t0 = 2_000.0
    g.record("c2", now=t0)
    assert voice_degrade_reason("c2", now=t0 + 1) == ""


def test_window_slides_old_sends_expire(monkeypatch):
    g = _fresh(monkeypatch)
    t0 = 5_000.0
    g.record("c3", now=t0)
    g.record("c3", now=t0 + 1)
    # 61s 后旧的两条出窗 → 新一条只算第 1 条，不触发
    assert voice_degrade_reason("c3", now=t0 + 61) == ""


def test_chats_are_isolated(monkeypatch):
    g = _fresh(monkeypatch)
    t0 = 9_000.0
    for i in range(3):
        g.record("hot", now=t0 + i)
    assert voice_degrade_reason("hot", now=t0 + 3) == "burst_circuit_tripped"
    assert voice_degrade_reason("cold", now=t0 + 3) == ""


def test_config_switches(monkeypatch):
    g = _fresh(monkeypatch)
    t0 = 3_000.0
    for i in range(5):
        g.record("c4", now=t0 + i)
    assert voice_degrade_reason("c4", {"burst_alert": {"enabled": False}}, now=t0 + 5) == ""
    assert voice_degrade_reason(
        "c4", {"burst_alert": {"circuit": {"enabled": False}}}, now=t0 + 5) == ""
    # 阈值可调：threshold_n=10 时 5 条不触发
    assert voice_degrade_reason(
        "c4", {"burst_alert": {"circuit": {"threshold_n": 10}}}, now=t0 + 5) == ""
    assert voice_degrade_reason("c4", now=t0 + 5) == "burst_circuit_tripped"


def test_open_circuit_extends_not_shrinks(monkeypatch):
    g = _fresh(monkeypatch)
    until1 = g.open_circuit("c5", cooldown_sec=300, now=100.0)
    until2 = g.open_circuit("c5", cooldown_sec=10, now=150.0)   # 更短的不覆盖
    assert until2 == until1 == 400.0
    assert g.snapshot()["circuit_opens"] == 1                    # 续期不算新开路


def test_both_send_lines_wired():
    """A 线 sender.voice_reply 与 B 线 autosend_helpers 都必须调决策入口。"""
    from src.client import sender
    from src.inbox import autosend_helpers
    assert "voice_degrade_reason" in inspect.getsource(sender)
    assert "voice_degrade_reason" in inspect.getsource(autosend_helpers)
