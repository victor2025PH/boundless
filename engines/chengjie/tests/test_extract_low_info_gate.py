"""B4（2026-09-11 用量分析）：合并抽取前的相关性闸门。

「hi」「嗯嗯」「哈哈哈」「👍」这类消息此前也带 6k 字 prompt 去问「有什么事实」，返回恒空。
闸门只拦不可能含事实的形态；任何带名字 / 数字 / 实体的短句必须放行（宁多抽不漏记）。
"""
from __future__ import annotations

import asyncio

import pytest

from src.companion.goals import profile_fill as pf


@pytest.mark.parametrize("text", [
    "hi", "Hi!", "hello 😊", "ok", "OK.", "thanks!!", "Thank you 🙏", "good morning",
    "gm", "lol", "hahaha", "嗯", "嗯嗯", "好的", "好的 谢谢", "哈哈哈哈哈", "在吗？",
    "晚安~", "收到", "👍", "😂😂😂", "...", "？", "こんにちは", "안녕하세요", "hola", "ok thanks",
])
def test_low_information_skipped(text):
    assert pf.low_information_message(text) in ("greeting_ack", "no_text"), text


@pytest.mark.parametrize("text", [
    "我叫小明", "叫我 Tom", "I'm Sarah", "my name is Tom", "我是护士", "I have a daughter",
    "明天去杭州", "我 28 岁", "生日 9 月 13 日", "call me at 10", "我住在上海", "hi, I'm Mike",
    "谢谢，我叫阿杰", "good morning, I work at a bank", "在的，我今天在医院值班",
    "ok see you at 8", "@tom 在吗", "我妈妈昨天住院了",
])
def test_informative_messages_pass(text):
    assert pf.low_information_message(text) == "", text


def test_low_information_never_raises():
    assert pf.low_information_message(None) == "no_text"
    assert pf.low_information_message("") == "no_text"
    assert pf.low_information_message(12345) == ""       # 数字视为信息


class _Spy:
    _cb_enabled = False
    _cb_open_until = 0.0

    def __init__(self):
        self.calls = 0

    async def chat(self, prompt):
        self.calls += 1
        return '{"facts":[],"slots":{}}'


def test_extract_facts_and_slots_skips_llm_for_greeting():
    spy = _Spy()
    out = asyncio.run(pf.extract_facts_and_slots(spy, "hi 😊", "Hi! How are you today?", slots=[]))
    assert spy.calls == 0
    assert out["llm"] == 0 and out["skipped"] == "greeting_ack"
    assert out["facts"] == [] and out["slots"] == {}


def test_extract_facts_and_slots_still_calls_llm_for_informative():
    spy = _Spy()
    out = asyncio.run(pf.extract_facts_and_slots(spy, "I'm Sarah, a nurse in Manila", "Nice to meet you Sarah!", slots=[]))
    assert spy.calls == 1
    assert out["llm"] == 1 and "skipped" not in out


def test_informative_tokens_picks_name_number_cjk():
    toks = pf.informative_tokens("I'm Sarah, 28, 住在上海")
    assert "w:sarah" in toks and "n:28" in toks and "c:住在上海" in toks


def test_extract_repeat_skip_no_new_entity_and_daily_cap():
    pf.reset_extract_memo()
    msg = "I'm Sarah, a nurse in Manila"
    assert pf.extract_repeat_skip(msg, conv="wa:1:x") == ""
    pf.remember_extract("wa:1:x", msg, llm=1, now=1_000_000.0)
    assert pf.extract_repeat_skip(msg, conv="wa:1:x", now=1_000_100.0) == "no_new_entity"
    # 新实体必须放行
    assert pf.extract_repeat_skip("I'm Sarah, I have a daughter", conv="wa:1:x",
                                 now=1_000_100.0) == ""
    # 日限额
    pf.reset_extract_memo()
    for i in range(3):
        pf.remember_extract("wa:1:x", f"fact number {i} extra", llm=1, now=2_000_000.0)
    assert pf.extract_repeat_skip("brand new token zulu", conv="wa:1:x",
                                 now=2_000_100.0, daily_cap=3) == "daily_cap"


def test_run_extraction_skips_repeat_without_llm():
    pf.reset_extract_memo()
    cfg = {"companion": {"goals": {"profile_llm": {"enabled": True, "per_conv_daily": 12}}}}
    spy = _Spy()
    first = asyncio.run(pf.run_extraction(
        spy, cfg, None, user_msg="I'm Sarah, a nurse in Manila", reply="hi Sarah",
        platform="wa", chat_key="ck", account_id="a1", conversation_id="wa:a1:ck"))
    assert first["llm"] == 1 and spy.calls == 1
    spy2 = _Spy()
    second = asyncio.run(pf.run_extraction(
        spy2, cfg, None, user_msg="I'm Sarah, a nurse in Manila", reply="ok",
        platform="wa", chat_key="ck", account_id="a1", conversation_id="wa:a1:ck"))
    assert second["llm"] == 0 and second.get("skipped") == "no_new_entity" and spy2.calls == 0
    pf.reset_extract_memo()
