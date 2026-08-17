"""语音出站断档台账 + 看门狗门禁（2026-08-02）。

背景：zhiliao 语音链断 5 天（hub 音色档 404 → voice_consistency=strict 全部
拒发）零告警——``_check_avatar_voice`` 的 hang 检测依赖「探针绿 + 失败 streak
新鲜度」，对低流量下零星请求全失败不敏感。新判据＝``voice_outage`` 台账滚动窗
「尝试 ≥N 且 0 成功」。

与 ``test_human_deliver_watchdog`` 同哲学：这类巡检的立命之本是**零误报**
（否则会被当噪声关掉），重点覆盖「不该告警」的路径 + 告警/重提/恢复时序。
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ai import voice_outage as vo  # noqa: E402
from src.inbox.health_watchdog import HealthWatchdog  # noqa: E402


class _Clock:
    """替换模块级 ``time`` 名字的假钟（不动全局 time 模块）。"""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def time(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh_singleton():
    vo.reset_for_test()
    yield
    vo.reset_for_test()


# ── 台账：窗口统计 / 连败 / 快照契约 ───────────────────────────────


def test_snapshot_contract_window_and_sources(monkeypatch):
    led = vo.VoiceOutageLedger()
    clock = _Clock(1_000_000.0)
    monkeypatch.setattr(vo, "time", clock)

    clock.t = 1_000_000.0
    led.record_voice_attempt(False, "aline", "synth_failed")   # 窗口外（30h 前）
    clock.t = 1_000_000.0 + 26 * 3600
    led.record_voice_attempt(False, "aline", "synth_failed")
    led.record_voice_attempt(False, "autosend", "")            # 空 reason → unknown
    clock.t = 1_000_000.0 + 27 * 3600
    led.record_voice_attempt(True, "autosend")

    now = 1_000_000.0 + 30 * 3600
    snap = led.outage_snapshot(now=now, window_hours=24)
    assert snap["attempts_24h"] == 3          # 30h 前那条被窗口剔除
    assert snap["ok_24h"] == 1
    assert snap["window_hours"] == 24.0
    assert snap["fail_reasons"] == {"synth_failed": 1, "unknown": 1}
    assert snap["by_source"] == {
        "aline": {"attempts": 1, "ok": 0},
        "autosend": {"attempts": 2, "ok": 1},
    }
    assert snap["last_ok_ts"] == 1_000_000.0 + 27 * 3600
    assert snap["last_fail_ts"] == 1_000_000.0 + 26 * 3600
    # 窗口参数化：拉大到 72h → 全部计入
    snap72 = led.outage_snapshot(now=now, window_hours=72)
    assert snap72["attempts_24h"] == 4 and snap72["ok_24h"] == 1


def test_consecutive_fails_streak_resets_on_ok():
    led = vo.VoiceOutageLedger()
    for _ in range(3):
        led.record_voice_attempt(False, "aline", "synth_failed")
    assert led.outage_snapshot()["consecutive_fails"] == 3
    led.record_voice_attempt(True, "aline")
    assert led.outage_snapshot()["consecutive_fails"] == 0
    led.record_voice_attempt(False, "aline", "x")
    assert led.outage_snapshot()["consecutive_fails"] == 1


def test_ledger_bounded_and_input_hardened():
    led = vo.VoiceOutageLedger()
    for _ in range(250):
        led.record_voice_attempt(False, "aline", "r" * 500)
    snap = led.outage_snapshot(window_hours=24)
    assert snap["attempts_24h"] == 200                      # deque maxlen 兜底
    assert all(len(k) <= 80 for k in snap["fail_reasons"])  # reason 截断
    led.record_voice_attempt(False, None, None)             # 坏输入不抛
    assert "unknown" in led.outage_snapshot()["by_source"]


def test_singleton_accessor_and_reset():
    a = vo.get_voice_outage()
    assert vo.get_voice_outage() is a
    a.record_voice_attempt(False, "aline", "x")
    vo.reset_for_test()
    b = vo.get_voice_outage()
    assert b is not a
    assert b.outage_snapshot()["attempts_24h"] == 0


# ── 看门狗脚手架（镜像 test_human_deliver_watchdog 的 __new__ 绕重依赖法）──


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, etype, data):
        self.events.append((etype, data))


class _StubLedger:
    def __init__(self, snap) -> None:
        self._snap = snap

    def outage_snapshot(self, now=None, *, window_hours=24.0):
        if isinstance(self._snap, dict):
            return dict(self._snap, window_hours=float(window_hours))
        return self._snap


def _wd(monkeypatch, *, snap=None, cfg=None):
    bus = _Bus()
    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)
    if snap is not None:
        monkeypatch.setattr(vo, "get_voice_outage",
                            lambda: _StubLedger(snap))

    class _CM:
        config = cfg if cfg is not None else {}

    wd = HealthWatchdog.__new__(HealthWatchdog)   # 绕开重依赖 __init__
    wd._app = types.SimpleNamespace()
    wd._config_manager = _CM()
    wd._vo_alerted = False
    wd._vo_last_remind = 0.0
    wd.total_voice_outage_alerts = 0
    return wd, bus


def _fail_snap(attempts=4, *, ok=0, streak=None, last_ok=0.0):
    return {
        "attempts_24h": attempts, "ok_24h": ok,
        "last_ok_ts": last_ok, "last_fail_ts": 2_000_000.0,
        "consecutive_fails": attempts if streak is None else streak,
        "fail_reasons": {"synth_failed": max(0, attempts - ok - 1),
                         "edge_rejected": 1},
        "by_source": {"aline": {"attempts": attempts, "ok": ok}},
    }


# ── 「不该告警」的路径（零误报是立命之本）───────────────────────────


def test_quiet_when_not_enough_attempts(monkeypatch):
    wd, bus = _wd(monkeypatch, snap=_fail_snap(2))
    wd._check_voice_outage(now=2_000_000.0)
    wd._check_voice_outage(now=2_000_000.0 + 10 * 3600)
    assert bus.events == []


def test_quiet_when_any_success_in_window(monkeypatch):
    wd, bus = _wd(monkeypatch, snap=_fail_snap(5, ok=1))
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []
    assert wd._vo_alerted is False


def test_quiet_when_disabled(monkeypatch):
    wd, bus = _wd(monkeypatch, snap=_fail_snap(9),
                  cfg={"health_watchdog": {"voice_outage_remind": {
                      "enabled": False}}})
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []


def test_quiet_when_ledger_broken_or_empty(monkeypatch):
    # 台账读取抛异常 → 静默
    def _boom():
        raise RuntimeError("ledger down")

    wd, bus = _wd(monkeypatch)
    monkeypatch.setattr(vo, "get_voice_outage", _boom)
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []
    # 快照坏形（非 dict）→ 静默
    wd2, bus2 = _wd(monkeypatch, snap=None)
    monkeypatch.setattr(vo, "get_voice_outage", lambda: _StubLedger(None))
    wd2._check_voice_outage(now=2_000_000.0)
    assert bus2.events == []
    # 台账真空（0 尝试）→ 静默
    wd3, bus3 = _wd(monkeypatch, snap=_fail_snap(0))
    wd3._check_voice_outage(now=2_000_000.0)
    assert bus3.events == []


def test_recovery_not_sent_if_never_alerted(monkeypatch):
    wd, bus = _wd(monkeypatch, snap=_fail_snap(5, ok=5))
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []


# ── 告警 / 重提节流 / 恢复时序 ─────────────────────────────────────


def test_alert_then_remind_throttle_then_recover(monkeypatch):
    snap = _fail_snap(4, last_ok=2_000_000.0 - 121.5 * 3600)
    wd, bus = _wd(monkeypatch, snap=snap)
    t0 = 2_000_000.0

    wd._check_voice_outage(now=t0)                      # 达阈值 → 首提
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "voice_outage_alert"
    assert data["attempts"] == 4 and data["window_hours"] == 24
    assert data["consecutive_fails"] == 4
    assert data["reminder"] is False
    assert data["rate_key"] == "voice_outage:remind"
    assert data["last_ok_hours"] == pytest.approx(121.5, abs=0.1)
    assert data["top_reasons"]                          # 有原因分布
    assert wd.total_voice_outage_alerts == 1

    wd._check_voice_outage(now=t0 + 60 * 60)            # < interval → 不重提
    assert len(bus.events) == 1

    wd._check_voice_outage(now=t0 + 241 * 60)           # ≥ 240min → 重提
    assert len(bus.events) == 2
    assert bus.events[-1][1]["reminder"] is True

    snap["ok_24h"] = 1                                  # 正面证据 → 恢复通知
    wd._check_voice_outage(now=t0 + 300 * 60)
    assert bus.events[-1][1].get("recovered") is True
    assert bus.events[-1][1]["rate_key"] == "voice_outage:recovered"
    assert wd._vo_alerted is False and wd._vo_last_remind == 0.0

    wd._check_voice_outage(now=t0 + 400 * 60)           # 恢复后healthy → 不再发
    assert len(bus.events) == 3


def test_low_traffic_after_alert_keeps_state_until_positive_evidence(monkeypatch):
    """告警后样本滑出窗口（attempts<min 且仍 0 成功）≠ 恢复：既不发恢复也不清态。"""
    snap = _fail_snap(4)
    wd, bus = _wd(monkeypatch, snap=snap)
    t0 = 2_000_000.0
    wd._check_voice_outage(now=t0)
    assert len(bus.events) == 1 and wd._vo_alerted is True

    snap["attempts_24h"] = 1                            # 无流量，样本掉出窗口
    snap["ok_24h"] = 0
    wd._check_voice_outage(now=t0 + 10 * 3600)
    assert len(bus.events) == 1                         # 不误报恢复、不无凭据重提
    assert wd._vo_alerted is True

    snap["ok_24h"] = 1                                  # 真成功过 → 恢复
    wd._check_voice_outage(now=t0 + 11 * 3600)
    assert bus.events[-1][1].get("recovered") is True


def test_custom_thresholds_respected(monkeypatch):
    cfg = {"health_watchdog": {"voice_outage_remind": {
        "min_attempts": 5, "interval_min": 30, "window_hours": 6}}}
    wd, bus = _wd(monkeypatch, snap=_fail_snap(4), cfg=cfg)
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []                             # 4 < 5 不告警

    wd2, bus2 = _wd(monkeypatch, snap=_fail_snap(5), cfg=cfg)
    t0 = 2_000_000.0
    wd2._check_voice_outage(now=t0)
    assert len(bus2.events) == 1
    assert bus2.events[0][1]["window_hours"] == 6       # 窗口参数透传
    wd2._check_voice_outage(now=t0 + 20 * 60)
    assert len(bus2.events) == 1                        # < 30min 节流
    wd2._check_voice_outage(now=t0 + 31 * 60)
    assert len(bus2.events) == 2                        # 自定义 interval 生效


def test_top_reasons_capped_at_three(monkeypatch):
    snap = _fail_snap(9)
    snap["fail_reasons"] = {"a": 5, "b": 4, "c": 3, "d": 2, "e": 1}
    wd, bus = _wd(monkeypatch, snap=snap)
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events[0][1]["top_reasons"] == {"a": 5, "b": 4, "c": 3}


def test_end_to_end_with_real_ledger(monkeypatch):
    """真台账 → 真判据：3 连败告警（last_ok_hours=-1），成功一次即恢复。"""
    vo.reset_for_test()
    led = vo.get_voice_outage()
    for r in ("synth_failed", "synth_failed", "edge_rejected"):
        led.record_voice_attempt(False, "aline", r)
    wd, bus = _wd(monkeypatch)                          # 不 stub，用真单例
    wd._check_voice_outage(now=time.time())
    assert len(bus.events) == 1
    data = bus.events[0][1]
    assert data["attempts"] == 3 and data["last_ok_hours"] == -1
    assert data["top_reasons"]["synth_failed"] == 2
    assert data["by_source"]["aline"]["attempts"] == 3

    led.record_voice_attempt(True, "autosend")
    wd._check_voice_outage(now=time.time())
    assert bus.events[-1][1].get("recovered") is True


# ── 接线 / 告警通道登记 ────────────────────────────────────────────


def test_check_is_wired_into_watchdog_tick():
    src = (Path(__file__).parent.parent / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._check_voice_outage()" in src, (
        "巡检没接进 _tick → 永远不会跑（本仓有过同类漏接线）")


def test_ab_line_exits_feed_ledger():
    """埋点接线静态钉：A 线 sender / B 线 voice_autosend 都必须喂 voice_outage。"""
    root = Path(__file__).parent.parent / "src"
    sender = (root / "client" / "sender.py").read_text(encoding="utf-8")
    assert "from src.ai.voice_outage import get_voice_outage" in sender
    assert "record_voice_attempt(True, \"aline\")" in sender.replace("'", '"')
    va = (root / "inbox" / "voice_autosend.py").read_text(encoding="utf-8")
    assert va.count("get_voice_outage().record_voice_attempt") >= 2, (
        "B 线 record_voice_sent / record_voice_fallback 两个单一出口都要埋点")


def test_bline_single_exits_record(monkeypatch):
    """B 线既有单一出口真调用即入账（不改签名的行为验证）。"""
    vo.reset_for_test()
    from src.inbox.voice_autosend import record_voice_fallback, record_voice_sent
    record_voice_sent(1200, synth_meta={"provider": "avatar_clone"})
    record_voice_fallback("7852_unready")
    snap = vo.get_voice_outage().outage_snapshot()
    assert snap["attempts_24h"] == 2 and snap["ok_24h"] == 1
    assert snap["fail_reasons"] == {"7852_unready": 1}
    assert snap["by_source"]["autosend"]["attempts"] == 2


def test_webhook_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert _EVENT_ALIASES["voice_outage"]["types"] == {"voice_outage_alert"}

    t1, x1 = _build_message("voice_outage_alert", {
        "attempts": 4, "window_hours": 24, "consecutive_fails": 4,
        "top_reasons": {"synth_failed": 3}, "last_ok_hours": 121.5})
    assert t1.startswith("🔇") and "语音出站连续失败" in t1
    assert "24h 内 4 次尝试 0 成功" in t1
    # 文案必须给运维指路（常见原因 + 日志关键字）
    assert "TTS failed" in x1 and "音色档" in x1 and "7852" in x1
    assert "synth_failed×3" in x1

    t2, _ = _build_message("voice_outage_alert", {"recovered": True})
    assert t2.startswith("✅") and "恢复" in t2

    t3, x3 = _build_message("voice_outage_alert", {
        "attempts": 3, "reminder": True, "last_ok_hours": -1})
    assert t3.startswith("⏰") and "无成功记录" in x3
