"""客户端错误回传 beacon 门禁：捕获级别/消毒/去重/节流/批量/开关。"""
from __future__ import annotations

import logging

import pytest

from src.utils import telemetry_beacon as tb


@pytest.fixture(autouse=True)
def _reset():
    tb.reset_for_tests()
    yield
    tb.reset_for_tests()


def _mk(config=None, post=None):
    b = tb._Beacon(config or {}, post=post or (lambda u, p: True))
    return b


def test_sanitize_scrubs_user_paths_and_secrets():
    s = tb.sanitize_message(
        r"open C:\Users\alice\AppData failed key=sk-abcdef123456 tok=cx.eyJhbGciOi.sig")
    assert "alice" not in s
    assert "sk-abcdef123456" not in s
    assert "cx.eyJhbGciOi" not in s
    assert "~" in s and "***" in s


def test_emit_captures_error_not_info():
    b = _mk()
    rec_info = logging.LogRecord("x", logging.INFO, "f", 1, "info msg", None, None)
    rec_err = logging.LogRecord("x", logging.ERROR, "f", 1, "boom", None, None)
    # Handler.level=ERROR：logging 框架会过滤 INFO；这里直接验证 handle() 语义
    b.handle(rec_info)
    b.handle(rec_err)
    assert len(b._pending) == 1
    (_, item), = b._pending.items()
    assert item["ev"]["msg"] == "boom"


def test_dedup_same_error_counts_n():
    b = _mk()
    for _ in range(5):
        b.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "same failure", None, None))
    assert len(b._pending) == 1
    item = next(iter(b._pending.values()))
    assert item["n"] == 5


def test_flush_posts_batch_and_counts(monkeypatch):
    sent = []

    def fake_post(url, payload):
        sent.append((url, payload))
        return True

    b = _mk(config={"licensing": {"trial": {"site_url": "https://x.example"}}},
            post=fake_post)
    b.emit(logging.LogRecord("a", logging.ERROR, "f", 1, "e1", None, None))
    b.emit(logging.LogRecord("b", logging.ERROR, "f", 1, "e2", None, None))
    b.emit(logging.LogRecord("b", logging.ERROR, "f", 1, "e2", None, None))
    n = b.flush_pending()
    assert n == 2
    url, payload = sent[0]
    assert url == "https://x.example/api/client-log"
    evs = payload["events"]
    assert len(evs) == 2
    e2 = [e for e in evs if e["msg"] == "e2"][0]
    assert e2["n"] == 2


def test_hourly_cap_drops_overflow():
    b = _mk(post=lambda u, p: True)
    b._hour_sent = tb.MAX_PER_HOUR  # 预算耗尽
    b.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "late", None, None))
    assert b.flush_pending() == 0
    assert not b._pending  # 超预算清积压


def test_install_gated_by_desktop_env(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    assert tb.install_beacon({}) is None
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setattr(
        "src.licensing.machine_bridge.machine_fingerprint", lambda: "AAAA-BBBB-CCCC-DDDD")
    b = tb.install_beacon({}, post=lambda u, p: True)
    assert b is not None
    assert tb.install_beacon({}) is b  # 幂等
    # 显式关闭优先
    tb.reset_for_tests()
    assert tb.install_beacon({"telemetry": {"client_errors": {"enabled": False}}}) is None


def test_own_logs_never_loop():
    b = _mk()
    b.emit(logging.LogRecord(tb.__name__, logging.ERROR, "f", 1, "self", None, None))
    assert not b._pending
