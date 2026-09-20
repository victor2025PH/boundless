# -*- coding: utf-8 -*-
"""被埋会话巡检：归档着、却有未读入站 ⇒ 客户在等而工作台看不见（P0-198）。

事故（2026-08-04，198 测试机）：一条 33 条消息的**活跃**会话在坐席聊天途中从工作台
彻底消失。根因＝归档在实现上是永久的——所有默认视图过滤 ``archived=1``，而入站链路
从不复位该标记，于是客户之后无论说多少句话都不回来、**也没有任何信号会响**。

入站自动复活（``InboxStore._unarchive_on_inbound``）堵住了新发生的，但够不着存量
（``archived_at`` 是那次才加的列，存量只能回填「升级时刻」，客户是在那之前开口的）。
本检查用一条**与时间戳无关**的信号兜住：未读只可能由入站消息产生，而「没人读过」
本身就是「没人看得见」的直接证据。

它同时是复活链路的**反向哨兵**——复活正常工作时这清单应恒为空，一旦断了这里就响。
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


def _row(cid: str, unread: int, *, age_h: float = 1.0, auto: bool = False) -> Dict[str, Any]:
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "acctA",
        "chat_key": cid, "display_name": "ds Lao", "unread": unread,
        "last_ts": time.time() - age_h * 3600.0, "last_text": "在吗",
        "archived_at": time.time() - age_h * 3600.0 - 60,
        "auto_archived_at": (time.time() - 100) if auto else 0.0,
    }


def _wd(monkeypatch, rows, cfg_extra=None, *, store_present: bool = True,
        legacy_store: bool = False, raises: bool = False):
    """``legacy_store``：旧版 store 没有 list_buried_archived（升级前的实例）。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _list(**k):
        if raises:
            raise RuntimeError("db locked")
        n = int(k.get("min_unread") or 1)
        return [r for r in rows if int(r.get("unread") or 0) >= n]

    if not store_present:
        store = None
    elif legacy_store:
        store = SimpleNamespace()
    else:
        store = SimpleNamespace(list_buried_archived=_list)
    state = SimpleNamespace(inbox_store=store)
    conf: Dict[str, Any] = {"health_watchdog": {"buried_conv_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["buried_conv_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = SimpleNamespace(config=conf)
    w._bc_alerted = False
    w._bc_last_remind = 0.0
    w.total_buried_conv_alerts = 0
    return w, bus


# ── 触发 ─────────────────────────────────────────────────────────────────

def test_alerts_on_single_buried_conversation(monkeypatch):
    """被埋**一条**就该报：那是一个真实客户在等，不存在「正常队列深度」可言。

    这与 draft_backlog（min_count=3，日常队列有波动）的取舍刻意不同。
    """
    w, bus = _wd(monkeypatch, [_row("telegram:acctA:c1", 3)])
    w._check_buried_conversations(now=time.time())

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "buried_conv_alert"
    assert p["buried_count"] == 1
    assert p["total_unread"] == 3
    assert p["reminder"] is False
    assert w.total_buried_conv_alerts == 1


def test_separates_manual_from_auto_archived(monkeypatch):
    """人工/自动必须分开报——处置完全不同。

    人工＝去问那个人是不是误操作（事故当天正是 hover 快捷键误触）；
    自动＝策略把活跃会话判死了，该调 idle_hours 或直接关掉。合成一个数字
    会让运维不知道该动哪里。
    """
    rows = [_row("c1", 2), _row("c2", 1, auto=True), _row("c3", 5, auto=True)]
    w, bus = _wd(monkeypatch, rows)
    w._check_buried_conversations(now=time.time())

    _, p = bus.events[0]
    assert p["buried_count"] == 3
    assert p["manual_archived"] == 1
    assert p["auto_archived"] == 2
    assert p["total_unread"] == 8


def test_reports_oldest_activity_hours(monkeypatch):
    """「最近活动距今多久」决定紧急度：刚被埋 vs 埋了一周，处置优先级不同。"""
    w, bus = _wd(monkeypatch, [_row("c1", 1, age_h=2), _row("c2", 1, age_h=51.5)])
    w._check_buried_conversations(now=time.time())

    _, p = bus.events[0]
    assert 51 <= p["oldest_hours"] <= 52


def test_samples_capped_for_payload_size(monkeypatch):
    """样本只带前 5 条：告警是「有事要看」的信号，不是数据导出口。"""
    rows = [_row(f"c{i}", 1) for i in range(12)]
    w, bus = _wd(monkeypatch, rows)
    w._check_buried_conversations(now=time.time())

    _, p = bus.events[0]
    assert p["buried_count"] == 12
    assert len(p["samples"]) == 5


# ── 不该报的（误报面才是告警可信度的命门）────────────────────────────────

def test_silent_when_nothing_buried(monkeypatch):
    """清单为空＝复活链路在正常工作，必须彻底静默（含不发恢复通知）。"""
    w, bus = _wd(monkeypatch, [])
    w._check_buried_conversations(now=time.time())
    assert bus.events == []


def test_silent_when_disabled(monkeypatch):
    w, bus = _wd(monkeypatch, [_row("c1", 9)], {"enabled": False})
    w._check_buried_conversations(now=time.time())
    assert bus.events == []


def test_silent_on_legacy_store_without_capability(monkeypatch):
    """旧版 store 没这个方法 → 静默，不能因为「查不了」就装作没事或报错刷屏。"""
    w, bus = _wd(monkeypatch, [_row("c1", 9)], legacy_store=True)
    w._check_buried_conversations(now=time.time())
    assert bus.events == []


def test_silent_when_no_store(monkeypatch):
    w, bus = _wd(monkeypatch, [_row("c1", 9)], store_present=False)
    w._check_buried_conversations(now=time.time())
    assert bus.events == []


def test_query_failure_is_swallowed(monkeypatch):
    """取数异常不得把巡检整条链打断（同 tick 还有别的检查要跑）。"""
    w, bus = _wd(monkeypatch, [], raises=True)
    w._check_buried_conversations(now=time.time())
    assert bus.events == []


def test_min_unread_threshold_filters(monkeypatch):
    """min_unread 调高＝只报「客户催了好几句」的，阈值必须真的传进 store。"""
    rows = [_row("c1", 1), _row("c2", 4)]
    w, bus = _wd(monkeypatch, rows, {"min_unread": 3})
    w._check_buried_conversations(now=time.time())

    _, p = bus.events[0]
    assert p["buried_count"] == 1 and p["total_unread"] == 4


# ── 节流与恢复 ───────────────────────────────────────────────────────────

def test_throttles_reminder_then_marks_it(monkeypatch):
    """重提有间隔；再次发出时必须标 reminder=True（文案据此换 ⏰ 前缀）。"""
    w, bus = _wd(monkeypatch, [_row("c1", 2)], {"interval_min": 240})
    t0 = time.time()
    w._check_buried_conversations(now=t0)
    w._check_buried_conversations(now=t0 + 3600)      # 1h 内不重提
    assert len(bus.events) == 1

    w._check_buried_conversations(now=t0 + 4 * 3600 + 10)
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_only_after_full_clear(monkeypatch):
    """恢复通知的诚实前提＝真清空。部分处置还发「已恢复」＝谎报。"""
    rows = [_row("c1", 2), _row("c2", 1)]
    w, bus = _wd(monkeypatch, rows)
    t0 = time.time()
    w._check_buried_conversations(now=t0)
    assert len(bus.events) == 1

    rows.pop()                                        # 只处置了一条
    w._check_buried_conversations(now=t0 + 10)
    assert len(bus.events) == 1, "还剩一条被埋，不该报恢复"

    rows.clear()
    w._check_buried_conversations(now=t0 + 20)
    assert len(bus.events) == 2
    assert bus.events[1][1]["recovered"] is True
    assert w._bc_alerted is False


def test_no_recovery_without_prior_alert(monkeypatch):
    """从没报过就别发恢复——凭空一句「已恢复」比不发更让人困惑。"""
    w, bus = _wd(monkeypatch, [])
    w._check_buried_conversations(now=time.time())
    w._check_buried_conversations(now=time.time() + 60)
    assert bus.events == []
