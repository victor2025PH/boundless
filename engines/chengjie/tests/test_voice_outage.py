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
    # by_source 逐链带 fail_reasons（单链断档告警的原因来源；全局 fail_reasons
    # 是合并口径，逐链报错时混着别链的原因等于把运维引到错误的机器上）
    assert snap["by_source"] == {
        "aline": {"attempts": 1, "ok": 0,
                  "fail_reasons": {"synth_failed": 1}},
        "autosend": {"attempts": 2, "ok": 1,
                     "fail_reasons": {"unknown": 1}},
    }
    assert snap["last_ok_ts"] == 1_000_000.0 + 27 * 3600
    assert snap["last_fail_ts"] == 1_000_000.0 + 26 * 3600
    # 低流量（deque 远未满）不得声称缩窗
    assert snap["truncated"] is False
    assert snap["effective_window_hours"] == 24.0
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
    # 断言「有界」这个性质，别把上限硬编成数字——2026-08-22 接进 wa_rpa/mr_rpa
    # 后上限从 200 提到 600（RPA 轮询链量级更大），硬编的断言当场变成假红。
    cap = led._MAXLEN
    for _ in range(cap + 50):
        led.record_voice_attempt(False, "aline", "r" * 500)
    snap = led.outage_snapshot(window_hours=24)
    assert snap["attempts_24h"] == cap                      # deque maxlen 兜底
    assert snap["truncated"] is True                        # 满了就得自陈缩窗
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
    wd._vo_src_alerted = {}
    wd._vo_src_last_remind = {}
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


def _mark_ok(snap, n=1, *, source="aline"):
    """把「窗内出现成功」改成**自洽**的一次改动。

    单链臂（2026-08-22）上线后，``ok_24h`` 与 ``by_source[*].ok`` 不再是两个
    可以各改各的旋钮——真台账里前者就是后者之和。只改 ok_24h 会造出「全局有
    成功、却没有任何一条链有成功」这种现实中不存在的快照，于是单链臂正确地
    判定「那条链全灭」并告警，测试却按旧假设数事件数。夹具改一处、两处同步。
    """
    snap["ok_24h"] = n
    src = (snap.setdefault("by_source", {})
           .setdefault(source, {"attempts": n, "ok": 0}))
    src["ok"] = n
    src["attempts"] = max(int(src.get("attempts") or 0), n)
    return snap


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

    _mark_ok(snap)                                      # 正面证据 → 恢复通知
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

    _mark_ok(snap)                                      # 真成功过 → 恢复
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


# ── 单链断档臂（2026-08-22：全局绿灯掩盖单链全灭）────────────────────


def _multi_snap(sources, *, fail_reasons=None):
    """按 by_source 反推全局口径，保证快照自洽（见 _mark_ok 的说明）。"""
    att = sum(int(v.get("attempts") or 0) for v in sources.values())
    ok = sum(int(v.get("ok") or 0) for v in sources.values())
    return {
        "attempts_24h": att, "ok_24h": ok,
        "last_ok_ts": 2_000_000.0 if ok else 0.0,
        "last_fail_ts": 2_000_000.0,
        "consecutive_fails": 0 if ok else att,
        "fail_reasons": fail_reasons or {},
        "by_source": sources,
    }


def test_per_source_alert_fires_while_global_stays_green(monkeypatch):
    """本臂唯一存在理由：Telegram 一切正常，WhatsApp 语音一条都发不出。

    全局臂看「窗内 0 成功」，此处 ok_24h=12，它会直接 return——那盏灯是绿的，
    而 WhatsApp 那边的客户已经完全收不到语音了（2026-08-22 事故的后半段）。
    """
    snap = _multi_snap({
        "aline": {"attempts": 12, "ok": 12},
        "wa_rpa": {"attempts": 5, "ok": 0,
                   "fail_reasons": {"hub_synth_timing_out:index_tts": 4,
                                    "send:share_skip_no_target": 1}},
    })
    wd, bus = _wd(monkeypatch, snap=snap)
    t0 = 2_000_000.0

    wd._check_voice_outage(now=t0)
    assert len(bus.events) == 1, "全局绿灯时单链臂必须仍然开口"
    etype, data = bus.events[0]
    assert etype == "voice_outage_alert"
    assert data["source"] == "wa_rpa" and data["attempts"] == 5
    assert data["reminder"] is False
    assert data["rate_key"] == "voice_outage:src:wa_rpa", (
        "限流键必须按 source 分开，否则多链只有第一条能发声")
    # 原因必须是**这条链自己的**，混进别链的原因会把运维引到错误的机器上
    assert "hub_synth_timing_out:index_tts" in data["top_reasons"]

    wd._check_voice_outage(now=t0 + 60 * 60)             # < interval → 节流
    assert len(bus.events) == 1
    wd._check_voice_outage(now=t0 + 241 * 60)            # ≥ 240min → 重提
    assert len(bus.events) == 2 and bus.events[-1][1]["reminder"] is True

    _mark_ok(snap, 1, source="wa_rpa")                   # 正面证据 → 恢复
    snap["attempts_24h"] = 18
    wd._check_voice_outage(now=t0 + 300 * 60)
    last = bus.events[-1][1]
    assert last.get("recovered") is True and last["source"] == "wa_rpa"
    assert last["rate_key"] == "voice_outage:src:wa_rpa:recovered"
    assert "wa_rpa" not in wd._vo_src_alerted


def test_per_source_quiet_when_global_already_screaming(monkeypatch):
    """所有链都灭 → 全局臂说一次就够，逐链再报 5 遍是刷屏。"""
    snap = _multi_snap({
        "aline": {"attempts": 4, "ok": 0},
        "wa_rpa": {"attempts": 4, "ok": 0},
    })
    wd, bus = _wd(monkeypatch, snap=snap)
    wd._check_voice_outage(now=2_000_000.0)
    assert len(bus.events) == 1
    assert "source" not in bus.events[0][1], "该由全局臂发声"


def test_per_source_quiet_on_thin_samples_and_healthy_chains(monkeypatch):
    """新链刚上线只试过一两次 → 静默（宁可漏报不误报）；健康链永不点名。"""
    snap = _multi_snap({
        "aline": {"attempts": 9, "ok": 9},
        "mr_rpa": {"attempts": 2, "ok": 0},          # 样本不足
        "manual": {"attempts": 6, "ok": 1},          # 有成功
    })
    wd, bus = _wd(monkeypatch, snap=snap)
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []


def test_per_source_recovery_not_sent_if_never_alerted(monkeypatch):
    snap = _multi_snap({"wa_rpa": {"attempts": 8, "ok": 8}})
    wd, bus = _wd(monkeypatch, snap=snap)
    wd._check_voice_outage(now=2_000_000.0)
    assert bus.events == []


def test_per_source_states_are_independent(monkeypatch):
    """两条链各自计时：先响的那条不得把后来的顶掉（共用计时器的经典 bug）。"""
    snap = _multi_snap({
        "aline": {"attempts": 12, "ok": 12},
        "wa_rpa": {"attempts": 4, "ok": 0},
        "mr_rpa": {"attempts": 4, "ok": 0},
    })
    wd, bus = _wd(monkeypatch, snap=snap)
    wd._check_voice_outage(now=2_000_000.0)
    srcs = {d.get("source") for _t, d in bus.events}
    assert srcs == {"wa_rpa", "mr_rpa"}


def test_per_source_ledger_records_reasons_per_chain():
    """真台账：fail_reasons 必须逐链分桶（单链告警的原因来源）。"""
    vo.reset_for_test()
    led = vo.get_voice_outage()
    led.record_voice_attempt(False, "wa_rpa", "hub_synth_timing_out:index_tts")
    led.record_voice_attempt(False, "wa_rpa", "hub_synth_timing_out:index_tts")
    led.record_voice_attempt(False, "mr_rpa", "send:share_intent_failed")
    led.record_voice_attempt(True, "aline")
    snap = led.outage_snapshot()
    by = snap["by_source"]
    assert by["wa_rpa"]["fail_reasons"] == {
        "hub_synth_timing_out:index_tts": 2}
    assert by["mr_rpa"]["fail_reasons"] == {"send:share_intent_failed": 1}
    assert by["aline"].get("fail_reasons") == {}
    # 全局仍是合并口径（两者并存，各有消费方）
    assert snap["fail_reasons"]["hub_synth_timing_out:index_tts"] == 2


def test_note_voice_attempt_never_raises(monkeypatch):
    """一行式入口的全部价值＝调用方不必包 try（它们都在发送热路上）。"""
    def _boom():
        raise RuntimeError("ledger down")

    monkeypatch.setattr(vo, "get_voice_outage", _boom)
    vo.note_voice_attempt(False, "wa_rpa", "x")      # 不得抛
    monkeypatch.undo()
    vo.reset_for_test()
    vo.note_voice_attempt(True, "wa_rpa")
    vo.note_voice_attempt(False, "wa_rpa", "send:share_skip_no_target")
    snap = vo.get_voice_outage().outage_snapshot()
    assert snap["by_source"]["wa_rpa"] == {
        "attempts": 2, "ok": 1,
        "fail_reasons": {"send:share_skip_no_target": 1}}


def test_rpa_voice_chains_feed_ledger():
    """静态接线钉：WhatsApp / Messenger 两条 RPA 语音链必须喂台账。

    2026-08-22 事故的根因之一就是这两条链**压根没接**——hub 引擎被同卡显存挤到
    每发必超时、WhatsApp 全程发不出语音，而台账仍显示 24h「24 次尝试 24 次成功」，
    看门狗/ops 卡/Prometheus 集体沉默，故障最后是靠老板的耳朵发现的。
    """
    root = Path(__file__).parent.parent / "src" / "integrations"
    for rel, source in (("whatsapp_rpa/runner.py", "wa_rpa"),
                        ("messenger_rpa/runner.py", "mr_rpa")):
        src = (root / rel).read_text(encoding="utf-8").replace("'", '"')
        assert "note_voice_attempt" in src, f"{rel} 没接语音断档台账"
        assert f'"{source}"' in src, f"{rel} 的 source 桶名不对"
        # 成败两侧都要记：只记成功＝永远绿灯，只记失败＝永远断档
        assert "_vo(True)" in src and "_vo(False" in src, (
            f"{rel} 必须成败双记")


def test_ops_card_and_alert_name_the_dead_chain():
    """看板/告警必须**点名**是哪条链，否则运维会把真故障当误报关掉。"""
    root = Path(__file__).parent.parent / "src" / "web"
    tpl = (root / "templates" / "ops_overview.html").read_text(
        encoding="utf-8")
    assert "ogDead" in tpl and "ov2_av_outage_chain" in tpl, (
        "单链全灭时汇总数字是绿的，卡片必须另出红字点名")
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        d = get_translations(lang)
        for k in ("ov2_av_outage_chain", "ov2_av_vsrc_wa_rpa",
                  "ov2_av_vsrc_mr_rpa", "ov2_av_vsrc_aline"):
            assert d.get(k), f"{lang} 缺 {k}（前端回落只显裸键名）"

    from src.inbox.webhook_notifier import _build_message
    t, x = _build_message("voice_outage_alert", {
        "source": "wa_rpa", "attempts": 5, "window_hours": 24,
        "top_reasons": {"hub_synth_timing_out:index_tts": 4}})
    assert "WhatsApp" in t, "标题不点名 → 运维查 Telegram 发现好着呢就关掉了"
    assert "全局" in x, "必须说明「全局告警不会响，这条是唯一信号」"
    assert "hub_synth_timing_out:index_tts×4" in x

    t2, _ = _build_message("voice_outage_alert", {
        "source": "mr_rpa", "recovered": True})
    assert t2.startswith("✅") and "Messenger" in t2


def test_prom_exposes_per_source_series():
    """Prometheus 侧必须能表达「一条链全灭而汇总好看」，否则规则写不出来。"""
    vo.reset_for_test()
    led = vo.get_voice_outage()
    for _ in range(12):
        led.record_voice_attempt(True, "aline")
    for _ in range(5):
        led.record_voice_attempt(False, "wa_rpa", "hub_synth_timing_out")
    txt = led.dump_prom()
    assert 'voice_outage_source_attempts_24h{source="wa_rpa"} 5' in txt
    assert 'voice_outage_source_ok_24h{source="wa_rpa"} 0' in txt
    assert 'voice_outage_source_ok_24h{source="aline"} 12' in txt
    # 汇总仍在（两者并存：一个答「整体还行吗」，一个答「哪条链死了」）
    assert "voice_outage_ok_24h 12" in txt
    # 标签消毒：脏 source 不得把整份 exposition 弄成不可解析
    led.record_voice_attempt(False, 'bad"\n{x}', "y")
    txt2 = led.dump_prom()
    assert '"' not in txt2.split("source=", 1)[1].split("}", 1)[0].strip('"')
    for line in txt2.splitlines():
        assert "\\" not in line


def test_snapshot_self_reports_window_truncation(monkeypatch):
    """事件挤满 deque 时必须自陈「我只看得到 Xh」，不许假装 24h。

    为什么这是真缺陷而不是洁癖（2026-08-22 接进 wa_rpa/mr_rpa 时想到的）：
    「窗内 0 成功」这个判据的分母就是 deque 里的事件。deque 一满，早上那条链
    全灭的记录会被下午别的链的成功记录挤出去——于是**告警随流量自动闭嘴**，
    而看板照样写着「24h」。这跟「目录 available:true + /health 200 的绿灯掩盖
    同卡显存吃紧」是同一类失真：每个数字都对，口径在说谎。
    """
    led = vo.VoiceOutageLedger()
    clock = _Clock(2_000_000.0)
    monkeypatch.setattr(vo, "time", clock)

    # 6 小时内均匀灌满 deque：最老一条仍落在 24h 窗内 ⇒ 更老的已被挤掉
    n = led._MAXLEN
    step = 6 * 3600.0 / n
    for i in range(n):
        clock.t = 2_000_000.0 + i * step
        led.record_voice_attempt(False, "wa_rpa", "hub_synth_timing_out")

    now = 2_000_000.0 + 6 * 3600
    snap = led.outage_snapshot(now=now, window_hours=24)
    assert snap["truncated"] is True
    # 名义窗仍按契约回 24（watchdog 阈值语义不变），真实覆盖另开字段说实话
    assert snap["window_hours"] == 24.0
    assert 5.0 <= snap["effective_window_hours"] <= 6.5

    # 窗口收到真实覆盖以内 → 不再算缩窗（最老事件本就在窗外，没有信息丢失）
    snap2 = led.outage_snapshot(now=now, window_hours=1)
    assert snap2["truncated"] is False
    assert snap2["effective_window_hours"] == 1.0


def test_truncated_window_is_disclosed_downstream():
    """缩窗必须一路说到人眼前：告警正文 + ops 卡标签，两处都不许只写 24h。"""
    from src.inbox.webhook_notifier import _build_message
    payload = {
        "attempts": 9, "window_hours": 24, "consecutive_fails": 9,
        "top_reasons": {"hub_synth_timing_out:index_tts": 9},
        "window_truncated": True, "effective_window_hours": 6.2,
    }
    _, x = _build_message("voice_outage_alert", payload)
    assert "6.2h" in x, "缩窗没说 → 运维按 24h 估断档时长，会低估影响面"
    # 单链臂同样要说（它才是多链下唯一会响的那条）
    _, x2 = _build_message(
        "voice_outage_alert", dict(payload, source="wa_rpa"))
    assert "6.2h" in x2
    # 没缩窗时一句都不许多说（噪声本身会训练运维忽略这一行）
    _, x3 = _build_message("voice_outage_alert", dict(
        payload, window_truncated=False))
    assert "6.2h" not in x3 and "窗口提醒" not in x3

    tpl = (Path(__file__).parent.parent / "src" / "web" / "templates"
           / "ops_overview.html").read_text(encoding="utf-8")
    assert "og.truncated" in tpl and "ov2_av_outage_trunc" in tpl, (
        "卡片标签写着「(24h)」而台账只覆盖几小时＝又一盏说谎的绿灯")
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        d = get_translations(lang)
        for k in ("ov2_av_outage_trunc", "ov2_av_outage_trunc_tip"):
            assert d.get(k), f"{lang} 缺 {k}"


# ── 显存归因随告警下发（2026-08-22：最需要根因的那族告警恰好没带根因）──


def _wd_vram(monkeypatch, snap, hosts, *, calls=None):
    wd, bus = _wd(monkeypatch, snap=snap, cfg={"avatar_voice": {"hub_fish": {
        "base_url": "http://hub:9000", "tts_engine": "index_tts"}}})
    import src.ai.avatar_voice as av

    def _fake(base_url="", *, exclude=None, **kw):
        if calls is not None:
            calls.append((base_url, exclude))
        return {"hosts": hosts}

    monkeypatch.setattr(av, "hub_vram_blockers", _fake, raising=False)
    # 服务名解析走 hub 目录（有缓存但测试里不该发网络）：钉成恒等即可，本组测的是
    # 「排掉自己」这条线接上了没有，不是名字映射本身（那有它自己的门禁）。
    monkeypatch.setattr(av, "hub_engine_service_name",
                        lambda base, eng, **kw: str(eng or ""), raising=False)
    return wd, bus


_HOSTS = [{
    "ip": "192.168.0.176", "free_mb": 900,
    "parkable": [{"label": "唱歌工作室", "mem_mb": 5800}],
    "extras_mb": 10240,
}]


def test_hub_starved_alert_carries_vram_attribution(monkeypatch):
    """「引擎在岗但每发必超时」必须自带「谁占着显存」。

    2026-08-22 事故里响的**恰好是这族**告警，而它的正文只写了「看 `GET
    /api/gpu/overview` 找占显存的」——把最关键的一步留给凌晨三点的人自己查。
    引擎离线那族早就带这段归因了，两族本是同一根因（显存不够 → 要么泊掉引擎、
    要么让它带着满卡硬跑）的两种表现，处置动作完全相同。
    """
    calls: list = []
    snap = _fail_snap(6)
    snap["fail_reasons"] = {"hub_synth_timing_out:index_tts": 6}
    wd, bus = _wd_vram(monkeypatch, snap, _HOSTS, calls=calls)

    wd._check_voice_outage(now=2_000_000.0)
    assert len(bus.events) == 1
    _, data = bus.events[0]
    assert data["vram_hosts"] == _HOSTS
    assert calls == [("http://hub:9000", "index_tts")], (
        "归因必须打配置里的 hub（不是硬编码），且**排掉正要救的那个引擎**——"
        "「泊掉 index_tts 腾显存」正是我们要消灭的那次故障")

    from src.inbox.webhook_notifier import _build_message
    _, x = _build_message("voice_outage_alert", data)
    assert "唱歌工作室" in x and "5.7G" in x, "点名可让路的服务"
    assert "10.0G 无主进程" in x, (
        "无主显存常是最大一块且泊车腾不出来——不说的话运维泊完仍不够、线索断了")


def test_non_hub_reasons_do_not_pay_for_attribution(monkeypatch):
    """设备端失败与显存无关：不许为它多打一次 hub GPU 面板。

    这不是省几十毫秒的问题——watchdog 里每一次外部 HTTP 都是一次可能的挂起，
    而 share intent 失败的处置在那台手机上，归因给了也只会误导。
    """
    calls: list = []
    snap = _fail_snap(6)
    snap["fail_reasons"] = {"send:share_skip_no_target": 6}
    wd, bus = _wd_vram(monkeypatch, snap, _HOSTS, calls=calls)

    wd._check_voice_outage(now=2_000_000.0)
    assert len(bus.events) == 1
    assert bus.events[0][1]["vram_hosts"] == []
    assert calls == [], "非 hub 原因不得触发 hub 调用"


def test_per_source_hub_alert_also_carries_attribution(monkeypatch):
    """多链下单链臂常是唯一会响的那条 → 根因也必须给全。"""
    snap = _multi_snap({
        "aline": {"attempts": 9, "ok": 9},
        "wa_rpa": {"attempts": 5, "ok": 0,
                   "fail_reasons": {"hub_synth_timing_out:index_tts": 5}},
    })
    wd, bus = _wd_vram(monkeypatch, snap, _HOSTS)

    wd._check_voice_outage(now=2_000_000.0)
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert data["source"] == "wa_rpa" and data["vram_hosts"] == _HOSTS
    from src.inbox.webhook_notifier import _build_message
    _, x = _build_message(etype, data)
    assert "唱歌工作室" in x


def test_attribution_failure_never_blocks_the_alert(monkeypatch):
    """归因读不到只让告警少一段，绝不能把告警本身弄没了。"""
    snap = _fail_snap(6)
    snap["fail_reasons"] = {"hub_engine_offline:index_tts": 6}
    wd, bus = _wd(monkeypatch, snap=snap, cfg={
        "avatar_voice": {"hub_fish": {"base_url": "http://hub:9000"}}})
    import src.ai.avatar_voice as av

    def _boom(base_url=""):
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(av, "hub_vram_blockers", _boom, raising=False)
    wd._check_voice_outage(now=2_000_000.0)
    assert len(bus.events) == 1
    assert bus.events[0][1]["vram_hosts"] == []
    from src.inbox.webhook_notifier import _build_message
    _, x = _build_message(*bus.events[0])
    assert "available:false" in x, "归因缺失时正文仍须给出原本的处置路径"
    # 归因取不到的时刻恰恰最需要它（hub 打满时这一跳也超时）——留空等于把运维
    # 推回「绿灯但没声音，无从下手」的原点。首版就是这么退化的：自动归因替掉了
    # 原来那句手查指引，而空 hosts 时两句都没有。
    assert "gpu/overview" in x, "取不到归因时必须回落成手查指引，不能一字不说"


def test_device_side_single_chain_alert_omits_vram_section():
    """设备端原因的单链告警不许出现显存段（连手查指引也不该给）。

    单链臂的原因分两族：`hub_` 前缀＝共用合成层（显存归因有用），
    `send:`/`share_skip_` 前缀＝那台手机上的 share intent 失败（显存无关）。
    给后者渲染「去看 GPU 面板」会让运维在正确的告警面前查错的机器。
    """
    from src.inbox.webhook_notifier import _build_message
    _, x = _build_message("voice_outage_alert", {
        "source": "wa_rpa", "attempts": 5, "window_hours": 24,
        "top_reasons": {"send:share_skip_no_target": 5}, "vram_hosts": [],
    })
    assert "显存" not in x and "gpu/overview" not in x
    assert "share_skip_" in x, "设备端排查线索仍须在"
