"""本地主链保险门禁（2026-08-15，算力本地化配套）。

背景：主对话链切 ``ai.primary=local_only``（173:8001 vLLM）后，该档语义是
「本地失败绝不回落云端」——vLLM 猝死（当晚实锤：WSL VM 被空闲回收拦腰打断
装载）意味着全站只剩 canned 占位句，且中枢执行器自己挂掉时无人救。
本保险是与中枢 176 顺序契约配套的我方半边：**单向只降不升**——连续探测失败
→ 热切 cloud 保服务；恢复只发通知，切回归执行器。

与 test_human_deliver_watchdog 同哲学：重点覆盖「不该动作」的路径（误切=
把隐私档白白让位），以及触发/恢复的时序与 fail-open。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.inbox.health_watchdog import HealthWatchdog  # noqa: E402


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, etype, data):
        self.events.append((etype, data))


class _CM:
    def __init__(self, cfg, *, overlay_ok=True):
        self.config = cfg
        self.writes: list = []
        self._overlay_ok = overlay_ok

    def set_overlay_flag(self, key, val):
        if isinstance(self._overlay_ok, Exception):
            raise self._overlay_ok
        if not self._overlay_ok:
            return False, "disk full"
        self.writes.append((key, val))
        # 模拟真实语义：写盘后热重载会让 config 反映新值
        self.config.setdefault("ai", {})["primary"] = val
        return True, ""


def _cfg(primary="local_only", base_url="http://192.168.0.173:8001/v1", guard=None, lock=None):
    ai = {"primary": primary, "fallback": {"base_url": base_url}}
    if lock is not None:
        ai["primary_lock"] = lock
    return {
        "ai": ai,
        "health_watchdog": {"ai_primary_guard": guard if guard is not None else {
            "fail_streak": 2, "min_span_sec": 240}},
    }


def _wd(monkeypatch, cfg, probe_seq, *, overlay_ok=True):
    bus = _Bus()
    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)
    cm = _CM(cfg, overlay_ok=overlay_ok)
    wd = HealthWatchdog.__new__(HealthWatchdog)  # 绕开重依赖 __init__
    wd._app = object()
    wd._config_manager = cm
    wd._apg_fail_count = 0
    wd._apg_first_fail_ts = 0.0
    wd._apg_switched = False
    wd._apg_locked_alerted = False
    wd.total_ai_primary_guard_switches = 0
    seq = list(probe_seq)

    def _probe(base, to):
        return seq.pop(0) if seq else False

    wd._probe_local_primary = _probe
    return wd, cm, bus


# ── 不该动作的路径（保险最重要的性质是零误切）────────────────────

def test_cloud_mode_never_probes_nor_writes(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(primary="cloud"), probe_seq=[])
    wd._check_ai_primary_guard(now=1000.0)
    assert cm.writes == [] and bus.events == []
    # cloud 档连探针都不该被消费（未降级态）——probe_seq 空也不抛即证明未调用


def test_healthy_probe_resets_counters(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(), probe_seq=[True])
    wd._apg_fail_count = 1
    wd._apg_first_fail_ts = 500.0
    wd._check_ai_primary_guard(now=1000.0)
    assert wd._apg_fail_count == 0 and wd._apg_first_fail_ts == 0.0
    assert cm.writes == [] and bus.events == []


def test_double_tap_second_probe_ok_counts_as_healthy(monkeypatch):
    # 单次 TCP 抖动：首探 False、复核 True → 不计失败
    wd, cm, bus = _wd(monkeypatch, _cfg(), probe_seq=[False, True])
    wd._check_ai_primary_guard(now=1000.0)
    assert wd._apg_fail_count == 0 and cm.writes == []


def test_below_streak_no_switch(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(), probe_seq=[False, False])
    wd._check_ai_primary_guard(now=1000.0)
    assert wd._apg_fail_count == 1
    assert cm.writes == [] and bus.events == []


def test_streak_met_but_span_short_no_switch(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False])
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1100.0)  # 跨度 100s < 240s
    assert wd._apg_fail_count == 2
    assert cm.writes == [] and bus.events == []


def test_disabled_flag_silences_everything(monkeypatch):
    cfg = _cfg(guard={"enabled": False})
    wd, cm, bus = _wd(monkeypatch, cfg, probe_seq=[])
    wd._check_ai_primary_guard(now=1000.0)
    assert cm.writes == [] and bus.events == []


def test_no_base_url_stays_silent(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(base_url=""), probe_seq=[])
    wd._check_ai_primary_guard(now=1000.0)
    assert cm.writes == [] and bus.events == []


# ── 触发路径 ─────────────────────────────────────────────────────

def test_trip_writes_cloud_and_alerts(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False])
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)  # streak=2 且跨度 300s ≥ 240s
    assert cm.writes == [("ai.primary", "cloud")]
    assert wd._apg_switched is True
    assert wd.total_ai_primary_guard_switches == 1
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "ai_primary_guard_alert"
    assert data["from_mode"] == "local_only" and data["fail_count"] == 2
    assert data["rate_key"] == "ai_primary_guard:switched"


def test_after_switch_cloud_mode_idles_until_recovery(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False, False, True])
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)  # 触发，config 变 cloud
    assert cm.config["ai"]["primary"] == "cloud"
    # 下一 tick：cloud 档 + 端点仍死（probe False）→ 不重复告警不再写盘
    wd._check_ai_primary_guard(now=1600.0)
    assert len(bus.events) == 1 and cm.writes == [("ai.primary", "cloud")]
    # 端点恢复（probe True）→ 恢复通知一次，switched 清位
    wd._check_ai_primary_guard(now=1900.0)
    assert len(bus.events) == 2
    etype, data = bus.events[1]
    assert etype == "ai_primary_guard_alert" and data.get("recovered") is True
    assert wd._apg_switched is False
    # 再一 tick：不再重发恢复通知
    wd._check_ai_primary_guard(now=2200.0)
    assert len(bus.events) == 2


def test_one_way_semantics_never_writes_local(monkeypatch):
    # 全程任何路径都不允许出现「写回 local*」的动作
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False, True, True])
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)
    wd._check_ai_primary_guard(now=1600.0)
    wd._check_ai_primary_guard(now=1900.0)
    assert all(v == "cloud" for _, v in cm.writes)


# ── 老板锁在本地档（2026-09-17 R88：lock=local）：只报不切 ─────────────
# 锁=local 时写 ai.primary=cloud 会在 AIClient 装载点被锁打回并再发「越权改档」
# 告警——运维群看到「已热切 cloud → 锁强制纠正」成对刷屏，误以为主链仍在 cloud 体制。

def test_lock_local_never_writes_cloud_only_alerts_once(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(primary="local", lock="local"),
                      probe_seq=[False] * 8)
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)   # 达阈值
    assert cm.writes == []                   # 绝不写 cloud
    assert wd._apg_switched is False and wd.total_ai_primary_guard_switches == 0
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "ai_primary_guard_alert"
    assert data["kind"] == "probe_fail_locked"
    assert data["lock"] == "local" and data["from_mode"] == "local"
    assert data["fail_count"] == 2 and data["rate_key"] == "ai_primary_guard:probe_fail_locked"
    # 端点持续死：不重复刷同一告警
    wd._check_ai_primary_guard(now=1600.0)
    wd._check_ai_primary_guard(now=1900.0)
    assert len(bus.events) == 1 and cm.writes == []


def test_lock_local_recovery_notice_once_then_silent(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(primary="local_only", lock="local_only"),
                      probe_seq=[False, False, False, False, True, True])
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)
    assert bus.events[-1][1]["kind"] == "probe_fail_locked"
    wd._check_ai_primary_guard(now=1600.0)   # 端点回来
    assert len(bus.events) == 2
    etype, data = bus.events[1]
    assert data.get("recovered") is True and data["kind"] == "probe_recovered_locked"
    assert data["lock"] == "local_only" and data["effective"] == "local_only"
    assert wd._apg_locked_alerted is False
    wd._check_ai_primary_guard(now=1900.0)   # 再一 tick 不重发
    assert len(bus.events) == 2 and cm.writes == []


def test_lock_cloud_or_unlocked_keeps_legacy_switch(monkeypatch):
    # 锁=cloud（08-22 体制）/ 未设锁：保险仍单向热切 cloud，事件带 lock 字段供文案渲染
    for lock, expect in ((None, None), ("cloud", "cloud")):
        wd, cm, bus = _wd(monkeypatch, _cfg(primary="local", lock=lock),
                          probe_seq=[False] * 4)
        wd._check_ai_primary_guard(now=1000.0)
        wd._check_ai_primary_guard(now=1300.0)
        assert cm.writes == [("ai.primary", "cloud")]
        assert bus.events[0][1].get("kind") is None
        assert bus.events[0][1].get("lock") == expect


# ── fail-open ────────────────────────────────────────────────────

def test_overlay_write_exception_no_event_and_retryable(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False],
                      overlay_ok=RuntimeError("boom"))
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)
    assert bus.events == [] and wd._apg_switched is False
    # 失败计数保留（下一轮达标可重试），绝不因写盘异常清状态装没事
    assert wd._apg_fail_count == 2


def test_overlay_write_refused_no_event(monkeypatch):
    wd, cm, bus = _wd(monkeypatch, _cfg(),
                      probe_seq=[False, False, False, False],
                      overlay_ok=False)
    wd._check_ai_primary_guard(now=1000.0)
    wd._check_ai_primary_guard(now=1300.0)
    assert bus.events == [] and wd._apg_switched is False
