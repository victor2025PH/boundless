"""platform/replybus 决策回执端点契约测试。

覆盖：①有效回复 → action=draft ②空/失败回复 → action=silent ③status 端点可用
④防双发红线断言——响应体绝不含 sent/delivered/available/fallback 等"已发送"或
本应只由客户端合成的语义字段。全程 mock 掉 generate_persona_reply（不触真实
AI/store/网络），仿 test_care_routes.py 的裸 FastAPI() + register + TestClient 模式。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.routes.replybus_routes import register_replybus_routes

_PATCH_TARGET = "src.web.routes.replybus_routes.generate_persona_reply"

_GOOD_MESSAGE = {
    "platform": "telegram",
    "account": "acct_pool_007",
    "external_id": "tg:987654321",
    "text": "在吗？想了解一下代发",
    "msg_id": "m_10086",
    "session_id": "s_tg_987654321",
    "context_hint": {"lang": "zh", "funnel_stage": "new"},
}


def _client(app_state=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_replybus_routes(app, api_auth=_auth, config_manager=None)
    for k, v in (app_state or {}).items():
        setattr(app.state, k, v)
    return TestClient(app)


# ── ① 有效回复 → draft ──────────────────────────────────────────────

def test_decide_valid_reply_maps_to_draft():
    client = _client()
    fake_result = {
        "ok": True, "reply": "可以的，方便说下您主要发什么品类吗？",
        "reply_lang": "zh", "persona": "sales_amy", "persona_tier": "domain",
        "intent": "sales_inquiry",
    }
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=fake_result)) as mocked:
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})

    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "draft"
    assert body["text"] == fake_result["reply"]
    assert body["persona"] == "sales_amy"
    assert body["reason"] == "sales_inquiry"

    # 取参正确性：chat_key=external_id / last_inbound=text / history=[] /
    # persona_id 取自 context_hint.persona（本例未给 → 应为空串）/
    # conversation_id 取自 session_id。
    _, kwargs = mocked.call_args
    assert kwargs["platform"] == "telegram"
    assert kwargs["chat_key"] == "tg:987654321"
    assert kwargs["last_inbound"] == "在吗？想了解一下代发"
    assert kwargs["history"] == []
    assert kwargs["persona_id"] == ""
    assert kwargs["conversation_id"] == "s_tg_987654321"


def test_decide_persona_id_from_context_hint():
    client = _client()
    msg = dict(_GOOD_MESSAGE, context_hint={"persona": "domain_beauty"})
    fake_result = {"ok": True, "reply": "hi~", "persona": "domain_beauty", "intent": ""}
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=fake_result)) as mocked:
        client.post("/api/replybus/decide", json={"message": msg})
    _, kwargs = mocked.call_args
    assert kwargs["persona_id"] == "domain_beauty"


# ── ② 空/失败回复 → silent ──────────────────────────────────────────

def test_decide_empty_reply_maps_to_silent():
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(
        return_value={"ok": False, "reply": "", "detail": "无可用对话上下文"},
    )):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    body = r.json()
    assert r.status_code == 200
    assert body["action"] == "silent"
    assert "text" not in body
    assert body["reason"] == "无可用对话上下文"


def test_decide_ok_true_but_blank_reply_is_silent():
    """ok=True 但 reply 全是空白字符——防止只信 ok 位不检查内容的疏漏。"""
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value={"ok": True, "reply": "   "})):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    assert r.json()["action"] == "silent"


def test_decide_exception_never_500s_and_falls_back_to_silent():
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(side_effect=RuntimeError("ai backend exploded"))):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "silent"
    assert body["reason"] == "generate_failed"


def test_decide_invalid_envelope_short_circuits_without_calling_ai():
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(
        return_value={"ok": True, "reply": "should not happen"},
    )) as mocked:
        r = client.post("/api/replybus/decide", json={"message": {"platform": "telegram"}})
    body = r.json()
    assert body["action"] == "silent"
    assert body["reason"] == "invalid_envelope"
    mocked.assert_not_called()


def test_decide_missing_message_key():
    client = _client()
    r = client.post("/api/replybus/decide", json={})
    assert r.json() == {"action": "silent", "reason": "missing_message_envelope"}


def test_decide_bad_json_body_does_not_500():
    client = _client()
    r = client.post(
        "/api/replybus/decide",
        content=b"not json{{{",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "silent"


# ── ③ status 端点 ───────────────────────────────────────────────────

def test_status_ready_true_when_ai_client_present():
    client = _client(app_state={"ai_client": SimpleNamespace()})
    r = client.get("/api/replybus/status")
    assert r.status_code == 200
    assert r.json() == {"ready": True}


def test_status_ready_false_when_no_ai_backend():
    client = _client()
    r = client.get("/api/replybus/status")
    assert r.json() == {"ready": False}


def test_status_ready_true_via_skill_manager_ai_client():
    sm = SimpleNamespace(ai_client=object())
    client = _client(app_state={"skill_manager": sm})
    r = client.get("/api/replybus/status")
    assert r.json() == {"ready": True}


# ── ④ 防双发红线：响应体绝不含"已发送"/客户端专属语义字段 ───────────────

_FORBIDDEN_KEYS = ("sent", "delivered", "sent_at", "delivered_at", "available", "fallback")


def test_decide_response_never_contains_sent_semantics_on_draft():
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(
        return_value={"ok": True, "reply": "好呀～", "persona": "domain"},
    )):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    body = r.json()
    for key in _FORBIDDEN_KEYS:
        assert key not in body, f"防双发红线被破坏：响应体不应包含 {key!r} 字段"
    assert body["action"] in ("draft", "silent", "handoff")  # 从不是服务端自造的 send


def test_decide_response_never_contains_sent_semantics_on_silent():
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value={"ok": False, "reply": ""})):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    body = r.json()
    for key in _FORBIDDEN_KEYS:
        assert key not in body


def test_decide_action_is_never_send():
    """当前接线策略：draft-only（见模块 docstring）。回归锁：防止未来无意间改成默认 send。"""
    client = _client()
    with patch(_PATCH_TARGET, new=AsyncMock(return_value={"ok": True, "reply": "任意回复"})):
        r = client.post("/api/replybus/decide", json={"message": _GOOD_MESSAGE})
    assert r.json()["action"] != "send"
