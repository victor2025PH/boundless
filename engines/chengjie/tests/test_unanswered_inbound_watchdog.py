# -*- coding: utf-8 -*-
"""入站漏球巡检：补 draft_backlog 的「压根没稿」盲区（P0 2026-08-05）。

实锤：telegram 客户 22:27 连发两条（含「以后给你介绍做你老公」这类高意向
社交信号），整晚零出站、零草稿，次日 07:10 只等来一条不接茬的通用晨安。
现有告警网对这形态全部沉默：

    draft_backlog → 只看「草稿行存在」的积压
    SLA           → 只看 L3/L4 草稿
    **没拟稿**    → 零信号（本巡检的存在理由）

判据要窄（零误报优先）：最后一条是入站 + 悬空 2h..72h + 私聊未归档 +
非 manual 接管 + 无 pending 草稿，全满足才算「漏球」。
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


class _Store:
    def __init__(self, rows, dirs, tags=None) -> None:
        self.rows = rows
        self.dirs = dirs
        self.tags = tags or {}

    def list_conversations(self, limit=400):
        return list(self.rows)

    def last_message_dirs(self, cids):
        return dict(self.dirs)

    def list_conv_tags_map(self, cids):
        return dict(self.tags)


def _conv(cid: str, age_h: float, *, platform="telegram", chat_key="12345",
          chat_type="private", account_id="acct1") -> Dict[str, Any]:
    return {
        "conversation_id": cid, "platform": platform, "account_id": account_id,
        "chat_key": chat_key, "chat_type": chat_type,
        "last_ts": time.time() - age_h * 3600.0,
    }


def _in(*cids):
    return {c: {"direction": "in"} for c in cids}


def _wd(monkeypatch, rows, dirs, *, tags=None, pending=None, cfg_extra=None,
        mode="auto_ai"):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    # 排除项在各自模块单测里守；这里钉成中性值让判据本体可控
    monkeypatch.setattr(
        "src.inbox.peer_bot_guard.proactive_exclude_row", lambda r, c: False)
    monkeypatch.setattr(
        "src.inbox.automation_mode.resolve_automation_mode",
        lambda s, cid, c: mode)
    svc = SimpleNamespace(list_drafts=lambda **k: [
        {"conversation_id": c} for c in (pending or [])])
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(
        inbox_store=_Store(rows, dirs, tags), draft_service=svc)
    conf: Dict[str, Any] = {
        "health_watchdog": {"unanswered_inbound_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["unanswered_inbound_remind"].update(cfg_extra)
    w._config_manager = SimpleNamespace(config=conf)
    w._ui_alerted = False
    w._ui_last_remind = 0.0
    w.total_unanswered_inbound_alerts = 0
    return w, bus


def test_alerts_on_dropped_ball(monkeypatch):
    """最后一条是客户消息、5h 无回复且无草稿 → 必须响，payload 带样例与稿龄。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"))
    w._check_unanswered_inbound(now=time.time())

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "unanswered_inbound_alert"
    assert p["count"] == 1
    assert p["samples"][0]["conversation_id"] == "c1"
    assert 4.5 < p["oldest_hours"] < 5.5
    assert p["reminder"] is False
    assert p["rate_key"] == "unanswered_inbound:remind"
    assert w.total_unanswered_inbound_alerts == 1


def test_quiet_when_last_is_outbound(monkeypatch):
    """球在对方那边（我方已回）→ 静默。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)],
                 {"c1": {"direction": "out"}})
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_quiet_when_pending_draft_exists(monkeypatch):
    """有稿在队 = draft_backlog 的辖区，不双报。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"), pending=["c1"])
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_age_window_excludes_fresh_and_stale(monkeypatch):
    """太新（给拟稿/拟人延迟链留时间）与太旧（回访语义）都不算漏球。"""
    rows = [_conv("fresh", 0.5), _conv("ancient", 100.0)]
    w, bus = _wd(monkeypatch, rows, _in("fresh", "ancient"))
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_manual_mode_excluded(monkeypatch):
    """manual=坐席显式接管，客户在等的是人（工作台未读可见）→ 不进本告警。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"), mode="manual")
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_groups_and_negative_ids_excluded(monkeypatch):
    """群/频道不属「客户在等」：chat_type 白名单 + telegram 负 ID 双兜底。"""
    rows = [
        _conv("g1", 5.0, chat_type="group"),
        _conv("g2", 5.0, chat_type="", chat_key="-1001234"),
    ]
    w, bus = _wd(monkeypatch, rows, _in("g1", "g2"))
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_archived_excluded(monkeypatch):
    """归档会话由 buried_conv 巡检负责（那边连未读都看不见，语义不同）。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"),
                 tags={"c1": {"archived": True}})
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_min_count_threshold(monkeypatch):
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"),
                 cfg_extra={"min_count": 2})
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_reminder_throttled_then_repeats(monkeypatch):
    """首提后按 interval_min 重提，不到点不吵。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"),
                 cfg_extra={"interval_min": 240, "max_age_hours": 500})
    t0 = time.time()
    w._check_unanswered_inbound(now=t0)
    w._check_unanswered_inbound(now=t0 + 3600)          # 1h：不重提
    assert len(bus.events) == 1
    w._check_unanswered_inbound(now=t0 + 4 * 3600 + 10)  # 过 4h：重提
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_notice_when_cleared(monkeypatch):
    """清零补恢复通知并复位状态（否则下次漏球不会再首提）。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"))
    w._check_unanswered_inbound(now=time.time())
    assert w._ui_alerted is True

    w._app.inbox_store = _Store([_conv("c1", 5.0)], {"c1": {"direction": "out"}})
    w._check_unanswered_inbound(now=time.time())
    assert bus.events[-1][1].get("recovered") is True
    assert bus.events[-1][1]["rate_key"] == "unanswered_inbound:recovered"
    assert w._ui_alerted is False


def test_store_failure_is_silent(monkeypatch):
    """取数异常不得把巡检 tick 搞崩，也绝不带着坏数据告警。"""
    w, bus = _wd(monkeypatch, [], {})

    def _boom(limit=400):
        raise RuntimeError("db down")

    w._app.inbox_store.list_conversations = _boom
    w._check_unanswered_inbound(now=time.time())     # 不抛即通过
    assert bus.events == []


def test_dirs_failure_is_silent(monkeypatch):
    """末条方向查不到＝没法判——宁可漏报不误报。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], {})

    def _boom(cids):
        raise RuntimeError("db down")

    w._app.inbox_store.last_message_dirs = _boom
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_disabled_by_config(monkeypatch):
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"),
                 cfg_extra={"enabled": False})
    w._check_unanswered_inbound(now=time.time())
    assert bus.events == []


def test_automation_probe_failure_counts_as_waiting(monkeypatch):
    """automation_mode 判定异常按「在等」计（本机默认档 auto_ai，判不出更可能
    是瞬时故障；与 draft_backlog『宁可多报不漏报』同一取向）。"""
    w, bus = _wd(monkeypatch, [_conv("c1", 5.0)], _in("c1"))
    monkeypatch.setattr(
        "src.inbox.automation_mode.resolve_automation_mode",
        lambda s, cid, c: (_ for _ in ()).throw(RuntimeError("boom")))
    w._check_unanswered_inbound(now=time.time())
    assert len(bus.events) == 1


def test_watchdog_tick_calls_the_check():
    """接线契约：巡检 tick 里必须真调用本检查（否则写了等于没写）。"""
    import inspect

    src = inspect.getsource(hw.HealthWatchdog)
    assert "_check_unanswered_inbound()" in src, "tick 未调用 _check_unanswered_inbound"
