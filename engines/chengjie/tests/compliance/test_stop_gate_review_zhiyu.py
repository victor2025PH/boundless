# -*- coding: utf-8 -*-
"""智语 2026-10-08：STOP 闸误判 / 漏判修复 + 含糊停联转待人工（宁可多拦）。

红线：全部单测 / 假发送函数，**绝不真发**。
"""
from __future__ import annotations

import pytest
from fastapi import Request

from src.ai.chat_assistant_service import quick_risk
from src.compliance import stop_gate as sg
from src.inbox import account_blocklist as abl
from src.inbox import autosend_policy as pol
from src.inbox import stop_contact as sc


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_AUTOSEND_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    try:
        from src.inbox import autosend_shadow_log as sl
        sl._reset_pending_for_tests()
    except Exception:
        pass
    abl.reset_for_tests()
    yield
    abl.reset_for_tests()


@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "stopreview.db")
    yield s
    s.close()


# ── 判定 ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我想退订单可以吗", "我要退款", "退订单怎么操作", "退订的酒店钱什么时候退", "这个订单能取消吗",
    "不要再发呆了", "你别发愁了", "别再发烧了吧", "cancel my order please", "I want a refund",
    "pa-cancel po ng order ko", "can I cancel the booking?", "the end", "that's the end of the story",
    "endless love", "cancelled na yung flight",
])
def test_refund_and_order_talk_is_not_stop_nor_review(text):
    assert not sg.is_stop_message(text), text
    assert sg.review_hint(text) == "", text


@pytest.mark.parametrize("text", [
    "请停止发送", "停止发送", "以后别给我发了", "退订", "pakitigil na po", "huwag mo na akong kontakin",
    "alisin mo na ako sa listahan", "stop na, di ako interested", "Hunong na palihug.",
    "ayaw na ko hasola", "band karo messages", "quit messaging me", "I don't want these messages",
    "Stop it, I'm not interested.", "enough",
    # 蛋博士 10-08：裸 CANCEL / END / QUIT（大小写不限、前后可带标点空格）= CTIA 硬停
    "CANCEL", "cancel", "End", "END?", "  Quit!! ", "...end...", "cancel.",
])
def test_previous_misses_now_stop(text):
    assert sg.is_stop_message(text), text


@pytest.mark.parametrize("text", ["stop it", "stop it 😂", "取消", "Cancel na lang", "please cancel", "cancel po",
                                  "cancel cancel", "omg stop", "should I stop playing?"])
def test_ambiguous_goes_to_review_not_hard_stop(text):
    assert not sg.is_stop_message(text), text
    assert sg.review_hint(text), text


def test_review_hint_never_raises():
    assert sg.review_hint(None) == "" and sg.review_hint("") == "" and sg.review_hint(object()) == ""


# ── replybus ───────────────────────────────────────────────────────────────────

def _replybus_client(monkeypatch, store=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.replybus_routes import register_replybus_routes
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    calls = []

    async def _fake_generate(**kw):
        calls.append(kw)
        return {"ok": True, "reply": "llm reply", "intent": "chat"}

    monkeypatch.setattr("src.web.routes.replybus_routes.generate_persona_reply", _fake_generate)
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_replybus_routes(app, api_auth=_auth, config_manager=None)
    return TestClient(app), calls


def _msg(text, ext):
    return {"message": {"platform": "telegram", "account": "acct_rv", "external_id": ext, "text": text}}


def test_replybus_ambiguous_silent_review_without_stop_record(monkeypatch, store):
    client, calls = _replybus_client(monkeypatch, store)
    r = client.post("/api/replybus/decide", json=_msg("please cancel", "tg:4440001")).json()
    assert r == {"action": "silent", "reason": "stop_review"}, r          # 不确认、不走 LLM
    assert calls == []
    acts = [x["action"] for x in abl.get_blocklist(store).audit_rows(path="replybus_decide")]
    assert acts == ["review"]
    # 没进停联名单：人工确认前，下一条正常消息照常起草
    r2 = client.post("/api/replybus/decide", json=_msg("how much po?", "tg:4440001")).json()
    assert r2["action"] == "draft" and len(calls) == 1


def test_replybus_refund_message_drafts_normally(monkeypatch, store):
    client, calls = _replybus_client(monkeypatch, store)
    r = client.post("/api/replybus/decide", json=_msg("我想退订单可以吗", "tg:4440002")).json()
    assert r["action"] == "draft" and len(calls) == 1


# ── 协议直发链 ────────────────────────────────────────────────────────────────

class _Reg:
    def get(self, p, a):
        return {"meta": {"auto_reply": True}}


@pytest.mark.asyncio
async def test_protocol_autoreply_ambiguous_not_sent_not_frozen(store, monkeypatch):
    from src.integrations import protocol_autoreply as pa
    import src.integrations.protocol_bridge as pb
    from src.inbox.normalizer import conv_id
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    pa._last_reply.clear()
    pa._last_sent.clear()
    sent, generated = [], []

    async def _gen(**kw):
        generated.append(kw.get("text"))
        return "ok sure"

    async def _send(**kw):
        sent.append(kw.get("text"))

    payload = {"direction": "in", "platform": "whatsapp", "account_id": "wa9",
               "chat_key": "639170000009", "text": "stop it"}
    res = await pa.run_autoreply(payload, registry=_Reg(), cfg={"protocol_autoreply": {"enabled": True}},
                                 generate=_gen, send=_send, now=1000.0)
    assert res.get("sent") is not True and res["reason"] == "stop_review", res
    assert sent == [] and generated == []
    assert sc.frozen_reason(store, conv_id("whatsapp", "wa9", "639170000009")) == ""


# ── 收件箱全自动 ──────────────────────────────────────────────────────────────

def test_inbox_autodraft_ambiguous_becomes_l1_review(store):
    from src.inbox.drafts import DraftService
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acct9", "u9")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    store.set_automation_mode(cid, "auto_ai", source="human")
    conv = {"conversation_id": cid, "platform": "telegram", "account_id": "acct9",
            "chat_key": "u9", "display_name": "Lea"}
    svc.auto_generate_draft(conv, "please cancel", automation_mode="auto_ai", enrich=False)
    assert sc.frozen_reason(store, cid) == ""                              # 不冻结
    drafts = [d for d in store.list_drafts(conversation_id=cid, limit=20)
              if d.get("status") in ("pending", "enriching")]
    assert drafts, "应留一张待人工审的稿"
    for d in drafts:
        assert d.get("autopilot_level") != "L2", d                          # 不直发
        assert "stop_review" in (d.get("risk_reasons") or []), d


@pytest.mark.parametrize("text", [
    "Yes you did because I was happy chatting with you i didn't want to stop",
    "I can't stop laughing", "don't ever stop", "never gonna stop",
])
def test_negated_stop_not_review(text):
    assert sg.review_hint(text) == "", text


@pytest.mark.parametrize("text", ["CANCEL", "end.", " Quit "])
def test_replybus_bare_ctia_word_hard_stops_with_single_confirm(monkeypatch, store, text):
    client, calls = _replybus_client(monkeypatch, store)
    ext = "tg:" + str(abs(hash(text)) % 10 ** 9)
    r1 = client.post("/api/replybus/decide", json=_msg(text, ext)).json()
    assert r1["action"] == "silent" and r1["reason"] == "stop", r1
    assert r1.get("confirm_text") and r1.get("confirm_once") is True
    r2 = client.post("/api/replybus/decide", json=_msg("hello?", ext)).json()
    assert r2 == {"action": "silent", "reason": "stop"}, r2
    assert calls == []
