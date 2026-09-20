# -*- coding: utf-8 -*-
"""P2-198：草稿链时间断层提示复活（Phase8 的休眠段）。

`build_time_gap_hint` 一直只被 process_message 链喂 `_turn_gap_sec`；
草稿链（工作台/AutoDraft）恒缺该键 → 「10 天前的旧轮次被当刚才」在拟稿
路径零防护。现 `normalize_history` 透传可选 ts、`apply_inbound_enrichments`
在键缺席时自行从历史推导。
"""
from __future__ import annotations

import time

from src.inbox.inbound_enrich import apply_inbound_enrichments
from src.inbox.persona_reply import normalize_history


NOW = time.time()


def test_gap_hint_fires_from_history_ts():
    """历史带 ts（AutoDraft 链）：上一条入站 10 天前 → 注入时间提示。"""
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="好呀好呀",
        history=[
            {"role": "user", "content": "下次去大阪玩吧", "ts": NOW - 10 * 86400},
            {"role": "assistant", "content": "好啊，一起去！", "ts": NOW - 10 * 86400 + 60},
        ],
        reply_lang="zh",
    )
    hint = ctx.get("_topic_switch_hint") or ""
    assert "时间提示" in hint and "不是刚才" in hint
    assert ctx.get("_turn_gap_sec", 0) > 9 * 86400


def test_gap_hint_silent_for_recent_history():
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="好呀好呀",
        history=[{"role": "user", "content": "在吗", "ts": NOW - 600}],
        reply_lang="zh",
    )
    assert "时间提示" not in (ctx.get("_topic_switch_hint") or "")


def test_gap_hint_silent_without_ts():
    """工作台 DOM 抓取（无 ts）→ 不推导、不注入（行为与旧版一致）。"""
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="好呀好呀",
        history=[{"role": "user", "content": "下次去大阪玩吧"}],
        reply_lang="zh",
    )
    assert "_turn_gap_sec" not in ctx or not ctx["_turn_gap_sec"]
    assert "时间提示" not in (ctx.get("_topic_switch_hint") or "")


def test_explicit_zero_from_process_message_not_overwritten():
    """A 线显式写 0（刚聊过）→ 不从历史重算（尊重上游语义）。"""
    ctx: dict = {"_turn_gap_sec": 0.0}
    apply_inbound_enrichments(
        ctx,
        text="好呀好呀",
        history=[{"role": "user", "content": "旧话", "ts": NOW - 10 * 86400}],
        reply_lang="zh",
    )
    assert ctx["_turn_gap_sec"] == 0.0


def test_current_text_row_excluded_from_gap():
    """历史里含当前这条（store 已落库）→ 跳过它取更早的那条算 gap。"""
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="好呀好呀",
        history=[
            {"role": "user", "content": "下次去大阪玩吧", "ts": NOW - 8 * 86400},
            {"role": "user", "content": "好呀好呀", "ts": NOW - 3},
        ],
        reply_lang="zh",
    )
    assert ctx.get("_turn_gap_sec", 0) > 7 * 86400


def test_normalize_history_carries_ts():
    hist, last_in = normalize_history([
        {"direction": "in", "text": "hi", "ts": 1000.5},
        {"direction": "out", "text": "hello"},
    ])
    assert hist[0].get("ts") == 1000.5
    assert "ts" not in hist[1]
    assert last_in == "hi"
