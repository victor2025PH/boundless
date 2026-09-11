# -*- coding: utf-8 -*-
"""Q-20 门禁：称呼层级与客户侧名字优先（#178 追加 · VDUJX6 / PQE9ZF / Q9GDEH / 2PKKM6）。

四条门禁（指令 E 段），每条对应一起真实事故：

  1. 联系人级「我怎么叫对方」为空 + 人设级爱称 babe → prompt **不出 babe**，改用会话
     显示名或不称呼（VDUJX6/PQE9ZF：一百个客户共用一个 babe）；
  2. 联系人级 ``peer_calls_you=Alicia`` ≠ 人设名 Mizuki → system prompt 含「绝不纠正」，
     且 call_peer / peer_calls_you 两方向绝不互换；
  3. 出站「It's Mizuki, not Alicia」被收口点守卫拦下（否认客户侧名字 → 剥除 / 改写）；
  4. VDUJX6 回放：人工手发一张图 3 分钟后客户问「是你吗」→ prompt 走**认领**分支、
     绝不出「你从未发过 / 没这个功能」，且「没这个功能 / I can't send photos」属能力自曝黑名单。

红线：本文件只读 ``persona_reply``（回放经它的 ``_prompt_addenda`` 接线点，不改它）。
"""
from __future__ import annotations

import time

import pytest

from src.inbox import contact_names as cn
from src.inbox import image_autosend as ia
from src.inbox.store import InboxConversation, InboxMessage, InboxStore

_CID = "whatsapp:acc1:peer1"
_PERSONA = {
    "id": "p_q20", "name": "Mizuki", "role": "留学生",
    "names": {"call_peer": "babe", "peer_calls_you": "Mizuki"},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    cn._reset_for_tests()
    ia.bind_prompt_conv("")
    s = InboxStore(tmp_path / "inbox.db")
    s.upsert_conversation(InboxConversation(
        conversation_id=_CID, platform="whatsapp", account_id="acc1",
        chat_key="peer1", display_name="David", last_text="hi", last_ts=time.time()))
    monkeypatch.setattr("src.integrations.protocol_bridge.get_inbox_store", lambda: s)
    yield s
    s.close()
    ia.bind_prompt_conv("")
    cn._reset_for_tests()


def _prompt_blocks(store):
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    return [
        pm._format_persona_instructions(dict(_PERSONA), conversation_id=_CID),
        pm._format_persona_compact(dict(_PERSONA), conversation_id=_CID),
    ]


# ══ 1. 联系人级空 + 人设 babe → 不出 babe ═══════════════════════════════════

def test_gate1_blank_contact_never_falls_back_to_persona_pet(store):
    assert cn.resolve_address_names(store, _CID, _PERSONA) == {
        "call_peer": "David", "peer_calls_you": ""}
    for blk in _prompt_blocks(store):
        assert "babe" not in blk
        assert "David" in blk
    # 会话显示名不合格（裸号码）→ 不称呼，同样不出 babe
    bare = "whatsapp:acc1:15551234567"
    store.upsert_conversation(InboxConversation(
        conversation_id=bare, platform="whatsapp", account_id="acc1",
        chat_key="15551234567", display_name="+15551234567", last_text="hi",
        last_ts=time.time()))
    assert cn.resolve_address_names(store, bare, _PERSONA)["call_peer"] == ""
    from src.utils.persona_manager import PersonaManager
    blk = PersonaManager.get_instance()._format_persona_instructions(
        dict(_PERSONA), conversation_id=bare)
    assert "babe" not in blk
    # 人设级只在**无会话**时生效（预览 / 工具型调用）
    assert "babe" in PersonaManager.get_instance()._format_persona_instructions(dict(_PERSONA))


# ══ 2. peer_calls_you=Alicia → 「绝不纠正」+ 方向不互换 ═════════════════════

def test_gate2_client_side_name_pins_never_correct(store):
    cn.set_contact_names(store, _CID, call_peer="honey", peer_calls_you="Alicia")
    for blk in _prompt_blocks(store):
        assert "绝不纠正" in blk and "Alicia" in blk
        assert "babe" not in blk
        # 方向：honey 是我叫对方，Alicia 是对方叫我——两句各归各，不得互换
        i_call = blk.index("honey")
        they_call = blk.index("Alicia")
        assert i_call != they_call
        assert "叫你「Alicia」" in blk or "叫你 Alicia" in blk or "一直叫你「Alicia」" in blk
        assert "叫你「honey」" not in blk
    # identity_addendum 会话参数同句（Q-6 接线 ③ 传 conversation_id 即得）
    from src.inbox.prompt_addenda import identity_addendum
    en = identity_addendum(_PERSONA, None, lang="en", conversation_id=_CID)
    assert "Alicia" in en and "Never correct" in en
    zh = identity_addendum(_PERSONA, None, lang="zh", conversation_id=_CID)
    assert "绝不纠正" in zh
    # 旧签名（无会话）输出不变：账号名一致 / 未知 → ""
    assert identity_addendum(_PERSONA, None, lang="en") == ""
    # 一致（Alicia 就是人设名）→ 不出该句
    cn.set_contact_names(store, _CID, peer_calls_you="Mizuki 🌸")
    assert identity_addendum(_PERSONA, None, lang="en", conversation_id=_CID) == ""


# ══ 3. 出站「It's Mizuki, not Alicia」被拦 ═══════════════════════════════════

def test_gate3_sendpoint_blocks_name_denial(store):
    from src.ai.sendpoint_guard import sendpoint_vocative_pass
    cn.set_contact_names(store, _CID, peer_calls_you="Alicia")
    base = {"call_peer": "babe", "peer_calls_you": "Mizuki", "self_names": ["Mizuki"]}
    names = cn.overlay_sendpoint_names(base, "whatsapp", "acc1", "peer1")
    assert names["peer_calls_you"] == "Alicia" and names["conversation_id"] == _CID
    assert names["call_peer"] == "David"      # 联系人级空 → 显示名，不是人设 babe

    # 否认句整句剥掉，其余正文保留（strip）
    out, meta = sendpoint_vocative_pass("It's Mizuki, not Alicia. How was your day?", names)
    assert "not Alicia" not in out and "Mizuki" not in out
    assert "How was your day?" in out
    assert meta.get("name_denial_action") == "strip"
    assert meta.get("name_denial_hits")

    # 整条都是否认 → 剥空换认领句「It's me, Alicia」
    out2, meta2 = sendpoint_vocative_pass("I'm not Alicia, my name is Mizuki", names)
    assert "not Alicia" not in out2 and "Alicia" in out2
    assert meta2.get("name_denial_action") == "replace"

    out3, meta3 = sendpoint_vocative_pass("我是 Mizuki 不是 Alicia 啦", names)
    assert "不是 Alicia" not in out3 and "Alicia" in out3
    assert meta3.get("name_denial_action") in ("strip", "replace")

    # 干净文本零改动；所有格 / 元语句放行
    clean = "Alicia is what you always call me, I like it 😊"
    assert sendpoint_vocative_pass(clean, names)[0] == clean
    poss = "That's not Alicia's style haha"
    assert sendpoint_vocative_pass(poss, names)[0] == poss


# ══ 4. VDUJX6 回放：人工发图 3 分钟后「是你吗」→ 认领，不出「没这个功能」 ═════

def _seed_manual_outbound_image(store, age_sec: float) -> None:
    now = time.time()
    assert store.ingest_message(InboxMessage(
        platform_msg_id="m_out_img", conversation_id=_CID, direction="out", text="",
        ts=now - age_sec, media_type="image", media_ref="/static/out/manual.jpg"))
    assert store.ingest_message(InboxMessage(
        platform_msg_id="m_in_q", conversation_id=_CID, direction="in",
        text="is that you?", ts=now - 5))


def test_gate4_vdujx6_replay_claims_recent_manual_photo(store, monkeypatch):
    import src.inbox.persona_reply as pr
    from src.utils import persona_guard as pg

    _seed_manual_outbound_image(store, age_sec=180)
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: None)

    rec = ia.recent_outbound_media(_CID, store=store)
    assert rec and rec["source"] == "store" and 170 <= rec["age_sec"] <= 200
    assert ia.is_media_identity_question("is that you?")
    assert ia.is_media_identity_question("这是你吗")
    assert not ia.is_media_identity_question("how are you today")

    # persona_reply 的真实顺序：先 last_media_receipt(cid)（绑会话桥）再 _prompt_addenda
    src_text = open(pr.__file__, encoding="utf-8").read()
    assert src_text.index("last_media_receipt(_cid_q6)") < src_text.index("_add = _prompt_addenda(")
    ia.last_media_receipt(_CID)
    for lang in ("en", "zh"):
        add = pr._prompt_addenda(dict(_PERSONA), None, _CID, lang=lang, inbound="is that you?")
        # Q-6 的「从未发过」拒绝依据不得出现（VDUJX6 就是据此拒绝的）
        assert "You have never sent TA a photo" not in add
        assert "你从未给 TA 发过照片" not in add
        assert ("CLAIM IT" in add) or ("认领" in add)
        assert "没这个功能" in add or "no such feature" in add   # 明令禁止句在 prompt 里以「绝不说」出现
        assert "无可用照片" not in add

    # 自动出图链：「是你吗」是追问不是索图 → 不发第二张（send_fn 绝不被调）
    import asyncio
    monkeypatch.setattr(ia, "resolve_image_autosend_cfg", lambda cfg: {"enabled": True})
    monkeypatch.setattr(
        "src.companion.photo_capability.persona_photos_enabled_by_id", lambda pid: True)

    async def _never_send(*a, **k):
        raise AssertionError("「是你吗」追问不得触发第二张图")

    sent = asyncio.run(ia.run_autosend_image(
        {}, "whatsapp", "acc1", "peer1", "p_q20", "is that you?", None,
        send_fn=_never_send, conv_key=_CID))
    assert sent is False

    # 能力自曝黑名单（deny_ai 同门）
    for bad in ("我没这个功能", "不支持发图", "我这边不能发照片", "只能发文字",
                "I can't send photos", "there's no such feature"):
        assert pg.matches_capability_leak(bad), bad
    for ok in ("我现在不能发照片，在开会", "I can't send a photo right now", "这会儿发不了照片呢"):
        assert not pg.matches_capability_leak(ok), ok


def test_gate4_no_recent_media_keeps_q6_wording(store, monkeypatch):
    """对照：30 分钟外的图不算「刚发」，Q-6 「从未发过」文案与自然拒绝保持原样。"""
    import src.inbox.persona_reply as pr
    _seed_manual_outbound_image(store, age_sec=3 * 3600)
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: None)
    assert ia.recent_outbound_media(_CID, store=store) == {}
    ia.last_media_receipt(_CID)
    add = pr._prompt_addenda(dict(_PERSONA), None, _CID, lang="en", inbound="is that you?")
    assert "You have never sent TA a photo" in add
    assert "CLAIM IT" not in add
