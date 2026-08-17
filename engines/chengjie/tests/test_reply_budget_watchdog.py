# -*- coding: utf-8 -*-
"""回复额度触顶聚合巡检（peer_bot_guard P1，2026-08-12）。

逐会话的 ``bot_peer_alert`` 只在首次拦截各响一次；「多个会话同日触顶」这个
**面**级信号（预算配小/撞 bot 波次）此前无人聚合。本检查与设置页「今日额度
状态」同数据源（``list_reply_budget_today`` × ``budget_flags``），near(≥80%)
计数随 payload 提供提前量但**不触发**告警（预警不该比事故更响）。

与 test_draft_backlog_watchdog 同哲学：重点覆盖**不该响**的路径——
低于阈值 / 守卫关 / 额度 0 / store 缺席 / 重提节流，误报比漏报更快摧毁信任。
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


def _row(cid: str, used: int, *, relieved: bool = False,
         name: str = "") -> Dict[str, Any]:
    return {"conversation_id": cid, "used": used, "relieved": relieved,
            "platform": "telegram", "account_id": "a", "chat_key": cid,
            "display_name": name or cid, "chat_type": "private"}


def _wd(monkeypatch, rows, *, guard=None, remind=None,
        store_present: bool = True):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    store = SimpleNamespace(
        list_reply_budget_today=lambda day, limit=200: list(rows),
    ) if store_present else None
    conf: Dict[str, Any] = {
        "inbox": {"peer_bot_guard": dict(
            {"enabled": True, "daily_reply_budget": 10}, **(guard or {}))},
        "health_watchdog": {"reply_budget_remind": dict(
            {"enabled": True}, **(remind or {}))},
    }
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=SimpleNamespace(inbox_store=store))
    w._config_manager = SimpleNamespace(config=conf)
    w._rb_alerted = False
    w._rb_last_remind = 0.0
    w.total_reply_budget_alerts = 0
    return w, bus


def test_alerts_at_threshold_with_full_payload(monkeypatch):
    """≥min_count 会话触顶 → 一次聚合告警，payload 带 硬停/near/样本。"""
    rows = [
        _row("c1", 25, name="话痨甲"),   # 2.5× → 硬停
        _row("c2", 12, name="话痨乙"),   # 软停
        _row("c3", 10, name="话痨丙"),   # 软停（恰触顶）
        _row("c4", 8),                    # 8/10 = 80% → near
        _row("c5", 3),                    # 正常
        _row("c6", 11, relieved=True),    # 已豁免：不算触顶
    ]
    w, bus = _wd(monkeypatch, rows)
    w._check_reply_budget(now=time.time())

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "reply_budget_alert"
    assert p["exhausted_count"] == 3
    assert p["hard_count"] == 1
    assert p["near_count"] == 1
    assert p["budget_limit"] == 10
    assert [s["title"] for s in p["samples"]] == ["话痨甲", "话痨乙", "话痨丙"]
    assert p["reminder"] is False
    assert w.total_reply_budget_alerts == 1


def test_quiet_below_min_count_and_near_never_triggers(monkeypatch):
    """触顶数低于阈值不响；near 再多也不独立触发（预警不该比事故更响）。"""
    rows = [_row("c1", 12), _row("c2", 15),          # 仅 2 个触顶（默认阈 3）
            _row("n1", 8), _row("n2", 9), _row("n3", 8), _row("n4", 9)]
    w, bus = _wd(monkeypatch, rows)
    w._check_reply_budget(now=time.time())
    assert bus.events == []


def test_reminder_throttled_then_reminds(monkeypatch):
    rows = [_row(f"c{i}", 12) for i in range(3)]
    w, bus = _wd(monkeypatch, rows, remind={"interval_min": 240})
    ts = time.time()
    w._check_reply_budget(now=ts)
    w._check_reply_budget(now=ts + 60)          # 节流窗内：静默
    assert len(bus.events) == 1
    w._check_reply_budget(now=ts + 241 * 60)    # 过窗：重提且标 reminder
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_only_when_exhausted_clears(monkeypatch):
    """恢复通知要等触顶**清零**（全豁免/跨日），降到阈值以下不谎报恢复。"""
    rows = [_row(f"c{i}", 12) for i in range(3)]
    w, bus = _wd(monkeypatch, rows)
    ts = time.time()
    w._check_reply_budget(now=ts)
    assert len(bus.events) == 1
    # 降到 1 个触顶：不响新告警、也不发恢复
    rows.clear()
    rows.extend([_row("c0", 12)])
    w._check_reply_budget(now=ts + 300 * 60)
    assert len(bus.events) == 1
    # 全部豁免 → 触顶清零 → 恢复通知
    rows.clear()
    rows.extend([_row("c0", 12, relieved=True)])
    w._check_reply_budget(now=ts + 301 * 60)
    assert len(bus.events) == 2
    assert bus.events[1][1].get("recovered") is True
    assert w._rb_alerted is False


def test_guard_disabled_resets_silently(monkeypatch):
    """守卫关闭/额度 0 → 静默复位不发恢复——「把守卫关了」≠「处理完了」。"""
    rows = [_row(f"c{i}", 12) for i in range(3)]
    w, bus = _wd(monkeypatch, rows)
    w._check_reply_budget(now=time.time())
    assert len(bus.events) == 1 and w._rb_alerted
    # 运营把守卫关了：状态复位、零新事件（不发 recovered）
    w._config_manager.config["inbox"]["peer_bot_guard"]["enabled"] = False
    w._check_reply_budget(now=time.time() + 600)
    assert len(bus.events) == 1
    assert w._rb_alerted is False
    # 额度 0（YAML 不限额）同语义
    w2, bus2 = _wd(monkeypatch, rows, guard={"daily_reply_budget": 0})
    w2._rb_alerted = True
    w2._check_reply_budget(now=time.time())
    assert bus2.events == [] and w2._rb_alerted is False


def test_quiet_paths_never_alert(monkeypatch):
    """store 缺席 / 旧 store 无列表方法 / 巡检开关关 / 取数抛异常：全静默。"""
    ts = time.time()
    w, bus = _wd(monkeypatch, [], store_present=False)
    w._check_reply_budget(now=ts)
    assert bus.events == []
    # 旧 store（无 list_reply_budget_today）
    w2, bus2 = _wd(monkeypatch, [])
    w2._app.state.inbox_store = SimpleNamespace()
    w2._check_reply_budget(now=ts)
    assert bus2.events == []
    # 巡检开关关
    w3, bus3 = _wd(monkeypatch, [_row(f"c{i}", 12) for i in range(5)],
                   remind={"enabled": False})
    w3._check_reply_budget(now=ts)
    assert bus3.events == []
    # 取数异常：宁静默不误报
    def _boom(day, limit=200):
        raise RuntimeError("db locked")
    w4, bus4 = _wd(monkeypatch, [])
    w4._app.state.inbox_store = SimpleNamespace(list_reply_budget_today=_boom)
    w4._check_reply_budget(now=ts)
    assert bus4.events == []


def test_min_count_override(monkeypatch):
    """min_count=1：单会话触顶即响（小体量部署的合理档）。"""
    w, bus = _wd(monkeypatch, [_row("only", 12)], remind={"min_count": 1})
    w._check_reply_budget(now=time.time())
    assert len(bus.events) == 1
    assert bus.events[0][1]["exhausted_count"] == 1
