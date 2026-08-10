# -*- coding: utf-8 -*-
"""主动开场变体守卫纯函数门禁（P0 2026-07-29）。

金标直接取自生产实锤：近 14 天 42 条主动问候里「好久没联系啦，你最近过得
怎么样呀？」级近重复 x3/x2/x2——守卫必须抓得住这批，同时放行真正换了
切入点的开场。
"""

from __future__ import annotations

from src.utils.proactive_variety import (
    format_recent_context,
    most_similar,
    normalize_for_similarity,
    rel_age_label,
    similarity,
    trailing_unanswered_inbound,
    trailing_unanswered_texts,
)

# ── 归一化 / 相似度 ─────────────────────────────────────────────────────

# 生产真实连发样本（同会话不同天发出的"不同"文案）
_PROD_DUPES = [
    "好久没联系啦，你最近过得怎么样呀？",
    "好久没联系啦，最近过得怎么样呀～",
    "好久没联系啦，最近过得怎么样？",
    "嘿，好久没联系了，你最近还好吗？",
]


def test_normalize_strips_punct_emoji_tone():
    # 语气助词（嘿/了/啦/呀…）一并剥掉——换个语气词不算新文案
    assert normalize_for_similarity("嘿，好久没联系了～？！") == "好久没联系"
    assert normalize_for_similarity("Hello, World! 😊") == "helloworld"
    assert normalize_for_similarity("") == ""


def test_production_duplicates_caught():
    """生产实锤的四条近重复互相之间必须全部判相似（默认阈 0.60）。"""
    for i, a in enumerate(_PROD_DUPES):
        for b in _PROD_DUPES[i + 1:]:
            assert similarity(a, b) >= 0.60, (a, b, similarity(a, b))


def test_genuinely_different_opener_passes():
    """真换了切入点的开场必须远低于阈值（校准语料 max=0.242，留裕量带）。"""
    fresh_openers = [
        "刚路过一家新开的咖啡店，突然想到你，改天带你来尝尝",
        "今天上班路上看到只超可爱的柴犬，拍给你看",
        "突然好想吃火锅，你说要不要冲一个",
    ]
    for fresh in fresh_openers:
        for old in _PROD_DUPES:
            assert similarity(fresh, old) < 0.45, (fresh, old)


def test_memory_followup_not_flagged():
    """记忆回访式开场（引用具体事实）不能被误伤。"""
    followup = "上次你说的Arbitrum借贷协议后来怎么样了呀"
    assert most_similar(followup, _PROD_DUPES) is None


def test_most_similar_returns_offender():
    hit = most_similar("好久没联系啦，你最近过得怎么样呢？", _PROD_DUPES,
                       threshold=0.60)
    assert hit is not None and "好久没联系" in hit


def test_most_similar_empty_inputs_safe():
    assert most_similar("", _PROD_DUPES) is None
    assert most_similar("随便说点什么", []) is None
    assert most_similar("x", None) is None


# ── 未回连发尾 ──────────────────────────────────────────────────────────

def _m(direction, text, ts=0.0):
    return {"direction": direction, "text": text, "ts": ts}


def test_trailing_unanswered_collects_out_run():
    msgs = [
        _m("in", "好呀"),
        _m("out", "第一条问候"),
        _m("out", "第二条问候"),
    ]
    assert trailing_unanswered_texts(msgs) == ["第一条问候", "第二条问候"]


def test_trailing_unanswered_stops_at_inbound():
    msgs = [
        _m("out", "更早的未回消息"),
        _m("in", "对方回了"),
        _m("out", "新一轮问候"),
    ]
    # 对方回过话之后的连发才算「本轮未回」
    assert trailing_unanswered_texts(msgs) == ["新一轮问候"]


def test_trailing_unanswered_empty_when_last_is_inbound():
    msgs = [_m("out", "问候"), _m("in", "在的")]
    assert trailing_unanswered_texts(msgs) == []


def test_trailing_unanswered_caps_and_keeps_recent():
    msgs = [_m("in", "hi")] + [_m("out", f"第{i}条") for i in range(1, 6)]
    out = trailing_unanswered_texts(msgs, max_texts=3)
    assert out == ["第3条", "第4条", "第5条"]  # 保最近 3 条


def test_trailing_unanswered_skips_blank_text():
    msgs = [_m("in", "hi"), _m("out", ""), _m("out", "有内容")]
    assert trailing_unanswered_texts(msgs) == ["有内容"]


# ── 相对时间 / 结构化上下文 ─────────────────────────────────────────────

def test_rel_age_label_buckets():
    assert rel_age_label(30) == "刚刚"
    assert rel_age_label(10 * 60) == "10分钟前"
    assert rel_age_label(3 * 3600) == "3小时前"
    assert rel_age_label(30 * 3600) == "昨天"
    assert rel_age_label(5 * 86400) == "5天前"
    assert rel_age_label(-5) == "刚刚"  # 时钟漂移不炸


def test_format_recent_context_labels_direction_and_age():
    now = 1_000_000.0
    msgs = [
        _m("in", "好呀", ts=now - 3 * 3600),
        _m("out", "嘿，好久没联系了", ts=now - 3600),
    ]
    ctx = format_recent_context(msgs, now=now)
    lines = ctx.splitlines()
    assert lines[0].startswith("TA（3小时前）：好呀")
    assert lines[1].startswith("你（1小时前）：嘿，好久没联系了")


def test_format_recent_context_truncates_long_lines():
    now = 1000.0
    msgs = [_m("in", "长" * 200, ts=now)]
    ctx = format_recent_context(msgs, now=now)
    assert len(ctx.splitlines()[0]) < 80
    assert "…" in ctx


def test_format_recent_context_caps_total_and_keeps_recent():
    now = 100_000.0
    # 契约同 store.list_recent_messages：时间升序（最旧在前，最新在尾）
    msgs = [_m("out", f"第{i}条消息" + "内容" * 20, ts=now - (30 - i))
            for i in range(30)]
    ctx = format_recent_context(msgs, now=now, max_lines=8, max_total_chars=300)
    assert len(ctx) <= 300
    assert "第29条消息" in ctx  # 最新的必须保住（从尾部截断）


def test_format_recent_context_empty_safe():
    assert format_recent_context([], now=0.0) == ""
    assert format_recent_context([{"direction": "in", "text": ""}], now=0.0) == ""


# ── 悬空入站话头（P0 2026-08-05：22:27「介绍老公」无人接 → 07:10 通用晨安实锤）──

def test_trailing_inbound_collects_unanswered_run():
    msgs = [
        _m("out", "看来他家亲戚里真有当领导的料啊"),
        _m("in", "我小弟，没关系"),
        _m("in", "以后给你介绍做你老公"),
    ]
    assert trailing_unanswered_inbound(msgs) == [
        "我小弟，没关系", "以后给你介绍做你老公"]


def test_trailing_inbound_empty_when_last_is_outbound():
    msgs = [_m("in", "在吗"), _m("out", "在的呀")]
    assert trailing_unanswered_inbound(msgs) == []


def test_trailing_inbound_stops_at_outbound():
    msgs = [
        _m("in", "更早的那句"),
        _m("out", "我回过这句"),
        _m("in", "新话头"),
    ]
    # 我方回过话之前的入站不算「悬空」，只收本轮
    assert trailing_unanswered_inbound(msgs) == ["新话头"]


def test_trailing_inbound_caps_and_keeps_recent():
    msgs = [_m("out", "x")] + [_m("in", f"第{i}句") for i in range(1, 5)]
    assert trailing_unanswered_inbound(msgs, max_texts=2) == ["第3句", "第4句"]


def test_trailing_inbound_skips_blank_and_empty_input():
    msgs = [_m("out", "x"), _m("in", ""), _m("in", "有内容")]
    assert trailing_unanswered_inbound(msgs) == ["有内容"]
    assert trailing_unanswered_inbound([]) == []
    assert trailing_unanswered_inbound(None) == []
