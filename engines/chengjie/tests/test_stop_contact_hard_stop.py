# -*- coding: utf-8 -*-
"""O-1 A（#252 #253 · D-O1，2026-09-08）：stop_contact / self_harm 硬停 + risk=high 不直发。

事故回放 XAM4KV（WhatsApp 09-07 21:00–21:04）：
  21:00:26 「please stop」        shadow=stop_contact risk=high → 21:00:39 发出
  21:02:18 「stop writing to me」 shadow=stop_contact risk=high → 21:02:30 发出「I hear you… Take care.」
  21:03:38 「Never write me again, please」 shadow=-（漏判）→ 21:03:48 又发
验收：第一条发**一条告别**并冻结；第二、三条**零出站**；冻结后关怀 / 目标不出手（档位 manual）；
人工解冻后恢复。词表补 never write me again / don't contact me / leave me alone / stop texting +
中文「别再发了 / 不要再联系我 / 拉黑」。
"""
from __future__ import annotations

import re

import pytest

from src.ai.chat_assistant_service import _stop_contact_hit, quick_analyze, quick_risk
from src.inbox import autosend_policy as pol
from src.inbox import stop_contact as sc
from src.inbox.drafts import DraftService
from src.inbox.store import InboxStore


@pytest.fixture(autouse=True)
def _shadow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_AUTOSEND_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    from src.inbox import autosend_shadow_log as sl
    sl._reset_pending_for_tests()
    yield


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "hardstop.db")
    yield s
    s.close()


# R88（2026-09-17）：硬停只对运营**锁定**的类别执行（缺省全部只记录、AI 照常回）。
# 锁定后的语义：不回客户、只冻结 + 坐席提醒。DraftService / 协议链带「全部锁定」配置验这条。
# 「未锁定 → 继续回复」由文末 R88 用例与 test_risk_grader_gate 覆盖。
def _lock_all_cfg(**extra):
    from src.inbox.risk_grader import LOCKABLE
    cfg = {"inbox": {"risk_grading": {"locked": list(LOCKABLE)}}}
    cfg.update(extra)
    return cfg


CID = "telegram:acct1:u1"


def _conv(chat_key="u1"):
    return {"conversation_id": CID, "platform": "telegram", "account_id": "acct1",
            "chat_key": chat_key, "display_name": "Sinue"}


# ═══════════════════════════════════════════════════════════════════════
# ① 词表：XAM4KV 三句 + 老板点名四组 + 中文 —— 必须判「停止联系」
# ═══════════════════════════════════════════════════════════════════════

_MUST_STOP = [
    "please stop",
    "Grammar, typing speed, and inconsistencies... obviously handled by an AI assistant, so stop writing to me",
    "Never write me again, please",
    "never write me again",
    "don't contact me",
    "Do not contact me again",
    "leave me alone",
    "stop texting",
    "stop texting me",
    "Please don't text me anymore",
    "don't ever message me again",
    "I'm blocking you",
    "delete my number",
    "别再发了",
    "不要再联系我",
    "再发我就拉黑",
    "把你拉黑",
    "别烦我",
]


@pytest.mark.parametrize("text", _MUST_STOP)
def test_stop_contact_word_table_hits(text):
    assert _stop_contact_hit(text.lower()), text
    a = quick_analyze(text)
    assert a["intent"] == "停止联系", (text, a)
    assert a["risk_level"] == "high" and "stop_contact" in a["risk_reasons"], (text, a)
    assert a["risk_hits"], text


# 不是停联：抱怨「你从不给我发消息」/ 否定式 / 转述 / 正常聊天
_NOT_STOP = [
    "you never text me back",
    "he never calls me anymore",
    "why do you never message me first",
    "i didn't want to stop",
    "can't stop thinking about you",
    "haha don't stop, keep telling me",
    "我把他拉黑了",
    "你昨天怎么不联系我",
    "I blocked my ex last week",
    "stop it, you're making me laugh",
]


@pytest.mark.parametrize("text", _NOT_STOP)
def test_stop_contact_word_table_no_false_positive(text):
    assert not _stop_contact_hit(text.lower()), text
    level, reasons = quick_risk(text)
    assert "stop_contact" not in reasons, (text, reasons)


# ═══════════════════════════════════════════════════════════════════════
# ② 告别文案：一句、无客服腔、无 AI 标点、查不到语言回英文
# ═══════════════════════════════════════════════════════════════════════

_BANNED = re.compile(
    r"take care|i hear you|i understand|assistant|助理|\bAI\b|人工智能|feel free|let me know"
    r"|如有需要|很高兴为您|—|–|;|；", re.IGNORECASE)


@pytest.mark.parametrize("lang", sc.farewell_languages() + ["zh-CN", "zh_TW", "zh-Hant", "en-US", "", "unknown", "xx"])
def test_farewell_text_one_sentence_no_service_tone(lang):
    t = sc.farewell_text(lang)
    assert t and len(t) <= 60, (lang, t)
    assert not _BANNED.search(t), (lang, t)
    # 一句：句末标点至多一个（含全角）
    assert len(re.findall(r"[.!?。！？]", t)) <= 1, (lang, t)


def test_farewell_language_routing():
    assert sc.farewell_text("zh-CN") == sc.farewell_text("zh")
    assert sc.farewell_text("zh-TW") == sc.farewell_text("zh-tw") != sc.farewell_text("zh")
    assert sc.farewell_text("xx") == sc.farewell_text("en") == sc.farewell_text("")
    assert sc.FREEZE_REASONS == pol.HARD_STOP_REASONS


# ═══════════════════════════════════════════════════════════════════════
# ③ 冻结 / 解冻（真库）：需人工 + 「客户要求停联」+ 档位 manual + 案例 + 通知
# ═══════════════════════════════════════════════════════════════════════

def test_freeze_and_unfreeze_roundtrip(store, monkeypatch):
    published = []

    class _Bus:
        def publish(self, t, data):
            published.append((t, data))

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    from src.integrations.protocol_autoreply import HANDOFF_TAG

    store.set_automation_mode(CID, "auto_ai", source="human")
    assert sc.frozen_reason(store, CID) == ""
    out = sc.freeze_conversation(
        store, platform="telegram", account_id="acct1", chat_key="u1",
        conversation_id=CID, reason="stop_contact", hits=["never write me again"],
        chat_name="Sinue")
    assert out["tagged"] and out["labelled"] and out["mode_set"] and out["prev_mode"] == "auto_ai"
    assert out["case"] is True and out["notified"] is True and out["already_frozen"] is False
    tags = store.get_conv_tags(CID)
    assert HANDOFF_TAG in tags and sc.STOP_CONTACT_TAG in tags
    assert store.get_handoff_meta(CID)["reason"] == "stop_contact"
    assert store.get_automation_mode_if_set(CID) == "manual"
    meta = store.get_automation_mode_meta(CID)
    assert sc.is_freeze_mode_source(meta["source"]) == "stop_contact"
    assert sc.freeze_prev_mode(meta["source"]) == "auto_ai"
    assert sc.frozen_reason(store, CID) == "stop_contact"
    assert published and published[-1][0] == "escalation"
    assert published[-1][1]["reason"] == "stop_contact" and published[-1][1]["name"] == "Sinue"
    # 需人工不属自动摘除类：AI 回上一句也摘不掉
    from src.integrations.protocol_autoreply import auto_clear_needs_human
    assert auto_clear_needs_human(store, CID, trigger="autosend_sent") is False
    assert sc.frozen_reason(store, CID) == "stop_contact"
    # 重复冻结幂等：不再发通知、不再落案例
    n_pub = len(published)
    out2 = sc.freeze_conversation(
        store, platform="telegram", account_id="acct1", chat_key="u1",
        conversation_id=CID, reason="stop_contact")
    assert out2["already_frozen"] is True and out2["notified"] is False and out2["mode_set"] is False
    assert len(published) == n_pub
    # 人工解冻：摘两标 + 档位还原 auto_ai
    un = sc.unfreeze_conversation(store, CID, actor="agent:zl")
    assert un["was"] == "stop_contact" and un["unlabelled"] and un["untagged"]
    assert un["mode_restored"] == "auto_ai"
    assert sc.frozen_reason(store, CID) == ""
    assert store.get_automation_mode_if_set(CID) == "auto_ai"
    assert HANDOFF_TAG not in store.get_conv_tags(CID)


def test_unfreeze_respects_newer_human_mode_choice(store):
    sc.freeze_conversation(store, platform="telegram", account_id="acct1", chat_key="u1",
                           conversation_id=CID, reason="stop_contact")
    # 坐席期间显式改了档位（review）→ 解冻不动它
    store.set_automation_mode(CID, "review", source="human")
    un = sc.unfreeze_conversation(store, CID)
    assert un["mode_restored"] == "" and store.get_automation_mode_if_set(CID) == "review"
    assert sc.frozen_reason(store, CID) == ""


def test_self_harm_freeze_has_no_stop_contact_label(store):
    out = sc.freeze_conversation(store, platform="telegram", account_id="acct1", chat_key="u1",
                                 conversation_id=CID, reason="self_harm", hits=["kill myself"])
    assert out["tagged"] and out["mode_set"]
    assert sc.STOP_CONTACT_TAG not in store.get_conv_tags(CID)
    assert sc.frozen_reason(store, CID) == "self_harm"
    assert store.get_handoff_meta(CID)["reason"] == "self_harm"


def test_frozen_reason_tolerates_mock_store():
    from unittest.mock import MagicMock
    assert sc.frozen_reason(MagicMock(), CID) == ""
    assert sc.frozen_reason(None, CID) == ""
    assert sc.is_farewell_draft({"risk_reasons": '["stop_contact", "farewell:stop_contact"]'})
    assert sc.is_farewell_draft({"risk_reasons": ["farewell:stop_contact"]})
    assert not sc.is_farewell_draft({"risk_reasons": ["stop_contact"]})
    assert not sc.is_farewell_draft(None)


# ═══════════════════════════════════════════════════════════════════════
# ④ DraftService 回放 XAM4KV 三条入站：一条告别 → 冻结 → 零草稿
# ═══════════════════════════════════════════════════════════════════════

def test_replay_xam4kv_notify_then_silence(store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    store.set_automation_mode(CID, "auto_ai", source="human")
    conv = _conv()
    d1 = svc.auto_generate_draft(conv, "please stop", automation_mode="auto_ai", enrich=True)
    assert d1 is None
    assert sc.frozen_reason(store, CID) == "stop_contact"
    assert store.get_automation_mode_if_set(CID) == "manual"
    # 第二、三条：不起草、不出站
    d2 = svc.auto_generate_draft(conv, "so stop writing to me", automation_mode="auto_ai", enrich=True)
    d3 = svc.auto_generate_draft(conv, "Never write me again, please", automation_mode="auto_ai", enrich=True)
    assert d2 is None and d3 is None
    pend = [d for d in store.list_drafts(conversation_id=CID, limit=20)
            if d.get("status") in ("pending", "enriching")]
    assert pend == []


def test_replay_self_harm_notify_then_human(store):
    """自伤锁定：不回客户，只冻结切人工；第二条零草稿。"""
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    store.set_automation_mode(CID, "auto_ai", source="human")
    conv = _conv()
    d1 = svc.auto_generate_draft(conv, "I want to kill myself", automation_mode="auto_ai", enrich=True)
    assert d1 is None
    assert sc.frozen_reason(store, CID) == "self_harm"
    assert store.get_automation_mode_if_set(CID) == "manual"
    assert sc.STOP_CONTACT_TAG not in store.get_conv_tags(CID)
    assert svc.auto_generate_draft(conv, "nobody cares anyway", automation_mode="auto_ai", enrich=True) is None


def test_review_mode_stop_contact_freezes_without_farewell(store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    did = svc.auto_generate_draft(_conv(), "leave me alone", automation_mode="review")
    assert did is None
    assert sc.frozen_reason(store, CID) == "stop_contact"
    assert store.get_automation_mode_if_set(CID) == "manual"


def test_high_risk_non_stop_goes_to_review_and_tags(store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    did = svc.auto_generate_draft(_conv(), "send me your bank card number", automation_mode="auto_ai")
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L1" and row["risk_level"] == "high"
    assert not str(row.get("draft_text") or "").strip()   # 锁定留白，无固定话术
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    assert HANDOFF_TAG in store.get_conv_tags(CID)
    assert store.get_handoff_meta(CID)["reason"] == "high_risk"
    assert sc.frozen_reason(store, CID) == ""            # 不是冻结，只是人审
    assert store.get_automation_mode_if_set(CID) is None  # 档位不动


# ═══════════════════════════════════════════════════════════════════════
# ⑤ worker 门禁：冻结会话只放行告别稿，其余 L2 取消
# ═══════════════════════════════════════════════════════════════════════

class _Svc:
    def __init__(self, store):
        self.queue = []
        self._store = store

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}


def _item(conv, text, draft_id, reasons):
    return {
        "draft_id": draft_id, "autopilot_level": "L2", "final_text": text,
        "platform": "telegram", "account_id": "acct1", "chat_key": "u1",
        "conversation_id": conv, "source_id": conv, "risk_reasons": reasons,
    }


def _seed(store, conv, draft_id, text, reasons):
    return store.upsert_draft({
        "draft_id": draft_id, "source_kind": "inbox", "source_id": draft_id,
        "conversation_id": conv, "platform": "telegram", "account_id": "acct1",
        "chat_key": "u1", "peer_text": "x", "draft_text": text,
        "autopilot_level": "L2", "risk_level": "high", "risk_reasons": reasons,
        "status": "pending",
    })


@pytest.mark.asyncio
async def test_worker_frozen_conversation_only_farewell_passes(store):
    from src.inbox.autosend_worker import AutosendWorker
    sc.freeze_conversation(store, platform="telegram", account_id="acct1", chat_key="u1",
                           conversation_id=CID, reason="stop_contact")
    fw = _seed(store, CID, "d-farewell", sc.farewell_text("en"), ["stop_contact", sc.FAREWELL_MARK])
    ai = _seed(store, CID, "d-ai", "I hear you, and I'll stop here. Take care.", ["stop_contact"])
    other = _seed(store, "telegram:acct1:u2", "d-other", "hey!", [])
    sent = []

    async def _cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    svc = _Svc(store)
    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [
        _item(CID, sc.farewell_text("en"), fw, ["stop_contact", sc.FAREWELL_MARK]),
        _item(CID, "I hear you, and I'll stop here. Take care.", ai, ["stop_contact"]),
        _item("telegram:acct1:u2", "hey!", other, []),
    ]
    await w._tick()
    assert sorted(sent) == sorted([sc.farewell_text("en"), "hey!"]), sent
    assert store.get_draft(ai)["status"] == "cancelled"
    assert store.get_draft(ai)["decided_by"] == "stop_contact_frozen"
    assert w.total_skipped_stop_contact == 1
    assert w.status_snapshot()["total_skipped_stop_contact"] == 1


@pytest.mark.asyncio
async def test_worker_self_harm_pass_mark_lets_one_reply_through(store):
    from src.inbox.autosend_worker import AutosendWorker
    sc.freeze_conversation(store, platform="telegram", account_id="acct1", chat_key="u1",
                           conversation_id=CID, reason="self_harm")
    one = _seed(store, CID, "d-one", "我在，别怕。", ["self_harm", sc.HARD_STOP_PASS_MARK])
    two = _seed(store, CID, "d-two", "还在吗？", ["self_harm"])
    sent = []

    async def _cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    svc = _Svc(store)
    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [_item(CID, "我在，别怕。", one, ["self_harm", sc.HARD_STOP_PASS_MARK]),
                 _item(CID, "还在吗？", two, ["self_harm"])]
    await w._tick()
    assert sent == ["我在，别怕。"]
    assert store.get_draft(two)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_worker_farewell_survives_manual_mode_downgrade_gate(store):
    """冻结把档位按成 manual；「档位已降级」取消闸必须放过告别稿（否则告别永远发不出）。"""
    from src.inbox.autosend_worker import AutosendWorker
    sc.freeze_conversation(store, platform="telegram", account_id="acct1", chat_key="u1",
                           conversation_id=CID, reason="stop_contact")
    assert store.get_automation_mode_if_set(CID) == "manual"
    fw = _seed(store, CID, "d-farewell2", sc.farewell_text("en"), [sc.FAREWELL_MARK])
    sent = []

    async def _cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    svc = _Svc(store)
    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [_item(CID, sc.farewell_text("en"), fw, [sc.FAREWELL_MARK])]
    await w._tick()
    assert sent == [sc.farewell_text("en")]
    assert w.total_skipped_mode == 0 and w.total_skipped_stop_contact == 0


# ═══════════════════════════════════════════════════════════════════════
# ⑥ 协议链直发：入站停联 → 只发一条告别 + 冻结；再来 → 档位 manual 早退
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_protocol_autoreply_stop_contact_farewell_then_silence(store, monkeypatch):
    from src.integrations import protocol_autoreply as pa
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    pa._last_reply.clear()
    pa._last_sent.clear()
    sent = []
    generated = []

    class _Reg:
        def get(self, p, a):
            return {"meta": {"auto_reply": True}}

    async def _gen(**kw):
        generated.append(kw.get("text"))
        return "I hear you, and I'll stop here. Take care."

    async def _send(**kw):
        sent.append(kw["text"])

    cfg = _lock_all_cfg(protocol_autoreply={"enabled": True})

    def _mode(p, a, c):
        from src.inbox.automation_mode import resolve_automation_mode
        return resolve_automation_mode(store, f"{p}:{a}:{c}", cfg)

    payload = {"direction": "in", "platform": "telegram", "account_id": "acct1",
               "chat_key": "u1", "text": "Never write me again, please"}
    res = await pa.run_autoreply(payload, registry=_Reg(), cfg=cfg, generate=_gen,
                                 send=_send, inbox_mode_fn=_mode, now=1000.0)
    assert res["reason"] == "stop_contact" and res.get("sent") is not True
    assert not res.get("farewell")
    assert sent == [] and generated == []
    assert sc.frozen_reason(store, CID) == "stop_contact"
    assert pa.record_decision_audit is not None and "stop_contact" in pa.AUDIT_REASONS
    # 第二条：档位已 manual → 直发链早退，零出站、零生成
    payload2 = dict(payload, text="stop texting me")
    res2 = await pa.run_autoreply(payload2, registry=_Reg(), cfg=cfg, generate=_gen,
                                  send=_send, inbox_mode_fn=_mode, now=1100.0)
    assert res2.get("sent") is not True and res2["reason"] == "inbox_manual"
    assert sent == [] and generated == []


# ═══════════════════════════════════════════════════════════════════════
# ⑦ R88（2026-09-17）：未锁定 ＝ 只记录——停联 / 自伤入站 AI 照常生成 / 发送、不冻结
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_r88_protocol_autoreply_unlocked_stop_contact_keeps_replying(store, monkeypatch, caplog):
    import logging
    from src.integrations import protocol_autoreply as pa
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    pa._last_reply.clear()
    pa._last_sent.clear()
    sent, generated = [], []

    class _Reg:
        def get(self, p, a):
            return {"meta": {"auto_reply": True}}

    async def _gen(**kw):
        generated.append(kw.get("text"))
        return "Of course, I'll give you space. Take care."

    async def _send(**kw):
        sent.append(kw["text"])

    cfg = {"protocol_autoreply": {"enabled": True}}      # 缺省：无 inbox.risk_grading.locked

    def _mode(p, a, c):
        from src.inbox.automation_mode import resolve_automation_mode
        return resolve_automation_mode(store, f"{p}:{a}:{c}", cfg)

    payload = {"direction": "in", "platform": "telegram", "account_id": "acct1",
               "chat_key": "u1", "text": "Never write me again, please"}
    with caplog.at_level(logging.INFO):
        res = await pa.run_autoreply(payload, registry=_Reg(), cfg=cfg, generate=_gen,
                                     send=_send, inbox_mode_fn=_mode, now=1000.0)
    assert res.get("sent") is True and res["reason"] != "stop_contact", res
    assert generated == [payload["text"]] and sent == ["Of course, I'll give you space. Take care."]
    assert sc.frozen_reason(store, CID) == ""
    assert store.get_automation_mode_if_set(CID) is None
    assert any("hard_stop=stop_contact" in r.getMessage() and "unlocked" in r.getMessage() for r in caplog.records)


def test_r88_draft_service_unlocked_stop_contact_and_self_harm_not_frozen(store):
    """B 线缺省：停联 / 自伤入站 → 普通 L2 稿（不是告别稿、不带硬停标记），会话不冻结、档位不动。"""
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    store.set_automation_mode(CID, "auto_ai", source="human")
    d1 = svc.auto_generate_draft(_conv(), "please stop", automation_mode="auto_ai", enrich=True)
    row = store.get_draft(d1)
    assert row["autopilot_level"] == "L2" and not sc.is_farewell_draft(row) and not sc.is_hard_stop_pass_draft(row)
    assert sc.frozen_reason(store, CID) == "" and store.get_automation_mode_if_set(CID) == "auto_ai"
    assert "stop_contact_recorded" in row["risk_reasons"] and "stop_contact" not in row["risk_reasons"], row["risk_reasons"]
    d2 = svc.auto_generate_draft(_conv(), "I want to kill myself", automation_mode="auto_ai", enrich=True)
    row2 = store.get_draft(d2)
    assert d2 and row2["autopilot_level"] == "L2" and not sc.is_hard_stop_pass_draft(row2)
    assert sc.frozen_reason(store, CID) == ""
    assert "self_harm_recorded" in row2["risk_reasons"]
