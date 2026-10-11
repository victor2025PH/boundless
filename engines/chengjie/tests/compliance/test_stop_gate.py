# -*- coding: utf-8 -*-
"""智安 P0-2（2026-10-08）：统一 STOP 硬闸。

红线：「用户发 STOP 后继续发」——本文件全部用单测 / 假发送函数 / 审计表验证，**绝不真发**。

覆盖：
① 多语词表（en / tl / Taglish / Bisaya / hi / zh）≥ 30 条必须命中 + 一组不得误判；
② 与主线同一入口：quick_analyze → stop_contact → hard_stop_reason；
③ 默认硬停：risk_grader 隐含锁定 stop_contact（locked_by=stop_gate），开关显式关才回到 R88；
④ 名单：会话冻结 / 本账号 / 跨账号同 external_id / 同手机号；审计表只记元数据；
⑤ 接入点：replybus decide（silent + reason=stop + 唯一模板确认）、protocol_autoreply、
   收件箱起草（autosend）、主动触达候选闸。
"""
from __future__ import annotations

import pytest
from fastapi import Request

from src.ai.chat_assistant_service import _stop_contact_hit, quick_analyze, quick_risk
from src.compliance import stop_gate as sg
from src.inbox import account_blocklist as abl
from src.inbox import autosend_policy as pol
from src.inbox import risk_grader as rg
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
    s = InboxStore(tmp_path / "stopgate.db")
    yield s
    s.close()


# ═══════════════════════════════════════════════════════════════════════
# ① 多语词表
# ═══════════════════════════════════════════════════════════════════════

MUST_STOP = [
    # en
    "STOP", "stop", "Stop.", "STOP!!!", "stop 🙏", "please STOP", "STOP pls", "stop stop stop",
    "unsubscribe", "Unsubscribe me", "stop messaging me", "Stop sending me messages",
    "opt out", "remove me from your list", "no more messages", "I want to unsubscribe",
    "STOPALL",
    # tl / Taglish
    "Stop po", "stop na po", "Stop na po kayo", "tigil na", "Tigil na po", "tigilan mo na ako",
    "ayoko na", "Ayoko na po", "ayaw ko na", "huwag mo na akong i-chat", "wag mo na ako i-message",
    "Wag nyo na ako i-text", "huwag mo na akong istorbohin", "ayoko na ng mga message nyo",
    "tama na po", "stop mo na ang pag message", "Pls stop na, ayoko na",
    # Bisaya
    "hunong na", "Ayaw na ko i-message", "ayaw na pag message", "undang na sa pag text",
    # hi / Hinglish
    "message mat karo", "mujhe msg mat bhejo", "band karo", "मुझे मैसेज मत करो", "मैसेज मत भेजो",
    "बंद करो", "pareshan mat karo",
    # zh（旧正则 + 新短指令）
    "退订", "停", "TD", "别再发了", "不要再联系我", "别给我发消息",
    # 多句
    "STOP. Ayoko na.", "Stop po. Salamat",
]

NOT_STOP = [
    "stop it, you're making me laugh",
    "Stop! haha you are so funny",
    "haha don't stop, keep telling me",
    "i didn't want to stop",
    "can't stop thinking about you",
    "I can't stop messaging you lol",
    "where is the bus stop",
    "I will stop by tomorrow",
    "stopwatch",
    "ayoko na ng adobo",
    "wag na, ok lang",
    "Tama na yung sagot mo",
    "Kailangan ko pa mag-deliver",
    "kaibigan ko rin",
    "Abot ko pa yung last trip",
    "Paano mag deposit?",
    "hello po",
    "bas",
    "no more",
    "我把他拉黑了",
    "你昨天怎么不联系我",
    "you never text me back",
]


@pytest.mark.parametrize("text", MUST_STOP)
def test_lexicon_must_stop(text):
    hit = _stop_contact_hit(text.lower())
    assert hit, text
    a = quick_analyze(text)
    assert a["intent"] == "停止联系", (text, a)
    assert "stop_contact" in a["risk_reasons"] and a["risk_level"] == "high", (text, a)
    assert pol.hard_stop_reason(a["risk_reasons"]) == "stop_contact"
    assert sc.stop_contact_hits(text), text
    assert sg.is_stop_message(text)


@pytest.mark.parametrize("text", NOT_STOP)
def test_lexicon_no_false_positive(text):
    assert not _stop_contact_hit(text.lower()), text
    _lvl, reasons = quick_risk(text)
    assert "stop_contact" not in reasons, (text, reasons)
    assert not sg.is_stop_message(text)


def test_lexicon_case_count_at_least_30():
    assert len(MUST_STOP) >= 30 and len(NOT_STOP) >= 15


def test_normalize_and_phone_key():
    assert sg.normalize("  STOP!!! 🙏 ") == "stop"
    assert sg.phone_key("639171234567@s.whatsapp.net") == "9171234567"
    assert sg.phone_key("639171234567:12@s.whatsapp.net") == "9171234567"
    assert sg.phone_key("phone:09171234567") == "9171234567"
    assert sg.phone_key("12345") == ""


# ═══════════════════════════════════════════════════════════════════════
# ② / ③ 默认硬停：隐含锁定
# ═══════════════════════════════════════════════════════════════════════

def test_stop_contact_implied_locked_by_default():
    assert sg.enforced({}) and sg.enforced(None)
    assert rg.is_locked("stop_contact", {}) is False            # 运营显式锁定那份不变
    assert rg.is_locked_effective("stop_contact", {}) == (True, "stop_gate")
    assert rg.stop_gate_locked({}) == ["stop_contact"]
    assert rg.implied_locked({}) == []           # 成人政策隐含锁定那份不受影响
    out, renamed = rg.rename_unlocked_hard_reasons(["stop_contact", "self_harm"], {})
    assert out == ["stop_contact", "self_harm_recorded"] and renamed == ["self_harm"]
    rows = {r["id"]: r for r in rg.public_table(None, {})}
    assert rows["stop_contact"]["locked"] is False
    assert rows["stop_contact"]["locked_by"] == "stop_gate" and rows["stop_contact"]["outcome"] == "freeze"


def test_kill_switch_restores_r88_record_only():
    off = {"compliance": {"stop_gate": {"enabled": False}}}
    assert not sg.enforced(off)
    assert rg.is_locked_effective("stop_contact", off) == (False, "")
    out, _ = rg.rename_unlocked_hard_reasons(["stop_contact"], off)
    assert out == ["stop_contact_recorded"]
    assert sg.enforced({"compliance": {"stop_gate": {"enabled": "false"}}}) is False


# ═══════════════════════════════════════════════════════════════════════
# ④ 名单 + 审计
# ═══════════════════════════════════════════════════════════════════════

def test_contact_stopped_sources_and_audit(store):
    assert sg.contact_stopped(store, "telegram", "acctA", "u9") == ""
    rec = sg.record_stop(store, platform="telegram", account_id="acctA", peer="u9", hit="stop")
    assert rec["listed"] and rec["already"] is False
    assert sg.contact_stopped(store, "telegram", "acctA", "u9") in ("frozen", "blocklist")
    # 跨账号：同平台同 external_id
    assert sg.contact_stopped(store, "telegram", "acctB", "u9") == "blocklist_peer"
    # 不同平台同 id 不串
    assert sg.contact_stopped(store, "messenger", "acctB", "u9") == ""
    # 同手机号：WA 停联 → 另一个 WA 号 / phone 形态同样停
    sg.record_stop(store, platform="whatsapp", account_id="wa1", peer="639171234567@s.whatsapp.net", hit="stop po")
    assert sg.contact_stopped(store, "whatsapp", "wa2", "09171234567@s.whatsapp.net") == "blocklist_phone"
    assert sg.contact_stopped(store, "messenger", "m1", "psid-1", phone="+63 917 123 4567") == "blocklist_phone"
    # outbound_check 记审计（不记原文）
    assert sg.outbound_check(store, path="unit", platform="telegram", account_id="acctB", peer="u9") == "blocklist_peer"
    rows = abl.get_blocklist(store).audit_rows(path="unit")
    assert rows and rows[0]["action"] == "blocked" and rows[0]["reason"] == "stop"
    assert set(rows[0].keys()) >= {"ts", "path", "action", "platform", "account_id", "peer", "conversation_id", "reason", "hit"}
    assert "text" not in rows[0]


def test_unfreeze_releases_same_account(store):
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acctA", "u7")
    store.set_automation_mode(cid, "auto_ai", source="human")
    sc.freeze_conversation(store, platform="telegram", account_id="acctA", chat_key="u7",
                           conversation_id=cid, reason="stop_contact", hits=["stop"])
    assert sg.contact_stopped(store, "telegram", "acctA", "u7") == "frozen"
    sc.unfreeze_conversation(store, cid, actor="tester")
    assert sg.contact_stopped(store, "telegram", "acctA", "u7") == ""


# ═══════════════════════════════════════════════════════════════════════
# ⑤ 接入点
# ═══════════════════════════════════════════════════════════════════════

def _replybus_client(monkeypatch, store=None, config=None):
    from types import SimpleNamespace
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

    cm = SimpleNamespace(config=config or {}) if config is not None else None
    register_replybus_routes(app, api_auth=_auth, config_manager=cm)
    return TestClient(app), calls


def _msg(text, ext="tg:5550001", account="acct_rb"):
    return {"message": {"platform": "telegram", "account": account, "external_id": ext, "text": text}}


@pytest.mark.parametrize("stop_text", ["STOP", "Stop po", "tigil na", "huwag mo na akong i-chat", "退订"])
def test_replybus_stop_then_always_silent(monkeypatch, store, stop_text):
    client, calls = _replybus_client(monkeypatch, store)
    ext = "tg:" + str(abs(hash(stop_text)) % 10 ** 9)
    r1 = client.post("/api/replybus/decide", json=_msg(stop_text, ext)).json()
    assert r1["action"] == "silent" and r1["reason"] == "stop", r1
    assert r1.get("confirm_text") and r1.get("confirm_once") is True      # 唯一一条模板确认
    assert "sent" not in r1 and "delivered" not in r1
    assert calls == []                                                   # 不走 LLM
    # 之后任何消息都 silent，且不再给确认
    r2 = client.post("/api/replybus/decide", json=_msg("hi again, musta?", ext)).json()
    assert r2 == {"action": "silent", "reason": "stop"}, r2
    r3 = client.post("/api/replybus/decide", json=_msg("STOP", ext)).json()
    assert r3 == {"action": "silent", "reason": "stop"}, r3
    assert calls == []
    rows = abl.get_blocklist(store).audit_rows(path="replybus_decide")
    actions = [x["action"] for x in rows]
    assert "detected" in actions and "confirm" in actions and actions.count("blocked") >= 2


def test_replybus_stop_cross_account_and_no_store(monkeypatch):
    client, calls = _replybus_client(monkeypatch, None)
    r1 = client.post("/api/replybus/decide", json=_msg("unsubscribe", "tg:777", "acc1")).json()
    assert r1["reason"] == "stop" and r1.get("confirm_text")
    r2 = client.post("/api/replybus/decide", json=_msg("hello", "tg:777", "acc2")).json()
    assert r2 == {"action": "silent", "reason": "stop"}
    assert calls == []


def test_replybus_normal_message_still_drafts(monkeypatch, store):
    client, calls = _replybus_client(monkeypatch, store)
    r = client.post("/api/replybus/decide", json=_msg("musta ka na? kain na tayo", "tg:888")).json()
    assert r["action"] == "draft" and r["text"] == "llm reply"
    assert len(calls) == 1


def test_replybus_kill_switch_off_generates(monkeypatch, store):
    client, calls = _replybus_client(monkeypatch, store, config={"compliance": {"stop_gate": {"enabled": False}}})
    r = client.post("/api/replybus/decide", json=_msg("STOP", "tg:999")).json()
    assert r["action"] == "draft" and len(calls) == 1


class _Reg:
    def get(self, p, a):
        return {"meta": {"auto_reply": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_text", ["STOP", "Stop po", "ayoko na", "Ayaw na ko i-message", "message mat karo"])
async def test_protocol_autoreply_default_hard_stop_without_manual_lock(store, monkeypatch, stop_text):
    """缺省配置（无 inbox.risk_grading.locked）：停联入站 → 零生成、零出站、冻结；再来消息 → 仍零出站。"""
    from src.integrations import protocol_autoreply as pa
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    pa._last_reply.clear()
    pa._last_sent.clear()
    sent, generated = [], []

    async def _gen(**kw):
        generated.append(kw.get("text"))
        return "ok sure"

    async def _send(**kw):
        sent.append(kw.get("text"))

    cfg = {"protocol_autoreply": {"enabled": True}}
    payload = {"direction": "in", "platform": "whatsapp", "account_id": "wa1",
               "chat_key": "639170000001", "text": stop_text}
    res = await pa.run_autoreply(payload, registry=_Reg(), cfg=cfg, generate=_gen, send=_send, now=1000.0)
    assert res.get("sent") is not True and res["reason"] == "stop_contact", res
    assert sent == [] and generated == []
    from src.inbox.normalizer import conv_id
    assert sc.frozen_reason(store, conv_id("whatsapp", "wa1", "639170000001")) == "stop_contact"
    # 第二条：没有 inbox_mode_fn（档位闸缺席）→ 会话风险持有（冻结时设的 stop_contact）先拦；
    # 持有被人工清掉但会话仍冻结 → STOP 硬闸拦（见下）
    res2 = await pa.run_autoreply(dict(payload, text="hello?"), registry=_Reg(), cfg=cfg,
                                  generate=_gen, send=_send, now=2000.0)
    assert res2.get("sent") is not True and res2["reason"] in ("risk_hold", "stop_gate"), res2
    assert sent == [] and generated == []
    acts = [r["action"] for r in abl.get_blocklist(store).audit_rows(path="protocol_autoreply")]
    assert "detected" in acts
    # 「需人工」/ 风险持有被人工摘掉、但会话仍冻结 → STOP 硬闸兜底拦下并留痕
    from src.integrations.protocol_autoreply import clear_needs_human
    _cid = conv_id("whatsapp", "wa1", "639170000001")
    clear_needs_human(store, _cid, actor="test")
    assert sc.frozen_reason(store, _cid) == "stop_contact"
    pa._last_reply.clear()
    res3 = await pa.run_autoreply(dict(payload, text="hello again"), registry=_Reg(), cfg=cfg,
                                  generate=_gen, send=_send, now=3000.0)
    assert res3.get("sent") is not True and res3["reason"] in ("stop_gate", "risk_hold"), res3
    assert sent == [] and generated == []


@pytest.mark.asyncio
async def test_protocol_autoreply_blocked_peer_on_other_account(store, monkeypatch):
    from src.integrations import protocol_autoreply as pa
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    pa._last_reply.clear()
    pa._last_sent.clear()
    sg.record_stop(store, platform="whatsapp", account_id="wa1", peer="639170000002", hit="stop")
    sent = []

    async def _gen(**kw):
        return "hey there"

    async def _send(**kw):
        sent.append(kw.get("text"))

    payload = {"direction": "in", "platform": "whatsapp", "account_id": "wa2",
               "chat_key": "639170000002", "text": "hi"}
    res = await pa.run_autoreply(payload, registry=_Reg(), cfg={"protocol_autoreply": {"enabled": True}},
                                 generate=_gen, send=_send, now=1000.0)
    assert res["reason"] == "stop_gate" and sent == []


def test_inbox_autodraft_default_hard_stop(store):
    """收件箱全自动（autosend_policy）：缺省配置下「STOP / Stop po」→ 不起草、冻结；后续零草稿。"""
    from src.inbox.drafts import DraftService
    from src.inbox.normalizer import conv_id
    cid = conv_id("telegram", "acct1", "u1")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    store.set_automation_mode(cid, "auto_ai", source="human")
    conv = {"conversation_id": cid, "platform": "telegram", "account_id": "acct1",
            "chat_key": "u1", "display_name": "Juan"}
    d1 = svc.auto_generate_draft(conv, "Stop po", automation_mode="auto_ai", enrich=True)
    assert d1 is None
    assert sc.frozen_reason(store, cid) == "stop_contact"
    assert svc.auto_generate_draft(conv, "hello", automation_mode="auto_ai", enrich=True) is None
    pend = [d for d in store.list_drafts(conversation_id=cid, limit=20)
            if d.get("status") in ("pending", "enriching")]
    assert pend == []


def test_inbox_autodraft_blocked_by_cross_account_list(store):
    from src.inbox.drafts import DraftService
    from src.inbox.normalizer import conv_id
    sg.record_stop(store, platform="telegram", account_id="acctX", peer="u5", hit="unsubscribe")
    cid = conv_id("telegram", "acctY", "u5")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    conv = {"conversation_id": cid, "platform": "telegram", "account_id": "acctY",
            "chat_key": "u5", "display_name": "Ana"}
    assert svc.auto_generate_draft(conv, "hi musta", automation_mode="auto_ai") is None
    acts = [r["action"] for r in abl.get_blocklist(store).audit_rows(path="inbox_autodraft")]
    assert acts == ["blocked"]


def test_proactive_candidate_blocked_cross_account(store):
    from src.companion.proactive_peer_hygiene import proactive_candidate_ok
    abl.get_blocklist(store)          # 登记进程默认实例（与生产首次取用同）
    sg.record_stop(store, platform="telegram", account_id="acc1", peer="u42", hit="stop")
    ok, why = proactive_candidate_ok({"platform": "telegram", "account_id": "acc2", "chat_key": "u42"}, {})
    assert ok is False and why == "stop_contact"
    ok2, _ = proactive_candidate_ok({"platform": "telegram", "account_id": "acc2", "chat_key": "u43"}, {})
    assert ok2 is True


# 收件箱自动发送 worker（autosend 管线最后一道）：SOP 链稿 / 常规 L2 稿直接落库后，
# 对端已在别的账号 STOP 过 → 取消稿 + 审计 blocked，零出站（只留痕，测试用假回调）
class _WSvc:
    def __init__(self, store, cfg=None):
        self.queue = []
        self._store = store
        self._cfg = cfg or {}

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}


def _wseed(store, cid, draft_id, peer):
    store.upsert_draft({
        "draft_id": draft_id, "source_kind": "inbox", "source_id": draft_id,
        "conversation_id": cid, "platform": "telegram", "account_id": "acctW",
        "chat_key": peer, "peer_text": "x", "draft_text": "hello again",
        "autopilot_level": "L2", "risk_level": "low", "risk_reasons": [], "status": "pending",
    })
    return {"draft_id": draft_id, "autopilot_level": "L2", "final_text": "hello again",
            "platform": "telegram", "account_id": "acctW", "chat_key": peer,
            "conversation_id": cid, "source_id": cid, "risk_reasons": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg,expect_block", [({}, True),
                                              ({"compliance": {"stop_gate": {"enabled": False}}}, False)])
async def test_autosend_worker_cancels_draft_for_stopped_peer(store, cfg, expect_block):
    from src.inbox.autosend_worker import AutosendWorker
    from src.inbox.normalizer import conv_id
    sg.record_stop(store, platform="telegram", account_id="acctOther", peer="u77", hit="Stop po")
    c_stop, c_ok = conv_id("telegram", "acctW", "u77"), conv_id("telegram", "acctW", "u78")
    sent = []

    async def _cb(platform, account_id, chat_key, text):
        sent.append(chat_key)
        return {"ok": True}

    async def _sleep(d):
        return None

    svc = _WSvc(store, cfg)
    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [_wseed(store, c_stop, "d-stop", "u77"), _wseed(store, c_ok, "d-ok", "u78")]
    await w._tick()
    if expect_block:
        assert sent == ["u78"], sent
        row = store.get_draft("d-stop")
        assert row["status"] == "cancelled" and row["decided_by"] == "stop_gate"
        acts = [r["action"] for r in abl.get_blocklist(store).audit_rows(path="autosend_worker")]
        assert acts == ["blocked"]
    else:
        assert sorted(sent) == ["u77", "u78"], sent
