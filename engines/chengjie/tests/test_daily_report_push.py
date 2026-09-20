# -*- coding: utf-8 -*-
"""WP-3 老板日报推送门禁（2026-08-17；实施35 §2.5 rider 的接线验收）。

钉住的不变量：
1. ``daily_report_enabled=false``（缺省）→ 零构建零发送（规格硬验收）；
2. 开启 → 每日一条（节流窗内第二次调用 None；计数只加一）；
3. **零流量日不推空报**（节流戳仍推进——不为空日每个 5min tick 重扫消息表）；
4. 数据面与 /workspace/boss 同源（value_report 日窗）：省时头条 = drafts.sent ×
   系数；value_lines 随报文携带且封顶 6 行；
5. formatter：period=daily → 「运营日报」标题；周报渲染逐字节不变（防串味）。

种子与 test_boss_value 同风格（直插持久表，锚固定 NOW 显式传参，零墙钟依赖）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.inbox.health_watchdog import HealthWatchdog
from src.inbox.store import InboxStore

NOW = 1_800_000_000.0
DAY = 86400.0


def _mk_store(tmp_path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.db")


def _seed_today(store: InboxStore, t0: float = NOW) -> None:
    """近 24h：AI 真发 2 条 + 出站 1 入站 1 + 触达 1。"""
    with store._lock:
        c = store._conn
        for did, created, sent in (("d1", t0 - 0.3 * DAY, t0 - 0.2 * DAY),
                                   ("d2", t0 - 0.6 * DAY, t0 - 0.5 * DAY)):
            c.execute(
                "INSERT INTO reply_drafts (draft_id, conversation_id, source_kind, "
                "source_id, status, autopilot_level, decided_at, sent_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, "conv:a", "inbox", f"s-{did}", "approved", "L1",
                 sent, sent, created, created))
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:a', 'care:x', 'sent', ?)", (t0 - 0.4 * DAY,))
        for mid, direction, ts in (("m1", "out", t0 - 0.2 * DAY),
                                   ("m2", "in", t0 - 0.1 * DAY)):
            c.execute(
                "INSERT INTO messages (message_id, conversation_id, direction, ts, ingested_at) "
                "VALUES (?,?,?,?,?)", (mid, "conv:a", direction, ts, ts))
        c.commit()


def _mk_watchdog(store, *, enabled=True, cfg=None,
                 interval=86400.0) -> HealthWatchdog:
    app = SimpleNamespace(state=SimpleNamespace(inbox_store=store))
    return HealthWatchdog(
        app=app, config_manager=SimpleNamespace(config=cfg or {}),
        daily_report_enabled=enabled, daily_interval_sec=interval)


@pytest.fixture
def bus_spy(monkeypatch):
    published = []

    class _Bus:
        def publish(self, ev, data):
            published.append((ev, data))

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    return published


def test_disabled_is_silent(tmp_path, bus_spy):
    store = _mk_store(tmp_path)
    _seed_today(store)
    hw = _mk_watchdog(store, enabled=False)
    hw._last_daily_ts = NOW - 2 * DAY
    assert hw._maybe_daily_report(now=NOW) is None
    assert bus_spy == []
    assert hw.total_daily_reports == 0


def test_daily_report_once_per_day(tmp_path, bus_spy):
    store = _mk_store(tmp_path)
    _seed_today(store)
    hw = _mk_watchdog(store, cfg={"ops": {"value_report":
                                          {"manual_minutes_per_reply": 6}}})
    # 节流戳窗内（构造语义=启动即置「现在」防重启刷屏；测试用固定 NOW 显式模拟）
    hw._last_daily_ts = NOW - 0.5 * DAY
    assert hw._maybe_daily_report(now=NOW) is None
    # 戳过期 → 发一条
    hw._last_daily_ts = NOW - 1.5 * DAY
    report = hw._maybe_daily_report(now=NOW)
    assert report is not None
    assert report["period"] == "daily" and report["days"] == 1
    # 省时头条：2 条真发 × 6 分钟 = 0.2 小时（数据面与 /workspace/boss 同源系数）
    assert report["headline"] == ["AI 经审核发出 2 条回复，折算省约 0.2 小时人工"]
    assert report["value_lines"] and len(report["value_lines"]) <= 6
    assert [ev for ev, _ in bus_spy] == ["ops_report"]
    assert hw.total_daily_reports == 1
    # 同窗第二次 → 节流 None，不重复发
    assert hw._maybe_daily_report(now=NOW + 60) is None
    assert hw.total_daily_reports == 1


def test_zero_traffic_day_sends_nothing_but_advances_stamp(tmp_path, bus_spy):
    store = _mk_store(tmp_path)          # 空库=零流量
    hw = _mk_watchdog(store)
    hw._last_daily_ts = NOW - 2 * DAY
    assert hw._maybe_daily_report(now=NOW) is None
    assert bus_spy == []
    # 节流戳已推进：零流量日不该每个巡检 tick 重扫消息表
    assert hw._last_daily_ts == NOW
    assert hw._maybe_daily_report(now=NOW + 300) is None


def test_store_missing_is_soft(bus_spy):
    hw = HealthWatchdog(app=SimpleNamespace(state=SimpleNamespace()),
                        daily_report_enabled=True)
    hw._last_daily_ts = NOW - 2 * DAY
    assert hw._maybe_daily_report(now=NOW) is None
    assert bus_spy == []


def test_stats_snapshot_carries_daily_counter(tmp_path):
    store = _mk_store(tmp_path)
    _seed_today(store)
    hw = _mk_watchdog(store)
    hw._last_daily_ts = NOW - 2 * DAY
    hw._maybe_daily_report(now=NOW)
    snap = hw.status_snapshot()
    assert snap.get("total_daily_reports") == 1
    assert "total_weekly_reports" in snap          # 周报计数位不受影响


def test_formatter_daily_title_weekly_unchanged():
    from src.inbox.webhook_notifier import _build_message

    title_d, text_d = _build_message("ops_report", {
        "period": "daily", "days": 1,
        "headline": ["AI 经审核发出 2 条回复，折算省约 0.2 小时人工"],
        "value_lines": ["出站消息 1 条"],
    })
    assert "运营日报" in title_d and "24 小时" in title_d
    assert "0.2 小时人工" in text_d and "出站消息 1 条" in text_d
    # 周报（无 period 标记）逐字节旧标题
    title_w, _ = _build_message("ops_report", {"days": 7, "headline": ["x"]})
    assert title_w == "📰 运营周报（近 7 天）"
