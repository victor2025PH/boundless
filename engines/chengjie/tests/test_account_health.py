# -*- coding: utf-8 -*-
"""账号健康聚合门禁（P1-9 2026-08-29）。

钉住的不变量：
1. 三段各自独立软失败——任何一段的数据源缺席只缺那一段，绝不抛；
2. peek 纪律：kill_switch 单例未初始化 → frozen 段按 fail-open 空表（active=0），
   ops_events 单例未初始化 → safety 段缺失（绝不新建库把零数据报成事实）;
3. 冻结条目带 auto/manual 归类（freeze_source 单一判据）与剩余秒数；
4. metrics 接线静态钉（drafts_routes 装配 account_health 键）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


class _FakeSessions:
    def __init__(self, sessions=None, unhealthy=None, stalled=0):
        self._d = {
            "sessions": sessions or {},
            "unhealthy": unhealthy or [],
            "unhealthy_count": len(unhealthy or []),
            "inbox_stalled_count": stalled,
        }

    def dump(self):
        return dict(self._d)


class _FakeOpsEvents:
    """_safety_window 契约面：_lock + _conn（真 sqlite 内存表）。"""

    def __init__(self, rows=()):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE ops_events (kind TEXT, reason TEXT, ts REAL)")
        for kind, reason, ts in rows:
            self._conn.execute(
                "INSERT INTO ops_events VALUES (?,?,?)", (kind, reason, ts))
        self._conn.commit()


def test_collect_sections_with_sources(monkeypatch):
    from src.ops import account_health as ah
    from src.ops import kill_switch as ks

    class _FakeKs:
        def status(self, *, now=None):
            return [
                {"scope": "account:telegram:a1", "actor": "ban_signal",
                 "reason": "auto_pause:flood_wait",
                 "expires_at": NOW + 1800},
                {"scope": "global", "actor": "admin",
                 "reason": "演练", "expires_at": 0},
            ]

    monkeypatch.setattr(ks, "_singleton", _FakeKs())
    sess = _FakeSessions(
        sessions={"telegram:a1": {}, "whatsapp:w1": {}, "line:l1": {}},
        unhealthy=["whatsapp:w1"], stalled=1)
    oe = _FakeOpsEvents(rows=[
        ("kill_switch_set", "auto_ban:peer_flood", NOW - 3600),
        ("kill_switch_set", "手动排查", NOW - 7200),
        ("kill_switch_clear", "", NOW - 1800),
        ("kill_switch_set", "auto_pause:x", NOW - 30 * 86400),   # 窗外不计
    ])
    out = ah.collect_account_health(
        NOW, ops_events_store=oe, session_health=sess)
    fr = out["frozen"]
    assert fr["active"] == 2 and fr["global_active"] is True
    it0 = fr["items"][0]
    assert it0["kind"] == "auto" and it0["cause"] == "flood_wait"
    assert 0 < it0["left_s"] <= 1800
    assert fr["items"][1]["kind"] == "manual"
    assert fr["items"][1]["left_s"] == 0            # 无期限＝0 不撒谎
    ss = out["sessions"]
    assert ss["total"] == 3 and ss["down"] == 1
    assert ss["down_keys"] == ["whatsapp:w1"] and ss["inbox_stalled"] == 1
    sf = out["safety_7d"]
    assert sf == {"auto_freezes": 1, "manual_freezes": 1, "lifts": 1}


def test_collect_peek_discipline_and_soft_fail(monkeypatch):
    """单例未初始化：frozen 空表（fail-open）、safety 段缺失；坏 session
    对象只缺 sessions 段——函数恒返回 dict。"""
    from src.ops import account_health as ah
    from src.ops import kill_switch as ks
    from src.ops import ops_events as oe_mod

    monkeypatch.setattr(ks, "_singleton", None)
    monkeypatch.setattr(oe_mod, "peek_ops_event_store", lambda: None)

    class _Boom:
        def dump(self):
            raise RuntimeError("x")

    out = ah.collect_account_health(NOW, session_health=_Boom())
    assert out["frozen"]["active"] == 0            # status_snapshot fail-open []
    assert "sessions" not in out
    assert "safety_7d" not in out
    assert out["generated_at"] == NOW


def test_metrics_wiring_pinned():
    """metrics 装配链必须带 account_health 段（ops 卡的唯一数据源）。"""
    src = (ENGINE_ROOT / "src" / "web" / "routes" / "drafts_routes.py"
           ).read_text(encoding="utf-8")
    assert "collect_account_health" in src
    assert 'metrics["account_health"]' in src


def test_boss_now_carries_frozen_count(monkeypatch):
    """老板页风险灯：kill_switch 生效数进 now.risk.frozen（fail-open=0）。"""
    from src.ops import kill_switch as ks
    from src.web.routes import boss_routes as br

    class _FakeKs:
        def status(self, *, now=None):
            return [{"scope": "platform:telegram", "actor": "a",
                     "reason": "r", "expires_at": 0}]

    monkeypatch.setattr(ks, "_singleton", _FakeKs())
    out = br._build_now(object(), {}, None, now=NOW)
    assert out["risk"]["frozen"] == 1
    monkeypatch.setattr(ks, "_singleton", None)
    out2 = br._build_now(object(), {}, None, now=NOW)
    assert out2["risk"]["frozen"] == 0
