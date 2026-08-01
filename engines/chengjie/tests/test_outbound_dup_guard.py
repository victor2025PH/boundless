# -*- coding: utf-8 -*-
"""出站近重复守卫纯函数门禁（2026-07-31，198 实锤三连发）。

金标取自客户机 inbox.db 实录：
  - 10:07:45 / 10:07:59 / 10:08:28 —— 同一条入站的三条同义改写全部发出
    （共同开头『Haha, that's true.』）；
  - 10:11:00 发出的提问在 10:12:07 被原样再发（客户已回答过）。
守卫语义：dup=原样重复/包含；similar=同义开头改写。两档都只用于「要求坐席确认」，
过短文本（『好的』『嗯嗯』）永不判——重复短语是正常聊天。
"""

from __future__ import annotations

from src.inbox.outbound_dup_guard import (
    dup_guard_metrics_snapshot,
    near_duplicate_of_recent,
    normalize_for_dup,
    record_dup_check,
)

NOW = 1_785_460_000.0


def _out(text, age_sec):
    return {"direction": "out", "text": text, "ts": NOW - age_sec}


def _in(text, age_sec):
    return {"direction": "in", "text": text, "ts": NOW - age_sec}


def test_exact_repeat_question_is_dup():
    """实录：把客户已答过的问题隔 67 秒原样再问 → dup。"""
    q = "What about you, do you usually cook or are you more used to ordering takeout?😄"
    rows = [_out(q, 67), _in("Take out is so expensive now", 30)]
    hit = near_duplicate_of_recent(q, rows, now=NOW)
    assert hit and hit["level"] == "dup"
    assert hit["age_sec"] >= 60


def test_contained_previous_question_is_dup():
    """旧问题被整体拼进新话里再发 → 仍是 dup（containment 判定）。"""
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    new = "Yeah eating out is pricey. " + q
    hit = near_duplicate_of_recent(new, [_out(q, 60)], now=NOW)
    assert hit and hit["level"] == "dup"


def test_paraphrase_variants_are_similar():
    """实录三连发：同义改写共享开头『Haha, that's true.』→ similar（要求确认）。"""
    first = "Haha, that's true. Making new friends when you're bored sounds like a good start, doesn't it?"
    second = "Haha, that's true. No rush, just let things happen naturally."
    hit = near_duplicate_of_recent(second, [_out(first, 14)], now=NOW)
    assert hit and hit["level"] == "similar"


def test_normal_conversation_flow_not_flagged():
    """正常追问/新话题不误伤。"""
    rows = [_out("Nice, 35 is a great age. I'm 41 myself, kind of an old man now haha 😄", 40)]
    assert near_duplicate_of_recent(
        "Haha, you really know how to make someone smile 😄", rows, now=NOW) is None


def test_short_texts_never_flagged():
    """『好的』『嗯嗯』这类口头禅重复是正常聊天，永不判。"""
    rows = [_out("好的", 5), _out("ok!", 5)]
    assert near_duplicate_of_recent("好的", rows, now=NOW) is None
    assert near_duplicate_of_recent("ok!", rows, now=NOW) is None


def test_window_expiry_and_direction_filter():
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    # 超出 180s 窗口 → 放行
    assert near_duplicate_of_recent(q, [_out(q, 300)], now=NOW) is None
    # 入站同文（客户自己的话）不参与比对
    assert near_duplicate_of_recent(q, [_in(q, 10)], now=NOW) is None


def test_defensive_on_bad_rows():
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    rows = [None, {"direction": "out"}, {"direction": "out", "text": q, "ts": "bad"},
            "junk", {"direction": "out", "text": q, "ts": NOW - 5}]
    hit = near_duplicate_of_recent(q, rows, now=NOW)
    assert hit and hit["level"] == "dup"      # 脏行跳过，好行仍命中
    assert near_duplicate_of_recent(q, None, now=NOW) is None


def test_normalize_strips_punct_emoji_space():
    a = normalize_for_dup("Haha, that's true!! 😄")
    b = normalize_for_dup("haha thats true")
    assert a == b


def test_metrics_counters():
    base = dup_guard_metrics_snapshot()
    record_dup_check("dup")
    record_dup_check("similar")
    record_dup_check("")
    record_dup_check("", forced=True)
    snap = dup_guard_metrics_snapshot()
    assert snap["checked"] == base["checked"] + 4
    assert snap["hit_dup"] == base["hit_dup"] + 1
    assert snap["hit_similar"] == base["hit_similar"] + 1
    assert snap["forced"] == base["forced"] + 1
