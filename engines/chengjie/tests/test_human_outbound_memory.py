# -*- coding: utf-8 -*-
"""#177 人工接管期间坐席以人设身份发出的话 → 进人设自述记忆（2026-09-05）。

事故：WhatsApp Olivia ↔ BABY BEAR 人工接管期间坐席替人设说「我有个女儿」「以后
搬过来一起住」，切回全自动后 AI 不记得（记忆钩子只挂 AI 回复之后）。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.inbox import human_outbound_memory as hom
from src.utils.context_store import ContextStore, make_context_key


# ── 抽取：只认高置信第一人称自述，宁漏勿错 ─────────────────────────────────
@pytest.mark.parametrize("text", [
    "我有个女儿，今年五岁了",
    "我家有两只猫",
    "我住在深圳",
    "我今年32岁",
    "我是单身",
    "我的工作是护士",
    "以后你可以搬过来和我一起住",       # 实录：邀请未来同住
    "等我这边安顿好了，你就来我这儿住",
    "I have a daughter, she's five.",
    "I've got two kids",
    "My daughter is starting school next week",
    "I live in Los Angeles",
    "I'm 34 years old",
    "You could move in with me next year",
    "I work as a nurse at the county hospital",
])
def test_extract_hits_first_person_self_facts(text):
    facts = hom.extract_self_facts(text)
    assert facts, text
    # 事实＝原话子句（不改写、不换主语）
    assert all(f in text for f in facts)


@pytest.mark.parametrize("text", [
    "你好",
    "哈哈哈好啊",
    "你有女儿吗？",                     # 疑问
    "她说她有个女儿",                   # 转述
    "我没有孩子",                       # 否定
    "如果我有个女儿就好了",             # 假设
    "我在忙，晚点聊",                   # 裸「我在」不收
    "我喜欢你",                         # 情话不是画像事实
    "Do you have a daughter?",
    "I don't have any kids",
    "She said she has a daughter",
    "I used to live in Boston",
    "I love you so much",
    "ok",
])
def test_extract_rejects_noise_negation_question_hearsay(text):
    assert hom.extract_self_facts(text) == [], text


def test_extract_dedups_and_caps_quote_length():
    text = "我有个女儿。我有个女儿！" + "我住在" + "杭" * 3
    facts = hom.extract_self_facts(text)
    assert len(facts) == 2
    assert all(len(f) <= hom._QUOTE_MAX for f in facts)


# ── 接地护栏：与 AI 侧同口径，事实必须锚定在原话上 ────────────────────────
def test_grounding_rejects_facts_without_lexical_overlap():
    # 抽取器只会产出原话子句，这里模拟「上游改写出一条原话里没有的事实」
    assert hom._grounded(["我有个儿子"], "今天天气不错，出去走走") == []
    kept = hom._grounded(["我有个女儿"], "我有个女儿，今年五岁")
    assert kept == ["我有个女儿"]


def test_grounding_english_human_outbound_kept():
    """J-10 A1：坐席用英文替人设说的话同样过引文级护栏（人工出站也可能是英文）。"""
    body = "I have a daughter, she's 5. Maybe you could move in with me someday"
    facts = hom.extract_self_facts(body)
    assert any("daughter" in f for f in facts)
    kept = hom._grounded(facts, body)
    assert kept == facts
    # 原话里没有的英文事实仍丢
    assert hom._grounded(["I have a son"], body) == []


# ── 端到端：手动发送成功 → ContextStore 持久 → 注入块 ───────────────────────
class _FakeSM:
    """只提供 human_outbound_memory 依赖的四个成员，ContextStore 用真的（tmp）。"""

    def __init__(self, db: Path):
        self._context_store = ContextStore(db_path=db, ttl_days=30)
        self.pushed = []

    def _get_user_context(self, user_id, account_id="", chat_scope=""):
        key = make_context_key(user_id, account_id)
        ctx = self._context_store.get(key)
        ctx["user_id"] = str(user_id)
        ctx["_context_store_key"] = key
        return ctx

    def _push_recent_reply(self, user_context, reply):
        self.pushed.append(reply)
        user_context.setdefault("recent_replies", []).append(reply)


def test_human_outbound_records_fact_with_author_human_and_persists(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244",
        "我有个女儿，以后你可以搬过来和我一起住",
        conversation_id="whatsapp:17345893506:13308422244",
        skill_manager=sm, now=1_757_000_000.0)
    assert res["ok"] is True
    assert len(res["facts"]) >= 1
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    log = ctx[hom.LOG_KEY]
    assert log and all(e["author"] == "human" for e in log)
    assert ctx["last_reply"].startswith("我有个女儿")
    assert ctx["last_reply_time"] == 1_757_000_000.0
    assert sm.pushed  # recent_replies 环也推了

    # 注入块：坐席替人设说过的话必须进 prompt
    note = hom.human_said_note(ctx)
    assert "亲口对 TA 说过" in note and "我有个女儿" in note

    # 持久：换一个 ContextStore 实例（模拟重启）仍读得到
    sm._context_store.close()
    sm2 = _FakeSM(tmp_path / "bot.db")
    ctx2 = sm2._get_user_context("13308422244", account_id="17345893506")
    assert ctx2.get(hom.LOG_KEY) and ctx2[hom.LOG_KEY][0]["fact"].startswith("我有个女儿")
    sm2._context_store.close()


def test_human_outbound_no_fact_text_only_updates_last_reply(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "你好呀", skill_manager=sm)
    assert res["ok"] is True and res["facts"] == []
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["last_reply"] == "你好呀"
    assert hom.LOG_KEY not in ctx
    assert hom.human_said_note(ctx) == ""
    sm._context_store.close()


def test_sent_text_fills_last_reply_but_facts_come_from_original(tmp_path):
    """坐席敲中文、出站翻译成英文发出：防复读环记客户看到的英文，事实抽自中文原文。"""
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "我有个女儿",
        sent_text="I have a daughter", skill_manager=sm)
    assert res["facts"] == ["我有个女儿"]
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["last_reply"] == "I have a daughter"
    sm._context_store.close()


def test_media_outbound_enters_memory_chain(tmp_path):
    """接力记忆 P0-1：手动发图 → last_reply「[图片] 配文」+ 配文抽事实 + 媒体账本 author=human。"""
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "我有个女儿（配文）",
        media_type="image", media_ref="/static/protocol_media/whatsapp/out_a.jpg",
        skill_manager=sm, now=1_757_000_000.0)
    assert res["ok"] is True
    assert any("我有个女儿" in f for f in res["facts"])       # 配文也是以人设身份说的话
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["last_reply"] == "[图片] 我有个女儿（配文）"
    log = ctx["_media_sent_log"]
    assert len(log) == 1 and log[0]["author"] == "human"
    assert log[0]["media_ref"].endswith("out_a.jpg") and log[0]["note"].startswith("[图片]")
    assert ctx.get("_photo_promise_streak") == 0
    # 无 desc 时注入块只列 note，不编画面
    note = hom.human_media_note(ctx)
    assert "发过的图片" in note and "[图片] 我有个女儿" in note and "画面" not in note.split("\n")[1]
    # 空配文 → 只记占位，不抽事实
    res2 = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "", media_type="image",
        media_ref="/static/protocol_media/whatsapp/out_b.jpg", skill_manager=sm)
    assert res2["ok"] and res2["facts"] == [] and ctx["last_reply"] == "[图片]"
    assert len(ctx["_media_sent_log"]) == 2
    # 语音：念的字＝说的话；无字只记占位
    res_v = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "", media_type="voice",
        skill_manager=sm)
    assert res_v["ok"] and ctx["last_reply"] == "[语音]"
    res_v2 = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "我住在深圳", media_type="voice",
        sent_text="I live in Shenzhen", skill_manager=sm)
    assert res_v2["facts"] == ["我住在深圳"]
    assert ctx["last_reply"] == "[语音] I live in Shenzhen"
    assert len(ctx["_media_sent_log"]) == 2   # 语音不进图片账本
    sm._context_store.close()


def test_attach_human_media_desc_backfills_and_persists(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    ref = "/static/protocol_media/whatsapp/out_c.jpg"
    hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "刚拍的", media_type="image",
        media_ref=ref, skill_manager=sm)
    assert hom.attach_human_media_desc(
        sm, "whatsapp", "17345893506", "13308422244", media_ref=ref,
        desc="一位女性在海边微笑，身穿白色连衣裙") is True
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["_media_sent_log"][-1]["desc"].startswith("一位女性在海边")
    note = hom.human_media_note(ctx)
    assert "画面：一位女性在海边" in note and "[图片] 刚拍的" in note
    # 未知 media_ref → False；持久：重开仍在
    assert hom.attach_human_media_desc(
        sm, "whatsapp", "17345893506", "13308422244", media_ref="/nope.jpg", desc="x") is False
    sm._context_store.close()
    sm2 = _FakeSM(tmp_path / "bot.db")
    ctx2 = sm2._get_user_context("13308422244", account_id="17345893506")
    assert ctx2["_media_sent_log"][-1]["desc"].startswith("一位女性在海边")
    sm2._context_store.close()


def test_human_media_note_ignores_ai_rows_and_empty():
    assert hom.human_media_note({}) == ""
    ctx = {"_media_sent_log": [{"ts": 1.0, "note": "[图片] AI 自拍", "scene": "cafe"}]}
    assert hom.human_media_note(ctx) == ""      # AI 发的由 selfie 链的块负责


def test_human_media_note_wording_follows_media_kind():
    """三期：只发过视频 → 「视频/那个视频」；只图 → 「图片/那张图」；混合 → 两者都提。"""
    v = {"ts": 1.0, "note": "[视频] 看看", "author": "human", "desc": "海边跑步"}
    i = {"ts": 2.0, "note": "[图片] 刚拍的", "author": "human"}
    nv = hom.human_media_note({"_media_sent_log": [v]})
    assert "发过的视频（事实）" in nv and "那个视频" in nv and "那张图" not in nv
    ni = hom.human_media_note({"_media_sent_log": [i]})
    assert "发过的图片（事实）" in ni and "那张图" in ni and "视频" not in ni
    nm = hom.human_media_note({"_media_sent_log": [v, i]})
    assert "发过的图片/视频（事实）" in nm and "那张图" in nm and "那个视频" in nm
    assert hom._media_kind_words(0, 0) == ("图片/视频", "你发的照片/那张图/那个视频")


def test_scene_state_media_note_mentions_video_and_human_author():
    sm_src = (Path(__file__).resolve().parents[1] / "src" / "skills" / "skill_manager.py").read_text(
        encoding="utf-8", errors="ignore")
    seg = sm_src[sm_src.index("def _inject_scene_state"):]
    seg = seg[:seg.index("async def _apply_photo_directive")]
    assert 'startswith("[视频]")' in seg and '"照片/视频" if _has_video' in seg
    assert '== "human"' in seg and "坐席替你发的" in seg


def _ingest(store, cid, *, direction, text, media_ref, ts, mid):
    from src.inbox.models import InboxMessage
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id=mid, direction=direction, text=text,
        media_type="image", media_ref=media_ref, ts=ts))


def test_store_append_outbound_media_desc_only_out_rows_idempotent(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "whatsapp:acct:peer"
    ref_out, ref_in = "/static/x/out.jpg", "/static/x/in.jpg"
    _ingest(store, cid, direction="out", text="刚拍的", media_ref=ref_out, ts=100.0, mid="o1")
    _ingest(store, cid, direction="in", text="", media_ref=ref_in, ts=101.0, mid="i1")
    assert store.append_outbound_media_desc(cid, media_ref=ref_out, desc="海边微笑") is True
    assert store.append_outbound_media_desc(cid, media_ref=ref_out, desc="海边微笑") is False
    assert store.append_outbound_media_desc(cid, media_ref=ref_in, desc="不该写入站") is False
    assert store.append_outbound_media_desc(cid, media_ref=ref_out, desc="") is False
    rows = {r["media_ref"]: r["text"] for r in store.list_recent_messages(cid, limit=10)}
    assert rows[ref_out] == "刚拍的\n[图片内容] 海边微笑"
    assert rows[ref_in] == ""
    # 空配文行：直接成为描述行
    _ingest(store, cid, direction="out", text="", media_ref="/static/x/out2.jpg", ts=102.0, mid="o2")
    assert store.append_outbound_media_desc(cid, media_ref="/static/x/out2.jpg", desc="猫") is True
    rows = {r["media_ref"]: r["text"] for r in store.list_recent_messages(cid, limit=10)}
    assert rows["/static/x/out2.jpg"] == "[图片内容] 猫"
    store.close()


def test_normalize_history_labels_outbound_media_with_caption_and_desc():
    from src.inbox.persona_reply import normalize_history
    hist, last_in = normalize_history([
        {"direction": "in", "text": "在干嘛"},
        {"direction": "out", "text": "刚拍的\n[图片内容] 海边微笑", "media_type": "image",
         "media_ref": "/o.jpg"},
        {"direction": "out", "text": "晚安呀", "media_type": "voice", "media_ref": "/v.ogg"},
        {"direction": "out", "text": "[我方发出的图片] 已标", "media_type": "image"},
        {"direction": "in", "text": "好看", "media_type": "image", "media_ref": "/i.jpg"},
    ])
    assert hist[1] == {"role": "assistant", "content": "[我方发出的图片] 刚拍的\n[图片内容] 海边微笑"}
    assert hist[2]["content"] == "[我方发出的语音] 晚安呀"
    assert hist[3]["content"] == "[我方发出的图片] 已标"        # 不重复加标签
    assert hist[4]["role"] == "user" and hist[4]["content"] == "好看"   # 入站 caption 原样
    assert last_in == "好看"


@pytest.mark.asyncio
async def test_describe_and_attach_outbound_media_end_to_end(tmp_path, monkeypatch):
    from src.inbox import media_enrich
    from src.inbox.store import InboxStore

    async def _fake_desc(**kw):
        assert kw["media_type"] == "image" and kw["local_path"] == "/tmp/out.jpg"
        return "一只橘猫趴在沙发上"

    monkeypatch.setattr(media_enrich, "describe_outbound_media", _fake_desc)
    store = InboxStore(tmp_path / "inbox.db")
    sm = _FakeSM(tmp_path / "bot.db")
    cid, ref = "whatsapp:17345893506:13308422244", "/static/x/out.jpg"
    _ingest(store, cid, direction="out", text="", media_ref=ref, ts=1.0, mid="o1")
    hom.on_human_outbound("whatsapp", "17345893506", "13308422244", "", media_type="image",
                          media_ref=ref, skill_manager=sm)
    st = SimpleNamespace(skill_manager=sm, inbox_store=store,
                         config_manager=SimpleNamespace(config={"vision": {"enabled": True}}))
    desc = await hom.describe_and_attach_outbound_media(
        st, "whatsapp", "17345893506", "13308422244", conversation_id=cid,
        media_type="image", media_ref=ref, local_path="/tmp/out.jpg", retry_delay_sec=0)
    assert desc == "一只橘猫趴在沙发上"
    row = store.list_recent_messages(cid, limit=5)[-1]
    assert row["text"] == "[图片内容] 一只橘猫趴在沙发上"
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["_media_sent_log"][-1]["desc"] == "一只橘猫趴在沙发上"
    store.close()
    sm._context_store.close()


@pytest.mark.asyncio
async def test_describe_outbound_media_skips_non_image_and_disabled(tmp_path):
    from src.inbox.media_enrich import describe_outbound_media
    assert await describe_outbound_media(media_type="voice", media_ref="/x.ogg") == ""
    assert await describe_outbound_media(media_type="sticker", media_ref="/x.webp") == ""
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 16)
    # vision.enabled 缺省关 → 不调 VLM、返回空串
    assert await describe_outbound_media(
        media_type="image", media_ref=str(p), local_path=str(p), config={}) == ""


@pytest.mark.asyncio
async def test_describe_outbound_media_video_uses_video_chain_and_gate(tmp_path, monkeypatch):
    """二期：出站视频复用入站视频识别层（抽帧 + 音轨）；可用配置关掉。"""
    from src.inbox import media_enrich
    from src.inbox.media_enrich import describe_outbound_media, outbound_desc_marker
    calls = []

    async def _fake_video(path, cfg, vtr):
        calls.append(path)
        return "一段海边散步的视频，\n背景有海浪声"

    monkeypatch.setattr(media_enrich, "_understand_video", _fake_video)
    monkeypatch.setattr(media_enrich, "lazy_voice_transcriber", lambda cfg: None)
    p = tmp_path / "a.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 16)
    desc = await describe_outbound_media(
        media_type="video", media_ref=str(p), local_path=str(p), config={"vision": {"enabled": True}})
    assert desc.startswith("一段海边散步的视频") and "\n" not in desc and calls == [str(p)]
    # 开关关 → 不识别
    assert await describe_outbound_media(
        media_type="video", media_ref=str(p), local_path=str(p),
        config={"inbox": {"handoff_memory": {"outbound_video_desc": False}}}) == ""
    assert len(calls) == 1
    assert outbound_desc_marker("video") == "[视频内容]" and outbound_desc_marker("image") == "[图片内容]"
    assert outbound_desc_marker("voice") == "" and outbound_desc_marker("sticker") == ""


@pytest.mark.asyncio
async def test_describe_and_attach_outbound_video_writes_video_marker(tmp_path, monkeypatch):
    from src.inbox import media_enrich
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore

    async def _fake_desc(**kw):
        assert kw["media_type"] == "video"
        return "海边散步"

    monkeypatch.setattr(media_enrich, "describe_outbound_media", _fake_desc)
    store = InboxStore(tmp_path / "inbox.db")
    sm = _FakeSM(tmp_path / "bot.db")
    cid, ref = "whatsapp:17345893506:13308422244", "/static/x/out.mp4"
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="v1", direction="out",
                                      text="看看这个", media_type="video", media_ref=ref, ts=1.0))
    hom.on_human_outbound("whatsapp", "17345893506", "13308422244", "看看这个", media_type="video",
                          media_ref=ref, skill_manager=sm)
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["last_reply"] == "[视频] 看看这个"
    assert ctx["_media_sent_log"][-1]["author"] == "human" and ctx["_media_sent_log"][-1]["note"] == "[视频] 看看这个"
    st = SimpleNamespace(skill_manager=sm, inbox_store=store)
    desc = await hom.describe_and_attach_outbound_media(
        st, "whatsapp", "17345893506", "13308422244", conversation_id=cid,
        media_type="video", media_ref=ref, local_path="/tmp/out.mp4", retry_delay_sec=0)
    assert desc == "海边散步"
    row = store.list_recent_messages(cid, limit=5)[-1]
    assert row["text"] == "看看这个\n[视频内容] 海边散步"
    assert ctx["_media_sent_log"][-1]["desc"] == "海边散步"
    assert "画面：海边散步" in hom.human_media_note(ctx)
    # 历史标签：出站视频带内容
    from src.inbox.persona_reply import normalize_history
    hist, _ = normalize_history([dict(row)])
    assert hist[0]["content"] == "[我方发出的视频] 看看这个\n[视频内容] 海边散步"
    # 语音：无描述标记 → 不回写
    assert await hom.describe_and_attach_outbound_media(
        st, "whatsapp", "17345893506", "13308422244", conversation_id=cid,
        media_type="voice", media_ref="/v.ogg", retry_delay_sec=0) == ""
    store.close()
    sm._context_store.close()


def test_default_account_id_resolved_from_conversation_id(tmp_path):
    """记忆键必须与 B 线 generate_inbox_draft 同桶（account:chat_key）。"""
    sm = _FakeSM(tmp_path / "bot.db")
    hom.on_human_outbound(
        "whatsapp", "default", "13308422244", "我有个女儿",
        conversation_id="whatsapp:17345893506:13308422244", skill_manager=sm)
    assert "17345893506:13308422244" in sm._context_store._cache
    assert "13308422244" not in sm._context_store._cache
    sm._context_store.close()


def test_never_raises_and_skips_without_skill_manager():
    assert hom.on_human_outbound("whatsapp", "a", "b", "我有个女儿")["reason"] == "no_skill_manager"
    assert hom.on_human_outbound(
        "whatsapp", "a", "b", "我有个女儿", skill_manager=object())["reason"] == "no_context_store"

    class _Boom:
        _context_store = object()

        def _get_user_context(self, *a, **k):
            raise RuntimeError("boom")

    assert hom.on_human_outbound(
        "whatsapp", "a", "b", "我有个女儿", skill_manager=_Boom())["reason"] == "error"


def test_record_human_outbound_resolves_skill_manager_from_app_state(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    # 直挂
    st = SimpleNamespace(skill_manager=sm)
    assert hom.record_human_outbound(
        st, "whatsapp", "17345893506", "13308422244", "我有个女儿")["ok"]
    # 经 telegram_client 暴露（unified_inbox_services._skill_manager 同路径）
    st2 = SimpleNamespace(telegram_client=SimpleNamespace(skill_manager=sm))
    assert hom.record_human_outbound(
        st2, "whatsapp", "17345893506", "13308422244", "我住在深圳")["ok"]
    # 两者都没 → 跳过不抛
    assert hom.record_human_outbound(
        SimpleNamespace(), "whatsapp", "a", "b", "x")["reason"] == "no_skill_manager"
    sm._context_store.close()


def test_record_human_said_caps_and_dedups():
    ctx = {}
    for i in range(hom._LOG_CAP + 4):
        hom.record_human_said(ctx, [f"我住在城市{i}"], now=float(i))
    assert len(ctx[hom.LOG_KEY]) == hom._LOG_CAP
    assert ctx[hom.LOG_KEY][0]["fact"] == "我住在城市4"                 # 挤掉最老的
    last = f"我住在城市{hom._LOG_CAP + 3}"
    assert hom.record_human_said(ctx, [last, last.replace("城市", " 城市")]) == 0
    # 注入块只列最近 _NOTE_MAX_LINES 条
    note = hom.human_said_note(ctx)
    assert note.count("\n- ") == hom._NOTE_MAX_LINES and last in note and "我住在城市4" not in note


@pytest.mark.parametrize("text", [
    "我是做外贸的",
    "我在一家医院上班",
    "我开了一家咖啡店",
    "我是一名护士",
    "我叫林晓",
    "你可以叫我小雨",
    "叫我阿杰就行",
    "我生日是3月8号",
    "我的生日在十月十二日",
    "我是天蝎座",
    "我属马的",
    "I work at a small clinic downtown",
    "I work for Amazon",
    "I work in Marketing",
    "I'm a nurse",
    "My name is Olivia",
    "You can call me Liv",
    "My birthday is on the 8th of March",
    "I'm a Scorpio",
])
def test_extract_phase2_identity_job_birthday(text):
    facts = hom.extract_self_facts(text)
    assert facts, text
    assert all(f in text for f in facts)


@pytest.mark.parametrize("text", [
    "你叫我干嘛",                       # 不是取名
    "我叫你起床你不起",                 # 我叫你…
    "我叫了外卖",
    "我是做什么的你猜",
    "我开了个玩笑",
    "我在忙上班的事",                    # 「在」后接的不是单位
    "I work in the morning",
    "call me later",
    "My name is not important",         # 否定
    "Is your name Olivia?",
])
def test_extract_phase2_rejects_lookalikes(text):
    assert hom.extract_self_facts(text) == [], text


# ── 接线：注入口挂在 skill_manager._inject_self_state（A/B 两线同经此处） ────
def test_skill_manager_inject_self_state_consumes_human_said_note():
    src = Path(__file__).resolve().parents[1] / "src" / "skills" / "skill_manager.py"
    text = src.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"def _inject_self_state\(.*?\n    def ", text, re.S)
    assert m, "_inject_self_state 不存在"
    body = m.group(0)
    assert "human_said_note" in body and "_self_state_block" in body
