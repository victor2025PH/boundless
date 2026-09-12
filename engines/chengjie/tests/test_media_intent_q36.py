# -*- coding: utf-8 -*-
"""Q-36（#313 2JK95C · #318 VV7BRY「Marina」）：客户「提议发图」不被当索图拒 + 入站图描述不夸大。

指令 C 段四条：① 「我发张我的照片给你看？」→ ``offer_media``，处置不出拒发模板词；② 「有你的照片吗」→ 仍
``request_media``；③ 「几个菜」caption → 出站「一桌菜」被软改回「几个菜」；④ 2JK95C / VV7BRY 回放。
另钉：六语词干各 ≥6、request_media 风控语义不弱化、处置句不进 repeat_question_guard / claim_guard /
media_promise、FactGate observation 只认 caption 锚定 + TTL 24h、proactive / 接力摘要注入措辞固定
「TA 发过一张…的照片」、群聊拟稿路径零改动。"""
from __future__ import annotations

import inspect
import json
import time

import pytest

from src.ai import companion_selfie as cs
from src.ai import outbound_promise_guard as opg
from src.ai import outbound_text_guard as otg
from src.companion import fact_gate as fg
from src.companion import photo_capability as pc
from src.inbox import claim_guard as cg
from src.inbox import commitment_guard as cmt
from src.inbox import image_observation as obs
from src.inbox import inbound_enrich as ie
from src.inbox import repeat_question_guard as rq
from src.inbox import risk_grader as rg

CONV = "telegram:8538547216:7332005191"
# 2JK95C 实录（12:39–12:42）
PEER_DISHES = "我还没吃呢，刚做了几个菜，休息一下再吃"
PEER_OFFER = "一会我拍照片给你看呢"
AI_REFUSED = "照片就先不发啦，先陪我聊会儿，我这边正瘫着不想动😄 你一个人做一桌菜，是自己吃还是有人一起？"
CAPTION_DISHES = "餐桌上有几个菜：一盘青菜、一碗汤和一盘炒蛋，旁边放着一碗米饭"
INBOUND_PHOTO = f"[图片内容] {CAPTION_DISHES}"
# VV7BRY：客户只发过一张「水边房子」照片
CAPTION_MARINA = "一栋水边的房子，旁边有码头和几艘船"
INBOUND_MARINA = f"[图片内容] {CAPTION_MARINA}"

REFUSAL_WORDS = ("先不发", "不发照片", "发不了", "不支持发图", "没这个功能", "先陪我聊", "not sending", "can't send")


class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.kv = {}

    def list_recent_messages(self, conv, limit=50):
        return self.rows[-limit:]

    def get_app_setting(self, k, default=""):
        return self.kv.get(k, default)

    def set_app_setting(self, k, v, updated_by=""):
        self.kv[k] = v


# ── ① offer_media：客户提议发自己的图 ─────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我发张我的照片给你看？", "我发张我的给你看？", PEER_OFFER, "看到我发的照片了吗", "要不要看我做的菜",
    "我拍了几张菜发给你看", "这是我拍的照片", "我的照片发给你看",
    "I sent a photo, did you see it?", "Can I send you a picture of me?", "wanna see my dinner?",
    "I'll send you a pic of my place", "here is a photo of me", "did you see the pic I sent",
    "私の写真送るね", "写真送ったよ、見た？", "撮って送るね", "見せようか", "私の写真見たい？",
    "내 사진 보내줄게", "사진 보냈어 봤어?", "찍어서 보내줄게", "내 사진 볼래?",
    "te mando una foto mía", "quieres ver mi foto?", "te mandé una foto, la viste?", "puedo mandarte una foto",
    "te mando uma foto minha", "quer ver minha foto?", "te mandei uma foto, viu?", "posso te mandar uma foto",
])
def test_offer_media_detected_and_not_request(text):
    assert cmt.detect_offer_media(text) == cmt.OFFER_MEDIA_KIND
    assert cmt.detect_request(text) is None, text
    assert cmt.detect_commitment(text) != "media", text
    assert cs.detect_selfie_request(text) is False, text
    assert opg.wants_media(text) == "", text


def test_offer_media_grade_low_and_hint_is_accept_not_refuse():
    g = rg.grade("我发张我的照片给你看？", "in", None, lang="zh", reasons=[], hits=[])
    assert g["category"] == "offer_media" and g["level"] == "low" and g["action"] == "accept_expect"
    assert "offer:media" in g["hits"]
    hint = ie.build_offer_media_hint("我发张我的照片给你看？")
    assert hint and "发来看看" in hint and "接受" in hint
    assert "不用" in hint or "绝不用" in hint          # 明令禁拒发话
    assert ie.build_offer_media_hint("你今天开心吗") == ""
    # 处置 = 接受 + 期待：B 线 Q-2 处置不出拒发模板（handle_inbound → clean），能力关的消毒不改处置句
    got = cmt.handle_inbound("我发张我的照片给你看？", conversation_id=CONV, store=_Store(), persona={}, lang="zh",
                             photos_ok=False)
    assert got["decision"] == "clean" and not got["text"]
    reply = "好呀，发来看看！等着看你做的菜～"
    assert pc.sanitize_no_photo_reply(reply, media_context=bool(opg.wants_media("我发张我的照片给你看？"))) == reply
    assert opg.detect_media_promise(reply) == "" and opg.detect_media_claim(reply, media_context=True) == ""
    assert cmt.apply_claim_rewrites(reply, lang="zh")[1]["action"] == "clean"
    assert not any(w in reply for w in REFUSAL_WORDS)


def test_no_photo_constraint_carries_direction_rule():
    c = pc.no_photo_constraint()
    assert "自己" in c and "发来看看" in c and pc.OFFER_MEDIA_RULE in c
    assert "不是在要你的照片" in pc.NO_PHOTO_PERSONA_LINE


def test_six_language_stems_at_least_six_each():
    for tbl in (cmt._OFFER_MEDIA_ZH, cmt._OFFER_MEDIA_EN, cmt._OFFER_MEDIA_JA, cmt._OFFER_MEDIA_KO,
                cmt._OFFER_MEDIA_ES, cmt._OFFER_MEDIA_PT):
        assert len(tbl) >= 6


# ── ② request_media 风控语义不弱化 ────────────────────────────────────────────

@pytest.mark.parametrize("text", ["有你的照片吗，这么多...", "有你的照片吗", "发张照片给我看看", "send me a pic",
                                  "can I see your face?", "show me yours", "看看你的照片", "got any pics of you?"])
def test_request_media_still_request(text):
    assert cmt.detect_offer_media(text) == ""
    assert cmt.detect_request(text) == "media", text
    g = rg.grade(text, "in", None, lang="", reasons=[], hits=[])
    assert g["category"] == "request_media" and g["level"] == "medium", (text, g)


def test_mixed_and_negated_sentences_are_not_offers_and_other_kinds_untouched():
    assert cmt.detect_offer_media("我发张我的给你看，你也发张你的？") == ""
    assert cmt.detect_offer_media("我不发我的照片") == ""
    assert cmt.detect_offer_media("I won't send you my photo") == ""
    assert cmt.detect_offer_media("写真送って") == "" and cmt.detect_request("写真送って") == "media"
    assert cmt.detect_offer_media("사진 보내줘") == "" and cmt.detect_offer_media("mándame una foto") == ""
    # 其余四类零改动
    assert cmt.detect_request("can you send me some money?") == "money"
    assert cmt.detect_request("what's your number?") == "contact"
    assert cmt.detect_request("can we meet up this weekend?") == "meet"
    assert cmt.detect_request("can I send you a gift? need your address") in ("gift", "contact")
    # 高级表逐字（Q-17 / Q-27 钉子）+ offer_media 是低级、不可覆写
    assert {c["id"] for c in rg.CATEGORIES if c["level"] == "high"} == {"self_harm", "minor", "threat", "money_request", "scam"}
    row = rg.category_def("offer_media")
    assert row["level"] == "low" and row["floor"] == "low" and row["overridable"] is False
    assert "offer_media" not in rg.OVERRIDABLE
    # AI 自提议的 offer-accept 桥不重做：仍在库、语义不变
    assert opg.offer_accepted("好呀", [{"role": "assistant", "content": "要不要看我的照片？"}]) == "image"


# ── 处置句：不进 repeat_question_guard / claim_guard ───────────────────────────

def test_accept_prompt_not_repeat_question_and_not_claim_rewrite():
    now = time.time()
    own = [{"text": "发来看看？", "ts": now - 3600}]
    out, rep = rq.check_repeat_questions("好呀，发来看看？", conversation_id=CONV, lang="zh", own_questions=own, now=now)
    assert rep["action"] == "clean" and out == "好呀，发来看看？"
    out2, rep2 = rq.check_repeat_questions("send it over? I'd love to see it", conversation_id=CONV, lang="en",
                                           own_questions=[{"text": "send it over?", "ts": now - 600}], now=now)
    assert rep2["action"] == "clean"
    # 对照：真重复问句仍删
    out3, rep3 = rq.check_repeat_questions("你在哪个城市呀？", conversation_id=CONV, lang="zh",
                                           own_questions=[{"text": "你住哪个城市？", "ts": now - 600}], now=now)
    assert rep3["action"] in ("strip", "rewrite")
    # claim_guard 误伤保护 ③：处置句带引用短语也不改写（零历史）
    t4, r4 = cg.check_claims("像你说的那几个菜，发来看看", history_texts=[], memory_facts=[], lang="zh")
    assert t4 == "像你说的那几个菜，发来看看" and r4["action"] in ("clean", "pass")
    t5, r5 = cg.check_claims("Like you said about that hiking trail, how was it?", history_texts=[], memory_facts=[], lang="en")
    assert r5["action"] == "rewrite"       # 对照：真无锚引用仍改
    for s in ("好呀，发来看看", "send it over!", "見せて〜", "보여줘", "mándamela", "manda aí"):
        assert cmt.is_media_accept_prompt(s), s
    assert not cmt.is_media_accept_prompt("你在哪个城市？")


# ── ③ caption 原词「几个菜」→ 出站「一桌菜」软改 ───────────────────────────────

def test_quantifier_soft_rewrite_back_to_caption_word():
    out, hits = obs.soften_quantifiers(AI_REFUSED, [CAPTION_DISHES])
    assert "做几个菜" in out and "一桌菜" not in out
    assert hits and hits[0]["from"] == "一桌菜" and hits[0]["to"] == "几个菜"
    out2, hits2 = obs.soften_quantifiers("满桌子好菜啊，太丰盛了", [CAPTION_DISHES])
    assert out2.startswith("几个好菜") and hits2
    # 软改不硬拦：caption 没有该名词原词 → 一字不动
    for t in ("好多年没见了", "我做了一堆事情", "lots of love"):
        assert obs.soften_quantifiers(t, [CAPTION_DISHES]) == (t, [])
    assert obs.soften_quantifiers("你一个人做一桌菜？", []) == ("你一个人做一桌菜？", [])
    # en
    out3, hits3 = obs.soften_quantifiers("Wow, a whole table of dishes for one person?",
                                         ["a wooden table with three dishes: rice, soup and greens"])
    assert "three dishes" in out3 and "whole table" not in out3 and hits3


def test_outbound_text_guard_step_uses_history_caption_and_is_switchable():
    cfg = otg.resolve_cfg(None)
    assert "caption_quantifier" not in cfg           # 默认键面不变（显式配置才进键）
    assert otg.resolve_cfg({"companion": {"outbound_text_guard": {"caption_quantifier": False}}})["caption_quantifier"] is False
    hist = ["晚上吃的啥啊", PEER_DISHES, INBOUND_PHOTO]
    before = otg.guard_stats().get("caption_quantifier", 0)
    out, meta = otg.apply_outbound_text_guard(AI_REFUSED, cfg, user_texts=hist)
    assert "几个菜" in out and "一桌菜" not in out
    assert meta["caption_quantifier_hits"][0]["to"] == "几个菜"
    assert otg.guard_stats()["caption_quantifier"] == before + 1
    out2, meta2 = otg.apply_outbound_text_guard(AI_REFUSED, dict(cfg, caption_quantifier=False), user_texts=hist)
    assert "一桌菜" in out2 and not meta2.get("caption_quantifier_hits")
    out3, _ = otg.apply_outbound_text_guard(AI_REFUSED, cfg, user_texts=["随便聊聊"])
    assert "一桌菜" in out3                              # 无 caption 无锚 → 不动


def test_caption_rule_injected_in_media_block_and_no_sticker_change():
    src = inspect.getsource(__import__("src.ai.ai_client", fromlist=["AIClient"]))
    assert "CAPTION_RULE" in src
    assert "只描述 / 引用画面里" in obs.CAPTION_RULE and "不要当作对方的住处" in obs.CAPTION_RULE
    assert "一桌" in obs.CAPTION_RULE and "满桌" in obs.CAPTION_RULE and "好多" in obs.CAPTION_RULE


# ── FactGate observation + KV TTL 24h ─────────────────────────────────────────

def test_fact_gate_observation_kind_and_ttl():
    assert fg.KIND_OBSERVATION == "observation" and fg.OBSERVATION_TTL_SEC == 24 * 3600
    ok, why = fg.check(CAPTION_MARINA, slot_or_kind=fg.KIND_OBSERVATION, evidence=CAPTION_MARINA,
                       inbound_texts=[{"direction": "in", "text": INBOUND_MARINA}])
    assert ok and why == ""
    # caption 不在入站里 / 出站行 → 不过
    assert fg.check(CAPTION_MARINA, slot_or_kind=fg.KIND_OBSERVATION, evidence=CAPTION_MARINA,
                    inbound_texts=[{"direction": "in", "text": "hello"}]) == (False, fg.REASON_UNANCHORED)
    assert fg.check(CAPTION_MARINA, slot_or_kind=fg.KIND_OBSERVATION, evidence=CAPTION_MARINA,
                    inbound_texts=[{"direction": "out", "text": INBOUND_MARINA}]) == (False, fg.REASON_UNANCHORED)
    assert fg.check(CAPTION_MARINA, slot_or_kind=fg.KIND_OBSERVATION, evidence="",
                    inbound_texts=[INBOUND_MARINA]) == (False, fg.REASON_NO_EVIDENCE)
    # fact 类判据一字不变：evidence 不在客户原话 → unanchored
    assert fg.check("客户住在码头边", slot_or_kind=fg.KIND_FACT, evidence=CAPTION_MARINA,
                    inbound_texts=["你好呀"])[0] is False
    st = _Store()
    t0 = 1_700_000_000.0
    item = obs.note_inbound(INBOUND_MARINA, CONV, inbox_store=st, ts=t0)
    assert item and item["caption"] == CAPTION_MARINA and item["ttl_sec"] == 24 * 3600
    raw = json.loads(st.kv[obs.kv_key(CONV)])
    assert raw["items"][0]["caption"] == CAPTION_MARINA           # KV 含原 caption
    assert [x["caption"] for x in obs.read_observations(CONV, inbox_store=st, now=t0 + 23 * 3600)] == [CAPTION_MARINA]
    assert obs.read_observations(CONV, inbox_store=st, now=t0 + 25 * 3600) == []
    assert obs.observation_captions(CONV, user_texts=[INBOUND_PHOTO], inbox_store=st, now=t0 + 25 * 3600) == [CAPTION_DISHES]
    assert obs.note_inbound("[贴纸内容] 一只猫在笑", CONV, inbox_store=st, ts=t0) is None      # 贴纸不算观察
    assert obs.note_inbound("刚吃完", CONV, inbox_store=st, ts=t0) is None


def test_inbound_enrich_notes_observation(monkeypatch):
    st = _Store()
    monkeypatch.setattr("src.integrations.protocol_bridge.get_inbox_store", lambda: st)
    ctx = {"conversation_id": CONV}
    ie.apply_inbound_enrichments(ctx, text=INBOUND_PHOTO, history=[], reply_lang="zh")
    assert ctx.get("_media_kind") == "image" and ctx.get("_media_desc") == CAPTION_DISHES
    assert [x["caption"] for x in obs.read_observations(CONV, inbox_store=st)] == [CAPTION_DISHES]


# ── ④ 回放 ─────────────────────────────────────────────────────────────────────

def test_replay_2jk95c_offer_then_dishes():
    # 客户「一会我拍照片给你看呢」→ offer_media：不索图、不开 claim 门、不进 request_media、有接受提示
    assert cmt.detect_offer_media(PEER_OFFER) == "offer_media"
    assert cs.detect_selfie_request(PEER_OFFER) is False
    assert opg.wants_media(PEER_OFFER) == ""
    assert cmt.detect_commitment(PEER_OFFER) is None and cmt.detect_request(PEER_OFFER) is None
    g = rg.grade(PEER_OFFER, "in", None, lang="zh", reasons=[], hits=[])
    assert g["category"] == "offer_media" and g["level"] == "low"
    ctx = {"conversation_id": CONV}
    ie.apply_inbound_enrichments(ctx, text=PEER_OFFER, history=[{"role": "user", "content": PEER_DISHES}], reply_lang="zh")
    hint = str(ctx.get("_topic_switch_hint") or "")
    assert "提议发 TA 自己的照片" in hint and "发来看看" in hint
    # 事故出站「…一桌菜…」：有「几个菜」caption 时改回原词
    out, meta = otg.apply_outbound_text_guard(AI_REFUSED, otg.resolve_cfg(None),
                                              user_texts=[PEER_DISHES, INBOUND_PHOTO, PEER_OFFER])
    assert "做几个菜" in out and "一桌菜" not in out


def test_replay_vv7bry_marina_stays_observation():
    from src.inbox.handoff_memory import build_handoff_note
    from src.utils.proactive_variety import format_recent_context
    ctx = format_recent_context([{"direction": "in", "text": INBOUND_MARINA, "ts": 1000.0},
                                 {"direction": "out", "text": "好美", "ts": 1100.0}], now=5000.0)
    assert "TA发过一张照片（识图所见：" in ctx and "[图片内容]" not in ctx
    note = build_handoff_note([{"direction": "in", "text": INBOUND_MARINA, "media_type": "image", "ts": 100.0},
                               {"direction": "out", "text": "好美", "ts": 120.0}], since_ts=50.0, now=200.0)["note"]
    assert obs.OBSERVATION_RULE in note and "住处" in obs.OBSERVATION_RULE
    line = obs.observation_line(CAPTION_MARINA)
    assert line.startswith("TA发过一张照片（识图所见：") and "Marina" not in line
    assert obs.observation_note([CAPTION_MARINA]).startswith("【对方发过的图（识图所见）】")
    # goal 注入：槽位线索只扫客户自己的文字——一张「水边的房子 / 别墅」照片不得把 residence / location 变成 mentioned
    from src.companion.goals.profile_slots import slot_state
    rows = [{"direction": "in", "text": "[图片内容] 一栋海边的别墅，门前有码头，住宅区很安静"}]
    for slot in ("residence", "location"):
        st, why = slot_state(slot, None, rows)
        assert st == "unknown", (slot, st, why)
    st2, why2 = slot_state("residence", None, [{"direction": "in", "text": "我住在海边的别墅"}])
    assert st2 == "mentioned", (st2, why2)                       # 对照：客户自己说的仍算线索
    # 记忆抽取：evidence 落在 caption 的事实按 kind=observation 标注（源码钉 + 门判据）
    sm_src = inspect.getsource(__import__("src.skills.skill_manager", fromlist=["SkillManager"]))
    i0 = sm_src.index("async def _episodic_memory_extract_async")
    body = sm_src[i0: i0 + 12000]
    assert "KIND_OBSERVATION" in body and "observation_line" in body and "[memory] observation" in body
    assert 'slot_or_kind="fact"' in body                               # Q-25 门原样在前
    assert "[caption-guard]" in sm_src and "conversation_id=_q36_cid" in sm_src


# ── 静态钉：接线口径 + 群拟稿路径零改动 ─────────────────────────────────────────

def test_static_pins_and_group_draft_path_untouched():
    src_cmt = inspect.getsource(cmt)
    assert src_cmt.count("not detect_offer_media(t)") == 2            # detect_request + detect_commitment 的 media 各一处
    assert "detect_offer_media" in inspect.getsource(cs.detect_selfie_request)
    assert "detect_offer_media" in inspect.getsource(opg.wants_media)
    assert "detect_offer_media" in inspect.getsource(rg.grade)
    # 群聊拟稿路径本条一行不改（Q-23 归属）
    for mod in ("src.inbox.autodraft_helpers", "src.inbox.peer_bot_guard"):
        s = inspect.getsource(__import__(mod, fromlist=["x"]))
        assert "offer_media" not in s and "image_observation" not in s
