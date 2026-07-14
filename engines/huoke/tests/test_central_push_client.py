# -*- coding: utf-8 -*-
"""L2 Worker push client SDK 单测."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.host import central_push_client as cli


@pytest.fixture
def reset_state(tmp_path, monkeypatch):
    """每测重置单例 + 用 tmp 目录的 retry queue."""
    monkeypatch.setattr(cli, "_DEFAULT_QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setattr(cli, "_retry_store_singleton", None)
    monkeypatch.setattr(cli, "_async_executor", None)
    yield


# ── HTTP mock ────────────────────────────────────────────────────────
class MockHttpServer:
    def __init__(self):
        self.calls = []
        self.fail_next = 0  # 让前 N 次失败 (模拟瞬态故障)
        self._lock = threading.Lock()

    def __call__(self, path, body, timeout=10.0, retries=3):
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("mock-fail-once")
            self.calls.append({"path": path, "body": body})
            # 默认返回常见字段
            if "upsert" in path:
                return {"customer_id": "cust-mock-1"}
            if "events/push" in path:
                return {"event_id": "evt-mock-1"}
            if "chats/push" in path:
                return {"chat_id": "chat-mock-1"}
            if "/initiate" in path:
                return {"handoff_id": "ho-mock-1"}
            if "/accept" in path:
                return {"accepted": True}
            if "/complete" in path:
                return {"completed": True}
            return {}


@pytest.fixture
def mock_http(reset_state, monkeypatch):
    s = MockHttpServer()
    monkeypatch.setattr(cli, "_http_post_json", s)
    return s


# ── deterministic UUID ───────────────────────────────────────────────
def test_compute_customer_id_deterministic():
    """同 (source, id) 总返回同一 UUID, 不同输入返回不同 UUID."""
    a = cli.compute_customer_id("facebook", "fb_001")
    b = cli.compute_customer_id("facebook", "fb_001")
    c = cli.compute_customer_id("facebook", "fb_002")
    d = cli.compute_customer_id("line", "fb_001")
    assert a == b
    assert a != c
    assert a != d
    # UUID format
    import uuid as _uuid
    _uuid.UUID(a)


# ── upsert ────────────────────────────────────────────────────────────
def test_upsert_customer_sync(mock_http):
    cid = cli.upsert_customer(
        canonical_id="fb_001", canonical_source="facebook",
        primary_name="Alice", worker_id="w1",
        fire_and_forget=False,
    )
    assert cid == "cust-mock-1"
    assert len(mock_http.calls) == 1
    assert mock_http.calls[0]["path"] == "/cluster/customers/upsert"
    assert mock_http.calls[0]["body"]["canonical_id"] == "fb_001"
    # body 也带了 worker 算的 customer_id (主控 ON CONFLICT 决定真值)
    assert mock_http.calls[0]["body"]["customer_id"] == \
        cli.compute_customer_id("facebook", "fb_001")


def test_upsert_customer_strips_none_fields(mock_http):
    cli.upsert_customer(
        canonical_id="x", canonical_source="facebook",
        primary_name=None, worker_id="w1",
        fire_and_forget=False,
    )
    body = mock_http.calls[0]["body"]
    assert "primary_name" not in body  # None 被 strip
    assert body["canonical_id"] == "x"


def test_upsert_default_async_returns_deterministic_id(mock_http):
    """默认 fire_and_forget=True, 返回 worker 算的 UUIDv5, 不依赖主控."""
    cid = cli.upsert_customer(
        canonical_id="fnf", canonical_source="facebook",
        worker_id="w1",
    )
    expected = cli.compute_customer_id("facebook", "fnf")
    assert cid == expected
    time.sleep(0.3)
    assert len(mock_http.calls) == 1


def test_upsert_explicit_customer_id_passthrough(mock_http):
    """传了 customer_id 就用调用方给的, 不算 UUIDv5."""
    cid = cli.upsert_customer(
        canonical_id="x", canonical_source="facebook",
        customer_id="00000000-0000-0000-0000-000000001234",
        worker_id="w1",
    )
    assert cid == "00000000-0000-0000-0000-000000001234"
    time.sleep(0.3)
    assert mock_http.calls[0]["body"]["customer_id"] == cid


# ── event ────────────────────────────────────────────────────────────
def test_record_event_default_async(mock_http):
    """record_event 默认 fire_and_forget=True (高频)."""
    res = cli.record_event(
        customer_id="cust-1", event_type="greeting_sent",
        worker_id="w1", device_id="d1",
        meta={"template_id": "jp_v3"},
    )
    assert res is None  # async
    time.sleep(0.3)
    assert len(mock_http.calls) == 1
    assert mock_http.calls[0]["path"] == "/cluster/customers/cust-1/events/push"


def test_record_event_sync(mock_http):
    res = cli.record_event(
        customer_id="cust-1", event_type="x", worker_id="w1",
        fire_and_forget=False,
    )
    assert res == "evt-mock-1"


# ── chat ─────────────────────────────────────────────────────────────
def test_record_chat_default_async(mock_http):
    cli.record_chat(
        customer_id="cust-1", channel="messenger", direction="outgoing",
        content="hi", content_lang="ja", ai_generated=True,
        worker_id="w1",
    )
    time.sleep(0.3)
    assert len(mock_http.calls) == 1
    body = mock_http.calls[0]["body"]
    assert body["content"] == "hi"
    assert body["ai_generated"] is True


# ── handoff ──────────────────────────────────────────────────────────
def test_initiate_handoff(mock_http):
    hid = cli.initiate_handoff(
        customer_id="c1", from_stage="messenger", to_stage="line",
        initiating_worker_id="w1",
        ai_summary="客户 30s 日本女性, 兴趣 料理",
    )
    assert hid == "ho-mock-1"


def test_accept_handoff(mock_http):
    ok = cli.accept_handoff("ho-1", accepted_by_human="seat-007")
    assert ok is True


def test_complete_handoff(mock_http):
    assert cli.complete_handoff("ho-1", "converted") is True


# ── retry queue ──────────────────────────────────────────────────────
def test_retry_queue_enqueue_on_failure(reset_state, monkeypatch):
    """fire_and_forget 失败时 enqueue 本地."""
    def always_fail(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cli, "_http_post_json", always_fail)

    cli.upsert_customer(
        canonical_id="fail", canonical_source="facebook",
        worker_id="w1", fire_and_forget=True,
    )
    time.sleep(0.3)
    assert cli.retry_queue_pending() == 1


def test_retry_queue_drain_on_recovery(reset_state, monkeypatch):
    """先 enqueue, 后恢复 → drain 应成功."""
    failed = MockHttpServer()
    failed.fail_next = 100  # 全失败
    monkeypatch.setattr(cli, "_http_post_json", failed)

    cli.record_event(
        customer_id="c1", event_type="x", worker_id="w1",
        fire_and_forget=True,
    )
    cli.record_chat(
        customer_id="c1", channel="messenger", direction="outgoing",
        content="x", worker_id="w1",
    )
    time.sleep(0.3)
    assert cli.retry_queue_pending() == 2

    # 模拟恢复
    ok_server = MockHttpServer()
    monkeypatch.setattr(cli, "_http_post_json", ok_server)

    drained = cli.drain_retry_queue()
    assert drained == 2
    assert cli.retry_queue_pending() == 0
    assert len(ok_server.calls) == 2


def test_4xx_error_no_retry(reset_state, monkeypatch):
    """4xx 错误 (业务错) sync 不 retry, raise RuntimeError."""
    from urllib.error import HTTPError

    call_count = [0]

    def fail_4xx(*a, **kw):
        call_count[0] += 1
        # 真 _http_post_json 处理 HTTPError, 但此 mock 抛 HTTPError 不会到 4xx 处理
        # (mock 直接替换了整个函数). 这里直接 raise RuntimeError 模拟 4xx 抛.
        raise RuntimeError("central push HTTP 400: invalid canonical_id")

    monkeypatch.setattr(cli, "_http_post_json", fail_4xx)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        cli.upsert_customer(
            canonical_id="bad", canonical_source="facebook",
            worker_id="w1",
            fire_and_forget=False,  # sync 才会 raise
        )
    # 应只调 1 次 (因为 mock 已经替换整 _http_post_json)
    assert call_count[0] == 1
