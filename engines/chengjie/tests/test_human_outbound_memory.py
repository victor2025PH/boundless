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


def test_media_outbound_records_placeholder_no_facts(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "我有个女儿（配文）",
        media_type="image", skill_manager=sm)
    assert res["ok"] is True and res["facts"] == []
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx["last_reply"] == "[图片]"
    res_v = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "", media_type="voice",
        skill_manager=sm)
    assert res_v["ok"] and ctx["last_reply"] == "[语音]"
    assert hom.LOG_KEY not in ctx
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
    for i in range(12):
        hom.record_human_said(ctx, [f"我住在城市{i}"], now=float(i))
    assert len(ctx[hom.LOG_KEY]) == hom._LOG_CAP
    assert hom.record_human_said(ctx, ["我住在城市11", "我住在 城市11"]) == 0


# ── 接线：注入口挂在 skill_manager._inject_self_state（A/B 两线同经此处） ────
def test_skill_manager_inject_self_state_consumes_human_said_note():
    src = Path(__file__).resolve().parents[1] / "src" / "skills" / "skill_manager.py"
    text = src.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"def _inject_self_state\(.*?\n    def ", text, re.S)
    assert m, "_inject_self_state 不存在"
    body = m.group(0)
    assert "human_said_note" in body and "_self_state_block" in body
