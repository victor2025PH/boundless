"""人工通过投递链静默断裂巡检门禁（2026-07-29）。

背景：「坐席点『发送』→ 草稿真投递给客户」这条链曾**整条不存在**——只把 DB 标成
approved，全库无消费者，实测 14 天零人工投递、坐席以为发了、客户什么也没收到。
修复靠 bootstrap 注入 ``DraftService.set_inbox_deliver_callback``，但**注入是静默的**：
配置漂移 / 注入抛异常被吞 / 将来重构漏接线，都会让它悄悄退回「只标记不发送」。

本巡检是那条链的被动活体探针。它的核心价值是**零误报**（否则会被当噪声关掉），
因此本门禁重点覆盖三个「不该告警」的前提，以及告警/重提/恢复的时序。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.inbox.health_watchdog import HealthWatchdog  # noqa: E402


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, etype, data):
        self.events.append((etype, data))


class _Worker:
    def __init__(self, *, deliver=True, delivered=0, errors=0) -> None:
        self._snap = {
            "deliver_enabled": deliver,
            "total_human_delivered": delivered,
            "total_human_deliver_errors": errors,
        }

    def status_snapshot(self):
        return dict(self._snap)


class _Store:
    """只实现 list_drafts —— 巡检唯一用到的读口。"""

    def __init__(self, rows) -> None:
        self._rows = rows

    def list_drafts(self, *, source_kind="", status="", limit=50):
        return [r for r in self._rows
                if (not source_kind or r.get("source_kind") == source_kind)
                and (not status or r.get("status") == status)][:limit]


class _State:
    pass


def _wd(monkeypatch, *, worker, rows, cfg=None, since=1000.0):
    bus = _Bus()
    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)

    st = _State()
    st.autosend_worker = worker
    st.inbox_store = _Store(rows)

    class _CM:
        config = cfg if cfg is not None else {}

    wd = HealthWatchdog.__new__(HealthWatchdog)   # 绕开重依赖 __init__
    wd._app = st
    wd._config_manager = _CM()
    wd._hd_since_ts = since
    wd._hd_bad_since = 0.0
    wd._hd_alerted = False
    wd._hd_last_remind = 0.0
    wd.total_human_deliver_alerts = 0
    return wd, bus


def _human_rows(n, *, at=2000.0, by="agent007"):
    return [{"source_kind": "inbox", "status": "approved",
             "decided_at": at, "decided_by": by} for _ in range(n)]


# ── 人工通过计数（纯读）────────────────────────────────────────


def test_human_approved_since_excludes_system_deciders(monkeypatch):
    rows = (
        _human_rows(2, at=2000.0)
        + [{"source_kind": "inbox", "status": "approved",
            "decided_at": 2000.0, "decided_by": d}
           for d in ("autosend_worker", "mode_downgraded", "send_blocked",
                     "stale_peer", "system", "composer_adopt", "")]
        + _human_rows(1, at=500.0)          # 早于窗口起点 → 不计
    )
    wd, _ = _wd(monkeypatch, worker=_Worker(), rows=rows, since=1000.0)
    assert wd.human_approved_since(1000.0) == 2


def test_human_approved_since_survives_broken_store(monkeypatch):
    wd, _ = _wd(monkeypatch, worker=_Worker(), rows=[])
    wd._app.inbox_store = None
    assert wd.human_approved_since(0.0) == 0

    class _Boom:
        def list_drafts(self, **kw):
            raise RuntimeError("db down")

    wd._app.inbox_store = _Boom()
    assert wd.human_approved_since(0.0) == 0     # 绝不抛


# ── 三个「不该告警」的前提（零误报是本巡检的立命之本）──────────


def test_quiet_when_deliver_disabled(monkeypatch):
    """部署刻意只标记不真发（deliver=false）→ 永不告警。"""
    wd, bus = _wd(monkeypatch, worker=_Worker(deliver=False),
                  rows=_human_rows(50))
    for t in (2000.0, 2000.0 + 10 * 3600):
        wd._check_human_deliver_chain(now=t)
    assert bus.events == []


def test_quiet_when_not_enough_human_approvals(monkeypatch):
    """人工通过数不足阈值（默认 3）→ 不构成证据，不告警。"""
    wd, bus = _wd(monkeypatch, worker=_Worker(), rows=_human_rows(2))
    for t in (2000.0, 2000.0 + 10 * 3600):
        wd._check_human_deliver_chain(now=t)
    assert bus.events == []
    assert wd._hd_bad_since == 0.0


def test_quiet_when_chain_proven_alive(monkeypatch):
    """投递成功过、或真尝试过但失败（失败另有告警）→ 本巡检静默。"""
    for w in (_Worker(delivered=1), _Worker(errors=1)):
        wd, bus = _wd(monkeypatch, worker=w, rows=_human_rows(50))
        wd._check_human_deliver_chain(now=2000.0)
        wd._check_human_deliver_chain(now=2000.0 + 10 * 3600)
        assert bus.events == []


def test_quiet_when_worker_absent_or_flag_off(monkeypatch):
    wd, bus = _wd(monkeypatch, worker=_Worker(), rows=_human_rows(50),
                  cfg={"health_watchdog": {"human_deliver_remind": {"enabled": False}}})
    wd._check_human_deliver_chain(now=2000.0 + 10 * 3600)
    assert bus.events == []

    wd2, bus2 = _wd(monkeypatch, worker=_Worker(), rows=_human_rows(50))
    wd2._app.autosend_worker = None
    wd2._check_human_deliver_chain(now=2000.0 + 10 * 3600)
    assert bus2.events == []


# ── 告警 / 重提 / 恢复时序 ─────────────────────────────────────


def test_alert_after_grace_then_remind_then_recover(monkeypatch):
    worker = _Worker(delivered=0)
    wd, bus = _wd(monkeypatch, worker=worker, rows=_human_rows(5))
    t0 = 2000.0

    wd._check_human_deliver_chain(now=t0)                 # 首次观测：只记时刻
    assert bus.events == [] and wd._hd_bad_since == t0

    wd._check_human_deliver_chain(now=t0 + 29 * 60)       # < 30min → 不发
    assert bus.events == []

    wd._check_human_deliver_chain(now=t0 + 31 * 60)       # ≥ 30min → 首提
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "human_deliver_alert"
    assert data["human_approved"] == 5 and data["delivered"] == 0
    assert data["reminder"] is False
    assert data["rate_key"] == "human_deliver:remind"
    assert wd.total_human_deliver_alerts == 1

    wd._check_human_deliver_chain(now=t0 + 60 * 60)       # 距首提 29min → 不重提
    assert len(bus.events) == 1

    wd._check_human_deliver_chain(now=t0 + 31 * 60 + 241 * 60)   # ≥4h → 重提
    assert len(bus.events) == 2
    assert bus.events[-1][1]["reminder"] is True

    worker._snap["total_human_delivered"] = 1             # 链路活了 → 恢复通知
    wd._check_human_deliver_chain(now=t0 + 600 * 60)
    assert bus.events[-1][0] == "human_deliver_alert"
    assert bus.events[-1][1].get("recovered") is True
    assert wd._hd_alerted is False and wd._hd_bad_since == 0.0


def test_recovery_not_sent_if_never_alerted(monkeypatch):
    """没告警过的正常态不该发「恢复」通知（防噪）。"""
    wd, bus = _wd(monkeypatch, worker=_Worker(delivered=3),
                  rows=_human_rows(5))
    wd._check_human_deliver_chain(now=2000.0)
    assert bus.events == []


def test_custom_thresholds_respected(monkeypatch):
    cfg = {"health_watchdog": {"human_deliver_remind": {
        "after_min": 5, "min_approvals": 1}}}
    wd, bus = _wd(monkeypatch, worker=_Worker(), rows=_human_rows(1), cfg=cfg)
    t0 = 2000.0
    wd._check_human_deliver_chain(now=t0)
    wd._check_human_deliver_chain(now=t0 + 4 * 60)
    assert bus.events == []
    wd._check_human_deliver_chain(now=t0 + 6 * 60)
    assert len(bus.events) == 1


# ── 接线 / 告警通道登记 ────────────────────────────────────────


def test_check_is_wired_into_watchdog_tick():
    src = (Path(__file__).parent.parent / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._check_human_deliver_chain()" in src, (
        "巡检没接进 _tick → 永远不会跑（本仓有过同类漏接线）")


def test_webhook_alias_and_message():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert _EVENT_ALIASES["human_deliver"]["types"] == {"human_deliver_alert"}

    t1, x1 = _build_message("human_deliver_alert", {
        "human_approved": 7, "delivered": 0, "bad_minutes": 65})
    assert t1.startswith("🚨") and "1 小时 5 分钟" in t1
    assert "7 条" in x1 and "deliver" in x1

    t2, _ = _build_message("human_deliver_alert", {"recovered": True})
    assert t2.startswith("✅") and "恢复" in t2

    t3, _ = _build_message("human_deliver_alert", {
        "human_approved": 3, "bad_minutes": 30, "reminder": True})
    assert t3.startswith("⏰")


def test_since_ts_is_process_scoped():
    """观测窗起点必须是进程起来的时刻：worker 计数器是进程内的，
    拿持久化的历史通过数去比会在每次重启后瞬间误报。"""
    src = (Path(__file__).parent.parent / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._hd_since_ts: float = time.time()" in src


@pytest.mark.parametrize("bad", [None, {}, {"deliver_enabled": True}])
def test_malformed_worker_snapshot_is_safe(monkeypatch, bad):
    class _W:
        def status_snapshot(self):
            return bad

    wd, bus = _wd(monkeypatch, worker=_W(), rows=_human_rows(50))
    # 缺字段按 0 处理；不得抛，也不得在缺 deliver_enabled 时告警
    wd._check_human_deliver_chain(now=time.time())
    wd._check_human_deliver_chain(now=time.time() + 10 * 3600)
    if not (bad or {}).get("deliver_enabled"):
        assert bus.events == []
