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
    """首提后：内容**没变**（同样那批稿）一天最多重提一次；有新稿加入按 interval_min 重提。

    2026-09-10 运维群降噪：此前同一批 5 条稿每 4h 一张卡连发一个月，收信人直接静音群。
    """
    drafts = [_draft("L1", 99.0 + i) for i in range(4)]
    w, bus = _wd(monkeypatch, drafts, {"interval_min": 240})
    t0 = time.time()
    w._check_draft_backlog(now=t0)
    w._check_draft_backlog(now=t0 + 60 * 60)          # 1h 后：不到 4h 不重提
    assert len(bus.events) == 1
    w._check_draft_backlog(now=t0 + 4 * 3600 + 10)    # 过 4h 但内容未变：不重提
    assert len(bus.events) == 1
    w._check_draft_backlog(now=t0 + 24 * 3600 + 10)   # 满 24h：重提，并标「情况未变」
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True
    assert bus.events[1][1]["unchanged"] is True
    # 有新稿掉进队列 → 内容变了 → 过常规间隔即重提
    drafts.append(_draft("L3", 30.0))
    w._check_draft_backlog(now=t0 + 28 * 3600 + 20)
    assert len(bus.events) == 3
    assert bus.events[2][1]["unchanged"] is False
    assert bus.events[2][1]["stale_count"] == 5


def test_first_seen_is_when_condition_became_true_not_ledger_first_sight(monkeypatch):
    """「已开 X」口径：第 min_count（默认 3）老的那条稿满 24h 的那一刻，而不是账本首次看到的时刻。
    账本 09-10 才上线、积压 8 月就有——否则摘要里 30 天的积压显示「已开 2 分钟」。"""
    drafts = [_draft("L1", 240.0), _draft("L1", 120.0), _draft("L1", 72.0), _draft("L1", 30.0)]
    w, bus = _wd(monkeypatch, drafts)
    t0 = time.time()
    w._check_draft_backlog(now=t0)
    assert len(bus.events) == 1
    since = w._remind.first_seen("draft_backlog")
    # 第 3 老 = 72h 前创建，满 24h 于 48h 前
    assert abs((t0 - since) / 3600.0 - 48.0) < 0.05


def test_reminder_state_survives_restart(monkeypatch, tmp_path):
    """账本落盘：新进程（新实例）接着上一轮的 alerted / last_remind 算，不整轮重发。"""
    drafts = [_draft("L1", 99.0 + i) for i in range(4)]
    w, bus = _wd(monkeypatch, drafts, {"interval_min": 240})
    w._config_manager.config_path = str(tmp_path / "config.yaml")
    del w.__dict__["_remind_ledger"]       # _wd 里 _db_alerted=False 已触发内存账本，换成文件账本
    t0 = time.time()
    w._check_draft_backlog(now=t0)
    assert len(bus.events) == 1
    assert (tmp_path / "health_remind_state.json").exists()
    # 「重启」：同一配置目录起新实例，账本从文件回灌
    w2, bus2 = _wd(monkeypatch, drafts, {"interval_min": 240})
    w2._config_manager.config_path = str(tmp_path / "config.yaml")
    del w2.__dict__["_remind_ledger"]
    w2._check_draft_backlog(now=t0 + 600)
    assert bus2.events == []               # 重启后 10 分钟：不重发
    assert w2._db_alerted is True


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


# ── 工作时间班表感知（2026-08-04）────────────────────────────────
# 班表开启时：休息中账号的积压=刻意扣留（复班补觉会处理），轰人=狼来了；
# 复班后给 work_resume_grace_hours（默认 2h）宽限消化隔夜稿。
# 窗口按「当前时刻 ±偏移」动态构造（UTC 显式时区、抖动 0），与机器时区无关。


def _ws_now_window(offset_start_h: float, offset_end_h: float) -> Dict[str, Any]:
    from datetime import datetime, timedelta
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    return {
        "enabled": True, "timezone": "UTC", "edge_jitter_min": 0,
        "default": {
            "start": (now + timedelta(hours=offset_start_h)).strftime("%H:%M"),
            "end": (now + timedelta(hours=offset_end_h)).strftime("%H:%M"),
        },
    }


def _sched_draft(level: str, age_h: float, account_id: str = "acct1"):
    d = _draft(level, age_h)
    d["platform"] = "telegram"
    d["account_id"] = account_id
    d["draft_id"] = f"{d['draft_id']}:{account_id}"
    return d


def test_off_hours_backlog_is_held_not_alerted(monkeypatch):
    """休息中账号的隔夜积压是刻意扣留 → 不告警（复班后见真章）。"""
    drafts = [_sched_draft("L1", 99.0 + i) for i in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._config_manager.config["inbox"] = {
        "work_schedule": _ws_now_window(2, 4)}   # 此刻休息中
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_off_hours_hold_does_not_fake_recovery(monkeypatch):
    """休息期把积压「藏起来」不等于处理完——绝不谎报恢复。"""
    drafts = [_sched_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._db_alerted = True    # 之前（在班时）已告过警
    w._config_manager.config["inbox"] = {
        "work_schedule": _ws_now_window(2, 4)}
    w._check_draft_backlog(now=time.time())
    assert bus.events == []          # 不重提、也不发 recovered
    assert w._db_alerted is True


def test_resume_grace_holds_overnight_drafts(monkeypatch):
    """复班 1h < 宽限 2h：隔夜稿（created < 班次开始）暂不告警。"""
    drafts = [_sched_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._config_manager.config["inbox"] = {
        "work_schedule": _ws_now_window(-1, 3)}
    w._check_draft_backlog(now=time.time())
    assert bus.events == []


def test_resume_grace_expires_then_alerts(monkeypatch):
    """复班已 3h > 宽限 2h：补觉窗口用完还没消化 → 照常轰人。"""
    drafts = [_sched_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._config_manager.config["inbox"] = {
        "work_schedule": _ws_now_window(-3, 3)}
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    p = bus.events[0][1]
    assert p["stale_count"] == 4
    assert p["off_hours_held"] == 0


def test_resume_grace_zero_disables_hold(monkeypatch):
    drafts = [_sched_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts, {"work_resume_grace_hours": 0})
    w._config_manager.config["inbox"] = {
        "work_schedule": _ws_now_window(-1, 3)}
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1


def test_mixed_accounts_partial_hold(monkeypatch):
    """夜班号（休息中）扣留、常班号（复班已久）照报，payload 点名扣留数。"""
    off = _ws_now_window(2, 4)["default"]
    on = _ws_now_window(-3, 3)["default"]
    drafts = (
        [_sched_draft("L1", 99.0 + i, account_id="resting") for i in range(3)]
        + [_sched_draft("L1", 88.0 + i, account_id="working")
           for i in range(3)])
    w, bus = _wd(monkeypatch, drafts)
    w._config_manager.config["inbox"] = {"work_schedule": {
        "enabled": True, "timezone": "UTC", "edge_jitter_min": 0,
        "default": on,
        "accounts": {"telegram:resting": off},
    }}
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
    p = bus.events[0][1]
    assert p["stale_count"] == 3
    assert p["off_hours_held"] == 3


def test_schedule_disabled_keeps_old_behavior(monkeypatch):
    drafts = [_sched_draft("L1", 99.0) for _ in range(4)]
    w, bus = _wd(monkeypatch, drafts)
    w._config_manager.config["inbox"] = {
        "work_schedule": {**_ws_now_window(2, 4), "enabled": False}}
    w._check_draft_backlog(now=time.time())
    assert len(bus.events) == 1
