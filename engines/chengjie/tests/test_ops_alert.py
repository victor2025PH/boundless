"""实施31：引擎侧运维告警中继 ops_alert + 事件审计 ops_events 单测（纯逻辑，不打真实网络）。"""
from __future__ import annotations

from src.ops import ops_alert
from src.ops import ops_events


def setup_function(_):
    # 每例清空防抖状态 + 用内存库隔离审计
    ops_alert._seen.clear()
    ops_events.reset_ops_event_store()
    ops_events.get_ops_event_store(":memory:")


def test_should_send_debounce():
    assert ops_alert.should_send("banned", "A", now=1000.0) is True
    # 窗内重复 → 抑制
    assert ops_alert.should_send("banned", "A", now=1500.0) is False
    # 超窗 → 放行
    assert ops_alert.should_send("banned", "A", now=1000.0 + 1801) is True
    # 不同账号互不影响
    assert ops_alert.should_send("banned", "B", now=1500.0) is True
    # 不同 kind 互不影响
    assert ops_alert.should_send("paused", "A", now=1500.0) is True


def test_should_send_no_debounce_when_zero():
    assert ops_alert.should_send("k", "A", now=1.0, debounce_sec=0) is True
    assert ops_alert.should_send("k", "A", now=1.0, debounce_sec=0) is True


def test_notify_uses_poster_with_key():
    calls = []
    ok = ops_alert.notify(
        "banned", "hello", account_id="123", key="TESTKEY",
        poster=lambda base, key, text, src: calls.append((base, key, text, src)),
    )
    assert ok is True
    assert len(calls) == 1
    base, key, text, src = calls[0]
    assert key == "TESTKEY"
    assert text == "hello"
    assert base == ops_alert.DEFAULT_BASE  # 未传 base 且无 env → 默认 bd2026.cc


def test_notify_skips_without_key(monkeypatch):
    monkeypatch.delenv("EVENT_INGEST_KEY", raising=False)
    calls = []
    ok = ops_alert.notify(
        "banned", "hello", account_id="123",
        poster=lambda *a: calls.append(a),
    )
    assert ok is False          # 无密钥 → 跳过
    assert calls == []


def test_notify_debounced_second_call_suppressed():
    calls = []
    p = lambda *a: calls.append(a)  # noqa: E731
    assert ops_alert.notify("k", "m1", account_id="X", key="K", poster=p, now=100.0) is True
    assert ops_alert.notify("k", "m2", account_id="X", key="K", poster=p, now=200.0) is False
    assert len(calls) == 1


def test_ban_signal_alert_callback_shape():
    cb = ops_alert.make_ban_signal_alert()
    # make_ban_signal_alert 内部调 notify（无 key 时静默跳过），此处断言不抛 + 审计已落
    cb("account_banned", {"platform": "telegram", "account_id": "999"}, "UserDeactivated")
    cb("account_paused", {"platform": "telegram", "account_id": "999"}, "PeerFlood, 60 分钟")
    store = ops_events.get_ops_event_store()
    assert store.count_since(account_id="999", since_ts=0) == 2


def test_audit_records_even_when_debounced():
    """审计与告警解耦：防抖抑制了 TG，但审计仍全量记。"""
    store = ops_events.get_ops_event_store()
    ops_alert.notify("banned", "m1", account_id="Z", key="K", now=100.0, poster=lambda *a: None)
    # 第二条被防抖抑制（不推 TG），但审计要记
    ok2 = ops_alert.notify("banned", "m2", account_id="Z", key="K", now=200.0, poster=lambda *a: None)
    assert ok2 is False
    assert store.count_since(account_id="Z", kind="banned", since_ts=0) == 2


def test_audit_records_when_no_key(monkeypatch):
    """无密钥不推 TG，但审计仍记（alerted=0）。"""
    monkeypatch.delenv("EVENT_INGEST_KEY", raising=False)
    store = ops_events.get_ops_event_store()
    ok = ops_alert.notify("paused", "m", account_id="Q", poster=lambda *a: None)
    assert ok is False
    rows = store.recent(account_id="Q")
    assert len(rows) == 1 and rows[0]["alerted"] == 0


def test_ops_event_summary():
    import time as _t
    now = _t.time()
    store = ops_events.get_ops_event_store()
    for _ in range(3):
        store.record("paused", account_id="S", ts=now)
    store.record("banned", account_id="S", ts=now)
    summ = store.summary(account_id="S", days=7)
    assert summ["total"] == 4
    assert summ["by_kind"] == {"paused": 3, "banned": 1}
