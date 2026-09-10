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
