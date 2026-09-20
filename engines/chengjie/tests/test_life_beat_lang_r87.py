# -*- coding: utf-8 -*-
"""R87 #321：昨天说过「JUDE 在市集买了一束花」，今天又当「今天」讲第三次。

结构性根因：``beat_mentioned`` 用中文 bigram 重叠判「真提及」；日语 / 英语会话的出站
文本不是中文 → 永远判不中 → 素材不退役 → stride 窗（默认 3 天）内每天重注入同一条，
LLM 每次都按「今天」讲。按模块「宁多勿漏」口径：语种不同 → 按已聊过记账。
"""
from __future__ import annotations

from datetime import datetime

from src.companion import life_beat_ledger as lbl
from src.companion.deep_persona import format_life_context, pick_life_beat

BEAT = "在市集拿零钱买了一小束花，插在窗台上"
BEAT_EN = "grabbed a small bunch of flowers at the market with loose change"


def test_script_mismatch_detects_non_chinese_replies():
    assert lbl.script_mismatch("市場で小銭で小さな花束を買ったの、窓辺に飾ってる", BEAT)
    assert lbl.script_mismatch("I picked up a tiny bunch of flowers at the market today!", BEAT)
    assert lbl.script_mismatch("오늘 시장에서 작은 꽃다발을 샀어", BEAT)
    assert lbl.script_mismatch("วันนี้ซื้อดอกไม้ที่ตลาดมาค่ะ", BEAT)
    # 中文回复 → 不是语种不同，交回重叠判定
    assert not lbl.script_mismatch("今天在市集用零钱买了一小束花，插窗台上了", BEAT)
    assert not lbl.script_mismatch("你吃饭了吗", BEAT)
    # 素材本身是英文 → 不启用（重叠判定自己能处理）
    assert not lbl.script_mismatch("I got flowers at the market", BEAT_EN)
    # 空回复不算
    assert not lbl.script_mismatch("", BEAT)


def test_mentioned_true_when_reply_language_differs():
    assert lbl.beat_mentioned("市場で小銭で小さな花束を買ったの", BEAT)
    assert lbl.beat_mentioned("Grabbed some flowers at the market with spare change", BEAT)
    # 空回复仍 False
    assert not lbl.beat_mentioned("", BEAT)
    # 中文口径不变：改写命中 / 无关不命中
    assert lbl.beat_mentioned("今儿在市集用零钱买了小束花，插窗台了", BEAT)
    assert not lbl.beat_mentioned("今晚吃什么？我有点饿了", BEAT)


def test_retired_after_japanese_reply_and_not_repicked(tmp_path):
    lbl.set_ledger_path(str(tmp_path / "lb.json"))
    try:
        ck = "whatsapp:acc:81"
        persona = {"id": "shiyi", "life_arc": {"stride_days": 3, "beats": [BEAT]}}
        now = datetime(2026, 9, 12, 3, 45)
        assert pick_life_beat(persona, now, skip_fn=lbl.skip_fn_for(ck)) == BEAT
        reply_ja = "市場で小銭で小さな花束を買ったの"
        if lbl.beat_mentioned(reply_ja, BEAT):
            lbl.record_beat_used(ck, BEAT)
        # 第二天（同 stride 窗）不再选到它
        assert pick_life_beat(persona, datetime(2026, 9, 13, 3, 45), skip_fn=lbl.skip_fn_for(ck)) is None
    finally:
        lbl.set_ledger_path(None)


def test_life_context_tells_model_not_to_retell_as_today():
    s = format_life_context(BEAT)
    assert "今天" in s and "重讲" in s and "前两天" in s
