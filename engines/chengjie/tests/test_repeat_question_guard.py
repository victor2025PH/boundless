# -*- coding: utf-8 -*-
"""Q-1 C（#264 #270）：出站重复提问守卫——24h 内我方问过的事不再问。"""
from __future__ import annotations

import logging
import time

from src.inbox.repeat_question_guard import (
    check_repeat_questions,
    is_question,
    normalize_question,
    own_questions_for,
    similarity,
)

NOW = time.mktime((2026, 9, 9, 3, 0, 0, 0, 0, -1))


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, limit=60):
        return list(self.rows)[-limit:]


def test_similarity_same_slot_is_one_and_paraphrase_detected():
    assert similarity("What do you do for work?", "So what kind of work do you do?") == 1.0
    assert similarity("Which city are you in?", "Where are you based these days?") == 1.0
    assert similarity("How old are you btw?", "What's your age?") == 1.0
    # 同义折叠 + 词干：非槽位问句也能对上
    assert similarity("Do you like hiking on weekends?", "You like hiking on the weekend?") >= 0.8
    # 不同话题不误伤
    assert similarity("What do you do for work?", "Did you sleep well?") < 0.5
    assert similarity("你是做什么工作的呀？", "你平时做哪一行的？") == 1.0
    toks, tags = normalize_question("你在哪个城市呀？", "zh")
    assert "slot:location" in tags


def test_is_question_variants():
    assert is_question("What do you do for work?")
    assert is_question("你那边现在几点呀")
    assert is_question("Where do you live these days")     # 漏问号但疑问词起头
    assert not is_question("Long day today.")
    assert not is_question("")


def test_own_questions_window_and_direction():
    rows = [
        {"direction": "out", "text": "Hey! What do you do for work?", "ts": NOW - 3600},
        {"direction": "in", "text": "what about you?", "ts": NOW - 3500},          # 客户问的不算
        {"direction": "out", "text": "Which city are you in?", "ts": NOW - 30 * 3600},  # 超 24h
        {"direction": "out", "text": "Nice. Sleep well!", "ts": NOW - 600},          # 非问句
    ]
    own = own_questions_for(_Store(rows), "c1", now=NOW)
    assert [q["text"] for q in own] == ["What do you do for work?"]
    assert own_questions_for(None, "c1") == [] and own_questions_for(_Store(rows), "") == []


def test_check_strips_repeated_question_keeps_rest(caplog):
    own = [{"text": "What do you do for work?", "ts": NOW - 3600}]
    with caplog.at_level(logging.INFO, logger="src.inbox.repeat_question_guard"):
        out, rep = check_repeat_questions(
            "Haha that sounds exhausting. So what kind of work do you do? Hope you get some rest.",
            conversation_id="c1", lang="en", own_questions=own, now=NOW)
    assert rep["action"] == "strip" and rep["stripped"] == 1
    assert "what kind of work" not in out.lower()
    assert out.startswith("Haha that sounds exhausting.") and "Hope you get some rest." in out
    assert any("[repeat-q] conv=c1" in r.getMessage() and "action=strip" in r.getMessage()
               for r in caplog.records)


def test_check_rewrites_when_only_repeated_question_remains():
    own = [{"text": "你是做什么工作的呀？", "ts": NOW - 7200}]
    out, rep = check_repeat_questions("你平时做哪一行的？", conversation_id="c2", lang="zh",
                                      own_questions=own, now=NOW)
    assert rep["action"] == "rewrite" and out and "？" not in out and "?" not in out


def test_check_clean_when_new_question_or_no_history():
    own = [{"text": "What do you do for work?", "ts": NOW - 3600}]
    txt = "That's wild. Did you sleep okay?"
    out, rep = check_repeat_questions(txt, conversation_id="c3", lang="en", own_questions=own, now=NOW)
    assert out == txt and rep["action"] == "clean"
    out2, rep2 = check_repeat_questions("What do you do for work?", conversation_id="c3",
                                        lang="en", own_questions=[], now=NOW)
    assert out2 == "What do you do for work?" and rep2["action"] == "clean"


def test_check_reads_store_when_own_not_given():
    rows = [{"direction": "out", "text": "Btw what do you do for work?", "ts": NOW - 1800}]
    out, rep = check_repeat_questions("Cool cool. What's your line of work?", conversation_id="c4",
                                      lang="en", store=_Store(rows), now=NOW)
    assert rep["action"] == "strip" and out == "Cool cool."


def test_hooked_in_draft_humanize_before_claim_guard():
    import inspect
    from src.inbox import outbound_humanize
    src = inspect.getsource(outbound_humanize.apply_draft_humanize)
    assert src.index("check_repeat_questions(") < src.index("check_claims(")
    out, meta = outbound_humanize.apply_draft_humanize(
        "Hey. What do you do for work?", conversation_id="", lang="en", origin="auto")
    assert meta.get("repeat_q") == "clean" and "work" in (out or "")
