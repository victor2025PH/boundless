# -*- coding: utf-8 -*-
"""通话主机健康 + 就绪度体检门禁：
- call_health_probe_target（探哪台/该不该探的纯决策）；
- evaluate_call_readiness（开闸前体检：blocker/warning 分级，纯函数）；
- HealthWatchdog._check_native_call（升级式提醒：首提/重提/恢复，探针 monkeypatch）。
"""
import types

from src.voicecall.health import (
    call_health_probe_target,
    evaluate_call_readiness,
)


def _full(enabled=True, brain="s2s", base="http://192.168.0.176:7860",
          transport_verified=True):
    return {"telegram_calls": {"enabled": enabled, "brain": brain,
                               "transport_verified": transport_verified},
            "realtime_voice": {"base_url": base}}


# ── 探测决策 ─────────────────────────────────────────────────────────────────
def test_probe_target_s2s_uses_realtime_host():
    assert call_health_probe_target(_full()) == "http://192.168.0.176:7860"


def test_probe_target_disabled_is_none():
    assert call_health_probe_target(_full(enabled=False)) is None


def test_probe_target_cascade_is_none():
    # cascade 脑当前无独立通话主机（嘴走 CosyVoice，另有健康灯）→ 不探
    assert call_health_probe_target(_full(brain="cascade")) is None


# ── 就绪度体检（纯函数）──────────────────────────────────────────────────────
def test_readiness_disabled():
    r = evaluate_call_readiness(_full(enabled=False))
    assert r["enabled"] is False and r["ready"] is False


def test_readiness_host_unreachable_blocks():
    r = evaluate_call_readiness(_full(), host_probe={"reachable": False})
    assert r["ready"] is False
    assert any("主机不可达" in b for b in r["blockers"])


def test_readiness_model_not_loaded_blocks():
    r = evaluate_call_readiness(_full(),
                                host_probe={"reachable": True, "model_loaded": False})
    assert r["ready"] is False
    assert any("模型未载入" in b for b in r["blockers"])


def test_readiness_transport_not_ready_blocks():
    r = evaluate_call_readiness(_full(),
                                host_probe={"reachable": True, "model_loaded": True},
                                transport_ready=False)
    assert r["ready"] is False
    assert any("传输层" in b for b in r["blockers"])


def test_readiness_transport_verified_config_gate():
    # 未跑 PoC 三闸门（transport_verified 默认 false）→ 即使主机在线也 blocker（防误判绿灯）
    r = evaluate_call_readiness(
        _full(transport_verified=False),
        host_probe={"reachable": True, "model_loaded": True},
        auto_ai_conversations=5)
    assert r["ready"] is False
    assert any("传输层未验证" in b for b in r["blockers"])


def test_readiness_no_auto_ai_blocks():
    r = evaluate_call_readiness(_full(),
                                host_probe={"reachable": True, "model_loaded": True},
                                auto_ai_conversations=0)
    assert r["ready"] is False
    assert any("auto_ai" in b for b in r["blockers"])


def test_readiness_all_clear_with_ref_warning():
    r = evaluate_call_readiness(
        _full(), host_probe={"reachable": True, "model_loaded": True},
        transport_ready=True, auto_ai_conversations=5,
        ref_summary={"persona_count": 3, "with_reference": 0})
    assert r["ready"] is True                       # 无 blocker
    assert any("参考音" in w for w in r["warnings"])  # 但降级内置音色是 warning


def test_readiness_cascade_hardware_warning():
    r = evaluate_call_readiness(_full(brain="cascade"),
                                host_probe={"reachable": True, "model_loaded": True},
                                transport_ready=True)
    assert any("TTFB" in w or "cascade" in w for w in r["warnings"])


# ── 升级式提醒 ───────────────────────────────────────────────────────────────
class _CM:
    def __init__(self, config):
        self.config = config


def _wd(monkeypatch, probe_result, published):
    from src.inbox.health_watchdog import HealthWatchdog
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d):
            published.append((t, d))
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    import src.voicecall.health as H
    monkeypatch.setattr(H, "probe_call_host", lambda cfg, **kw: probe_result)
    cm = _CM({"telegram_calls": {"enabled": True, "brain": "s2s"},
              "realtime_voice": {"base_url": "http://h:7860"},
              "health_watchdog": {"tg_call_remind": {"enabled": True,
                                                     "after_min": 30, "interval_min": 240}}})
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app, config_manager=cm, interval_sec=60)


def test_native_call_first_alert_after_threshold(monkeypatch):
    pub = []
    wd = _wd(monkeypatch, {"reachable": False, "model_loaded": False,
                           "url": "http://h:7860", "error": "refused"}, pub)
    t0 = 1_000_000.0
    wd._check_native_call(now=t0)              # 首次记 down_since，不发
    assert pub == []
    wd._check_native_call(now=t0 + 10 * 60)     # 10min 未到 30min 阈值
    assert pub == []
    wd._check_native_call(now=t0 + 31 * 60)     # 超阈值 → 首提
    assert len(pub) == 1
    assert pub[0][0] == "tg_call_alert"
    assert pub[0][1]["reminder"] is False
    assert wd.total_tg_call_reminders == 1


def test_native_call_recovery_notifies(monkeypatch):
    pub = []
    wd = _wd(monkeypatch, {"reachable": False, "model_loaded": False}, pub)
    t0 = 1_000_000.0
    wd._check_native_call(now=t0)
    wd._check_native_call(now=t0 + 31 * 60)     # 首提
    # 主机恢复 → 换 probe 结果
    import src.voicecall.health as H
    monkeypatch.setattr(H, "probe_call_host",
                        lambda cfg, **kw: {"reachable": True, "model_loaded": True})
    wd._check_native_call(now=t0 + 40 * 60)
    assert pub[-1][1].get("recovered") is True
    assert wd._tgcall_alerted is False


def test_native_call_silent_when_disabled(monkeypatch):
    pub = []
    wd = _wd(monkeypatch, None, pub)            # probe 返回 None（未启用/非 s2s）
    wd._check_native_call(now=1_000_000.0)
    assert pub == []
