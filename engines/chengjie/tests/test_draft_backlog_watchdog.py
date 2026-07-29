# -*- coding: utf-8 -*-
"""待审草稿积压巡检：补 SLA 告警的 **L1 盲区**。

2026-07-29 实测生产：待审 7 条、最老 **214h（8.9 天）**，其中 **6 条是 L1**。
而 `SLAWatcher._check_sla_breach` 明确只看 L3/L4（`autopilot_level not in ("L3","L4")
→ continue`），于是三档各有归宿、唯独 L1 掉在缝里：

    L2（auto_ai+low）  → worker 自动发，没人管也发得出去
    L3/L4（med/high）  → 有逐条 SLA 告警
    **L1（review+low）→ 既不自动发、也无任何告警 ⇒ 无声烂掉**

偏偏 L1 是唯一「必须人来处理」的那一档——告警洞正好开在最需要人的地方。
本检查做**聚合**信号（L1 是低风险日常稿，逐条告警＝噪音；「N 条超 X 小时」才是
运维该看的排班问题），并单独点名「其中多少条不在 SLA 覆盖内」。
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


def _draft(level: str, age_h: float) -> Dict[str, Any]:
    return {"draft_id": f"inbox:{level}:{age_h}", "autopilot_level": level,
            "status": "pending", "created_ts": time.time() - age_h * 3600.0}


def _wd(monkeypatch, drafts, cfg_extra=None, *, svc_present: bool = True,
        replied=None):
    """``replied``：draft_id → bool（或抛异常的可调用），模拟「会话已回过」。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _replied_after(d):
        r = (replied or {}).get(str(d.get("draft_id") or ""))
        if isinstance(r, Exception):
            raise r
        return bool(r)

    svc = SimpleNamespace(
        list_drafts=lambda **k: list(drafts),
        conversation_replied_after=_replied_after,
    ) if svc_present else None
    state = SimpleNamespace(draft_service=svc)
    conf: Dict[str, Any] = {"health_watchdog": {"draft_backlog_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["draft_backlog_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = SimpleNamespace(config=conf)
    w._db_alerted = False
    w._db_last_remind = 0.0
    w.total_draft_backlog_alerts = 0
    return w, bus


def test_alerts_on_stale_l1_backlog_and_names_the_blind_spot(monkeypatch):
    """L1 积压必须报，且要点名「不在 SLA 覆盖内」的条数——那是本检查的存在理由。"""
    drafts = [_draft("L1", 214.1), _draft("L1", 156.5), _draft("L1", 31.2),
              _draft("L3", 167.9), _draft("L1", 2.0)]  # 最后一条新鲜，不算
    w, bus = _wd(monkeypatch, drafts)
    w._check_draft_backlog(now=time.time())

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "draft_backlog_alert"
    assert p["stale_count"] == 4              # 4 条超 24h（新鲜那条排除）
    assert p["by_level"] == {"L1": 3, "L3": 1}
    assert p["sla_uncovered"] == 3            # 3 条 L1 不在逐条 SLA 覆盖内
    assert p["oldest_hours"] > 214            # 最老稿龄如实上报
    assert p["reminder"] is False


def test_quiet_below_min_count(monkeypatch):
    """少量超龄不吵（默认 ≥3 条才算积压，避免把日常波动当事故）。"""
    w, bus = _wd(monkeypatch, [_draft("L1", 99.0), _draft("L1", 88.0)])
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_quiet_when_all_fresh(monkeypatch):
    w, bus = _wd(monkeypatch, [_draft("L1", 1.0), _draft("L1", 2.0), _draft("L1", 3.0)])
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_reminder_throttled_then_repeats(monkeypatch):
    """首提后按 interval_min 重提，不到点不吵。"""
    drafts = [_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts, {"interval_min": 240})
    t0 = time.time()
    w._check_draft_backlog(now=t0)
    w._check_draft_backlog(now=t0 + 60 * 60)          # 1h 后：不到 4h 不重提
    assert len(bus.events) == 1
    w._check_draft_backlog(now=t0 + 4 * 3600 + 10)    # 过 4h：重提
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_notice_when_queue_cleared(monkeypatch):
    """清空后补一条恢复通知，并清零状态（否则下次积压不会再首提）。"""
    drafts = [_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._check_draft_backlog(now=time.time())
    assert w._db_alerted is True

    w._app.draft_service = SimpleNamespace(list_drafts=lambda **k: [])
    w._check_draft_backlog(now=time.time())
    assert bus.events[-1][1].get("recovered") is True
    assert w._db_alerted is False


def test_partial_drain_does_not_send_recovery(monkeypatch):
    """降到阈值以下但**没清空** → 不发恢复（否则会谎报「已处理完」）。"""
    w, bus = _wd(monkeypatch, [_draft("L1", 99.0) for _ in range(4)])
    w._check_draft_backlog(now=time.time())
    w._app.draft_service = SimpleNamespace(list_drafts=lambda **k: [_draft("L1", 99.0)])
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    assert w._db_alerted is True


def test_disabled_by_config(monkeypatch):
    w, bus = _wd(monkeypatch, [_draft("L1", 99.0) for _ in range(4)],
                 {"enabled": False})
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_silent_without_draft_service(monkeypatch):
    """拿不到 draft_service（未挂载）→ 静默，不猜。"""
    w, bus = _wd(monkeypatch, [], svc_present=False)
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_store_failure_is_swallowed(monkeypatch):
    """取数异常不得把巡检 tick 搞崩（其余检查还要跑）。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _boom(**k):
        raise RuntimeError("db down")

    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(draft_service=SimpleNamespace(list_drafts=_boom))
    w._config_manager = SimpleNamespace(
        config={"health_watchdog": {"draft_backlog_remind": {"enabled": True}}})
    w._db_alerted = False
    w._db_last_remind = 0.0
    w.total_draft_backlog_alerts = 0
    w._check_draft_backlog(now=time.time())     # 不抛即通过
    assert bus.events == []


def test_missing_created_ts_is_ignored(monkeypatch):
    """无 created_ts 的行不参与判定（宁可漏报不误报）。"""
    rows = [{"draft_id": "x", "autopilot_level": "L1", "created_ts": 0}
            for _ in range(5)]
    w, bus = _wd(monkeypatch, rows)
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_orphan_rows_excluded_from_waiting_count(monkeypatch):
    """账目残留（内容已人工回过、草稿行没人处置）不算「客户在等」。

    坐席常走「采用文案→手动发送」，而发送路由不处置草稿行 → 那行一直 pending。
    把它算进「无人处理」会**虚报**，运维开工作台一看「其实已经回过了」就不再信告警。
    """
    drafts = [_draft("L1", 99.0), _draft("L1", 88.0), _draft("L1", 77.0),
              _draft("L1", 66.0)]
    replied = {drafts[0]["draft_id"]: True, drafts[1]["draft_id"]: True}
    w, bus = _wd(monkeypatch, drafts, {"min_count": 2}, replied=replied)
    w._check_draft_backlog(now=time.time())

    assert len(bus.events) == 1
    p = bus.events[0][1]
    assert p["stale_count"] == 2, "只数客户真的在等的"
    assert p["already_replied"] == 2, "孤儿行单独报，便于清账"
    # 最老稿龄应取「在等」那批（99h/88h 是孤儿 → 最老应为 77h 附近）
    assert 70 < p["oldest_hours"] < 85


def test_all_orphans_means_no_alert(monkeypatch):
    """全是账目残留 → 客户其实没人在等，不该报「无人处理」。"""
    drafts = [_draft("L1", 99.0) for _ in range(4)]
    replied = {d["draft_id"]: True for d in drafts}
    w, bus = _wd(monkeypatch, drafts, replied=replied)
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_reply_probe_failure_counts_as_waiting(monkeypatch):
    """判定异常时按「在等」计——宁可多报不漏报（漏报＝客户一直没人回）。"""
    drafts = [_draft("L1", 99.0) for _ in range(3)]
    replied = {d["draft_id"]: RuntimeError("db") for d in drafts}
    w, bus = _wd(monkeypatch, drafts, replied=replied)
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    assert bus.events[0][1]["stale_count"] == 3


def test_probe_cap_treats_overflow_as_waiting(monkeypatch):
    """超过探测预算的部分保守算在等（不因预算耗尽而漏报）。"""
    drafts = [_draft("L1", 99.0 + i) for i in range(6)]
    replied = {d["draft_id"]: True for d in drafts}   # 全都「已回过」
    w, bus = _wd(monkeypatch, drafts, {"min_count": 2}, replied=replied)
    w._BACKLOG_REPLY_PROBE_CAP = 2                    # 只够查 2 条
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    p = bus.events[0][1]
    assert p["already_replied"] == 2
    assert p["stale_count"] == 4, "预算外的一律算在等"


def test_service_without_probe_method_still_works(monkeypatch):
    """旧 DraftService（无 conversation_replied_after）→ 退化为纯年龄口径，不崩。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    drafts = [_draft("L1", 99.0) for _ in range(4)]
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(
        draft_service=SimpleNamespace(list_drafts=lambda **k: list(drafts)))
    w._config_manager = SimpleNamespace(
        config={"health_watchdog": {"draft_backlog_remind": {"enabled": True}}})
    w._db_alerted = False
    w._db_last_remind = 0.0
    w.total_draft_backlog_alerts = 0
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    assert bus.events[0][1]["stale_count"] == 4
    assert bus.events[0][1]["already_replied"] == 0


def test_watchdog_tick_calls_the_check():
    """接线契约：巡检 tick 里必须真调用本检查（否则写了等于没写）。"""
    import inspect

    src = inspect.getsource(hw.HealthWatchdog)
    assert "_check_draft_backlog()" in src, "tick 未调用 _check_draft_backlog"
