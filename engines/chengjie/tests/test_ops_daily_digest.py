# -*- coding: utf-8 -*-
"""每日运维摘要（2026-09-10 运维群降噪 P1.2）：固定钟点、一天一张、重启不重发。

慢性积压（待审草稿 / 客户在等 / 案例跟进）与算力黄灯改为「内容没变一天提一次」后，
运维仍要有一个固定入口看到「此刻还有什么没处理、开了多久」——就是这张卡。数据面直接
取提醒账本 open_items（各巡检外发时已登记一行人话），不重跑巡检。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.inbox.remind_ledger import RemindLedger


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _at(day: str, hh: int, mm: int) -> float:
    return time.mktime(time.strptime(f"{day} {hh:02d}:{mm:02d}:00", "%Y-%m-%d %H:%M:%S"))


def _wd(monkeypatch, cfg: Dict[str, Any] | None):
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=SimpleNamespace())
    w._config_manager = SimpleNamespace(
        config={"health_watchdog": {"daily_digest": cfg}} if cfg is not None else {})
    w.__dict__["_remind_ledger"] = RemindLedger(None)
    return w, bus


def test_digest_disabled_by_default(monkeypatch):
    w, bus = _wd(monkeypatch, None)
    assert w._maybe_daily_digest(now=_at("2026-09-10", 12, 0)) is None
    assert bus.events == []


def test_digest_fires_once_per_day_after_configured_time(monkeypatch):
    w, bus = _wd(monkeypatch, {"enabled": True, "hour": 9, "minute": 0})
    t0 = _at("2026-09-10", 8, 59)
    # 先让两项巡检处于告警中（模拟各自外发时的登记）
    w._remind.decide("draft_backlog", now=t0 - 30 * 86400, interval_sec=10)
    w._remind.mark_sent("draft_backlog", now=t0 - 30 * 86400,
                        summary="待审草稿 5 条无人处理（最久 30 天）")
    w._remind.decide("lan_gpu:http://192.168.0.173:8001", now=t0 - 16 * 3600, interval_sec=10)
    w._remind.mark_sent("lan_gpu:http://192.168.0.173:8001", now=t0 - 16 * 3600,
                        summary="192.168.0.173:8001 探测失败")
    w._tp_state = {"vision": {"alerted": True}, "chat": {"alerted": False}}

    assert w._maybe_daily_digest(now=t0) is None            # 还没到点
    rep = w._maybe_daily_digest(now=_at("2026-09-10", 9, 5))
    assert rep is not None and bus.events[-1][0] == "ops_digest_report"
    assert rep["day"] == "2026-09-10" and rep["rate_key"] == "ops_digest:2026-09-10"
    labels = [(x["label"], x["summary"]) for x in rep["open"]]
    assert labels == [("待审草稿", "待审草稿 5 条无人处理（最久 30 天）"),
                      ("LAN GPU", "192.168.0.173:8001 探测失败")]
    assert rep["open"][0]["hours"] > 24 * 29 and 16.0 <= rep["open"][1]["hours"] < 17.0
    assert rep["probes"] == {"total": 2, "ok": 1, "bad": ["vision"]}
    # 摘要自己的记账键不会混进「未处理」清单
    assert all(not x["key"].startswith("_") for x in rep["open"])

    # 同一天再来、哪怕是重启后的新实例（同一账本）都不重发
    assert w._maybe_daily_digest(now=_at("2026-09-10", 18, 0)) is None
    w2, bus2 = _wd(monkeypatch, {"enabled": True, "hour": 9, "minute": 0})
    w2.__dict__["_remind_ledger"] = w._remind
    assert w2._maybe_daily_digest(now=_at("2026-09-10", 20, 0)) is None
    assert bus2.events == []
    # 次日到点再发一张
    assert w2._maybe_daily_digest(now=_at("2026-09-11", 9, 0)) is not None
    assert len(bus.events) == 1 and len(bus2.events) == 1


def test_digest_survives_broken_cost_module(monkeypatch):
    """成本段装配失败只丢成本段，摘要照发（纯装配、绝不抛）。"""
    w, bus = _wd(monkeypatch, {"enabled": True, "hour": 0, "minute": 0})
    import src.web.routes.cost_routes as cr
    monkeypatch.setattr(cr, "build_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rep = w._maybe_daily_digest(now=_at("2026-09-12", 1, 0))
    assert rep is not None and rep["cost"] is None and rep["open"] == []
    assert bus.events[-1][0] == "ops_digest_report"


def test_digest_carries_primary_from_overlay(monkeypatch):
    """2026-09-17：每日摘要必须带主链档位一行（数据面），文案层另测 copy gate。"""
    w, bus = _wd(monkeypatch, {"enabled": True, "hour": 0, "minute": 0})
    w._config_manager = SimpleNamespace(config={
        "health_watchdog": {"daily_digest": {"enabled": True, "hour": 0, "minute": 0}},
        "ai": {
            "primary": "local", "primary_lock": "local",
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "fallback": {"enabled": True, "base_url": "http://192.168.0.173:8001/v1",
                         "model": "chatx"},
        },
    })
    w._app = SimpleNamespace(state=SimpleNamespace(
        ai_client=SimpleNamespace(_primary_mode="local", _primary_lock="local")))
    rep = w._maybe_daily_digest(now=_at("2026-09-18", 1, 0))
    assert rep is not None
    assert rep["primary"]["effective"] == "local"
    assert "本地 vLLM chatx" in (rep["primary"] or {}).get("primary_text", "")
    assert bus.events[-1][0] == "ops_digest_report"
