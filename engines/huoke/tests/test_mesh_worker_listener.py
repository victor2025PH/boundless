# -*- coding: utf-8 -*-
"""worker agent_mesh listener (PR-6.6) 单测.

覆盖:
1. listener 启停 / 异常恢复
2. cmd=pause_ai → ai_takeover_state.mark_taken_over
3. cmd=resume_ai → ai_takeover_state.release
4. cmd=manual_reply → facebook.send_message (mock)
5. unknown cmd → ack with error
6. record_human_reply (sent_via_worker=True) → agent_mesh.send_message 触发
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.host import agent_mesh_worker_listener as listener
from src.host import ai_takeover_state


@pytest.fixture(autouse=True)
def reset_state():
    listener.reset_for_tests()
    ai_takeover_state.clear_for_tests()
    yield
    listener.stop_worker_listener(timeout_sec=2.0)
    listener.reset_for_tests()
    ai_takeover_state.clear_for_tests()


# ── pause_ai 命令处理 ────────────────────────────────────────────────
def test_handle_pause_ai_marks_takeover():
    err = listener._handle_pause_ai({
        "device_id": "d1", "peer_name": "Alice",
        "by_username": "agent_zhang", "ttl_sec": 600,
    })
    assert err is None
    assert ai_takeover_state.is_taken_over("Alice", "d1") is True
    info = ai_takeover_state.get_takeover_info("Alice", "d1")
    assert info["by"] == "agent_zhang"


def test_handle_pause_ai_missing_fields_error():
    err = listener._handle_pause_ai({"device_id": "d1"})
    assert err is not None
    assert "必填" in err


# ── resume_ai 命令处理 ───────────────────────────────────────────────
def test_handle_resume_ai_releases():
    ai_takeover_state.mark_taken_over("Alice", "d1", by_username="x")
    err = listener._handle_resume_ai({"device_id": "d1", "peer_name": "Alice"})
    assert err is None
    assert ai_takeover_state.is_taken_over("Alice", "d1") is False


# ── manual_reply (mock facebook) ─────────────────────────────────────
def test_handle_manual_reply_calls_facebook(monkeypatch):
    sent_args = {}

    def fake_send_message(self, target_name, message, device_id=None):
        sent_args["target"] = target_name
        sent_args["msg"] = message
        sent_args["device"] = device_id
        return True

    import src.app_automation.facebook as _fb
    monkeypatch.setattr(_fb.FacebookAutomation, "send_message", fake_send_message)
    # 让 FacebookAutomation 构造时不真起 device manager
    monkeypatch.setattr(_fb.FacebookAutomation, "__init__", lambda self, **kw: None)

    err = listener._handle_manual_reply({
        "device_id": "d1", "peer_name": "Alice", "text": "テスト",
    })
    assert err is None
    assert sent_args == {"target": "Alice", "msg": "テスト", "device": "d1"}


def test_handle_manual_reply_send_returns_false_is_error(monkeypatch):
    import src.app_automation.facebook as _fb
    monkeypatch.setattr(_fb.FacebookAutomation, "send_message",
                        lambda self, target_name, message, device_id=None: False)
    monkeypatch.setattr(_fb.FacebookAutomation, "__init__", lambda self, **kw: None)
    err = listener._handle_manual_reply({
        "device_id": "d1", "peer_name": "Alice", "text": "x",
    })
    assert "False" in err or "Returned" in err.lower() or err is not None


def test_handle_manual_reply_missing_fields():
    err = listener._handle_manual_reply({"device_id": "d1"})
    assert err is not None
    assert "必填" in err


# ── _ListenerThread dispatch ─────────────────────────────────────────
def test_dispatch_routes_to_correct_handler(monkeypatch):
    """dispatch 收到 cmd 后路由到对应 handler + 调 deliver/ack."""
    posts = []

    def fake_post(path, body=None, timeout=8.0):
        posts.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(listener, "_http_post", fake_post)

    t = listener._ListenerThread(worker_id="w-test")

    # pause_ai dispatch
    t._dispatch({
        "id": 100, "payload": {
            "cmd": "pause_ai", "device_id": "d1",
            "peer_name": "Alice", "by_username": "agent_x",
        },
    })
    assert ai_takeover_state.is_taken_over("Alice", "d1") is True
    # 应 deliver + ack 都调用
    assert any("/deliver" in p for p, _ in posts)
    assert any("/ack" in p for p, _ in posts)
    # ack body error 为空 (成功)
    ack_body = next((b for p, b in posts if "/ack" in p), {})
    assert (ack_body.get("error") or "") == ""


def test_dispatch_unknown_cmd_acks_with_error(monkeypatch):
    posts = []
    monkeypatch.setattr(listener, "_http_post",
                        lambda p, b=None, timeout=8.0: posts.append((p, b)) or {"ok": True})

    t = listener._ListenerThread(worker_id="w-test")
    t._dispatch({"id": 200, "payload": {"cmd": "weird_unknown_cmd"}})

    ack_body = next((b for p, b in posts if "/ack" in p), {})
    assert "unknown cmd" in (ack_body.get("error") or "")
    assert t._failed == 1
    assert t._processed == 0


def test_dispatch_handler_exception_acks_with_error(monkeypatch):
    posts = []
    monkeypatch.setattr(listener, "_http_post",
                        lambda p, b=None, timeout=8.0: posts.append((p, b)) or {"ok": True})

    # patch handler to throw
    monkeypatch.setitem(
        listener._COMMAND_HANDLERS, "pause_ai",
        lambda payload: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    t = listener._ListenerThread(worker_id="w-test")
    t._dispatch({"id": 300, "payload": {"cmd": "pause_ai"}})

    ack_body = next((b for p, b in posts if "/ack" in p), {})
    assert "handler exception" in (ack_body.get("error") or "")


# ── start/stop ───────────────────────────────────────────────────────
def test_start_stop_listener_idempotent(monkeypatch):
    monkeypatch.setattr(listener, "_http_get", lambda p, timeout=8.0: {"messages": []})
    monkeypatch.setattr(listener, "_http_post", lambda p, b=None, timeout=8.0: {"ok": True})

    t1 = listener.start_worker_listener(worker_id="w-1", interval_sec=1.0,
                                          startup_delay_sec=0)
    assert t1.is_alive()
    t2 = listener.start_worker_listener(worker_id="w-2", interval_sec=99.0)
    # 重复 start 返回同一实例 (worker_id="w-1" 不变)
    assert t1 is t2

    ok = listener.stop_worker_listener(timeout_sec=2.0)
    assert ok is True


def test_listener_polls_in_background(monkeypatch):
    polled_paths = []

    def fake_get(path, timeout=8.0):
        polled_paths.append(path)
        return {"messages": []}

    monkeypatch.setattr(listener, "_http_get", fake_get)
    monkeypatch.setattr(listener, "_http_post", lambda *a, **kw: {"ok": True})

    listener.start_worker_listener(worker_id="my-worker", interval_sec=0.1,
                                     startup_delay_sec=0)
    time.sleep(0.4)
    listener.stop_worker_listener(timeout_sec=1.0)

    assert len(polled_paths) >= 2
    # path 含 worker_id + message_type=command
    assert all("to_agent=my-worker" in p for p in polled_paths)
    assert all("message_type=command" in p for p in polled_paths)


# ── customer_service.record_human_reply 触发 agent_mesh.send_message ─
def test_record_human_reply_with_send_via_worker(monkeypatch, tmp_path):
    """sent_via_worker=True + 完整 hint → 调 agent_mesh.send_message."""
    import src.host.database as _db
    db_path = tmp_path / "cs_test.db"
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    _db.init_db()

    from src.host.database import _connect
    import uuid as _uuid
    hid = str(_uuid.uuid4())
    with _connect() as c:
        c.execute(
            "INSERT INTO lead_handoffs (handoff_id, canonical_id, source_agent, channel, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (hid, "lead-x", "agent_a", "line", "pending"),
        )
        c.commit()

    from src.host.lead_mesh import customer_service as cs
    cs.assign_to_human(hid, "agent_zhang")

    # mock agent_mesh.send_message
    sent = []
    import src.host.lead_mesh.agent_mesh as _am
    monkeypatch.setattr(_am, "send_message",
                        lambda **kw: sent.append(kw) or "corr-1")

    result = cs.record_human_reply(
        hid, "agent_zhang", "テスト",
        sent_via_worker=True,
        peer_name_hint="Alice", device_id_hint="d1", worker_id_hint="w-test",
    )
    assert result["really_sent"] is True
    assert result["push_error"] is None

    # 验证 send_message 调用了
    assert len(sent) == 1
    kw = sent[0]
    assert kw["to_agent"] == "w-test"
    assert kw["payload"]["cmd"] == "manual_reply"
    assert kw["payload"]["device_id"] == "d1"
    assert kw["payload"]["peer_name"] == "Alice"
    assert kw["payload"]["text"] == "テスト"


def test_record_human_reply_send_via_worker_missing_hints_degrades(
    monkeypatch, tmp_path
):
    """sent_via_worker=True 但 hint 不全 → 降级仅本地记录, 不调 agent_mesh."""
    import src.host.database as _db
    db_path = tmp_path / "cs_degrade.db"
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    _db.init_db()

    from src.host.database import _connect
    import uuid as _uuid
    hid = str(_uuid.uuid4())
    with _connect() as c:
        c.execute(
            "INSERT INTO lead_handoffs (handoff_id, canonical_id, source_agent, channel, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (hid, "lead-y", "agent_a", "line", "pending"),
        )
        c.commit()

    from src.host.lead_mesh import customer_service as cs
    cs.assign_to_human(hid, "agent_zhang")

    sent = []
    import src.host.lead_mesh.agent_mesh as _am
    monkeypatch.setattr(_am, "send_message",
                        lambda **kw: sent.append(kw) or "corr-x")

    # 缺 worker_id
    result = cs.record_human_reply(
        hid, "agent_zhang", "x",
        sent_via_worker=True,
        peer_name_hint="Alice", device_id_hint="d1",
        # 没传 worker_id_hint
    )
    assert result["really_sent"] is False
    assert result["push_error"] is not None
    assert "缺" in result["push_error"]
    assert sent == []  # 真的没调
