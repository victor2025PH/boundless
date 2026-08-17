# -*- coding: utf-8 -*-
"""语音短问候 P0 三件套门禁（2026-08-05，晨安语音「太 AI」实锤修复）。

实锤样本：daily_ritual 产出「早安，昨晚睡得还好吗。」——
① 句号收尾让 TTS 把疑问句念成平调宣告（hub 的 polish 只会剥掉句号，
   问调信息就此丢失）→ clean_text_for_tts 先把「吗。」还原「吗？」；
② colloquial.min_chars 从 12 降到 6 后，短问候进入规则档改写范围——
   句首跳过表必须认得「早安/晚安」，否则兜底路径会产出「话说，早安」。
"""

from __future__ import annotations

from src.ai.tts_pipeline import clean_text_for_tts, polish_hub_speak_text
from src.ai.voice_colloquial import colloquialize


# ── ① 疑问语调还原（clean_text_for_tts，所有 TTS 后端共用收口点）─────────────

def test_question_particle_period_becomes_question_mark():
    assert clean_text_for_tts("早安，昨晚睡得还好吗。") == "早安，昨晚睡得还好吗？"


def test_mid_sentence_question_also_restored():
    assert clean_text_for_tts("你吃了吗。我先吃了。") == "你吃了吗？我先吃了。"


def test_ba_ne_deliberately_untouched():
    # 「吧/呢」语义两可（「走吧。」是祈使不是疑问），刻意不动
    assert clean_text_for_tts("那就走吧。") == "那就走吧。"
    assert clean_text_for_tts("我在想呢。") == "我在想呢。"


def test_existing_question_mark_unchanged():
    assert clean_text_for_tts("昨晚睡得还好吗？") == "昨晚睡得还好吗？"


def test_latin_text_unaffected():
    assert clean_text_for_tts("OK. See you.") == "OK. See you."


# ── ② hub 送稿清洗保住问号（polish 剥句末「。！」但问号=真疑问必须留）─────────

def test_polish_hub_keeps_question_mark():
    assert polish_hub_speak_text("早安，昨晚睡得还好吗？").endswith("吗？")


def test_clean_then_polish_pipeline_preserves_intonation():
    # 完整链：LLM 写「吗。」→ clean 还原「吗？」→ hub polish 不得再丢
    out = polish_hub_speak_text(clean_text_for_tts("早安，昨晚睡得还好吗。"))
    assert out.endswith("吗？")


# ── ③ 问候句首保护（min_chars 6 放量后规则档不得在问候前加迟疑词）────────────

def test_greeting_lead_not_injected():
    for text in ("早安，昨晚睡得还好吗？", "晚安，做个好梦", "早上好呀今天降温了"):
        out = colloquialize(text, "warm", min_chars=6, lead_prob=1.0)
        assert out == text, f"问候开场被改写: {text!r} -> {out!r}"


def test_short_noop_floor_still_respected():
    # min_chars 之下仍是 no-op（保预渲染命中：库存台词「早安呀」3 字）
    assert colloquialize("早安呀", "warm", min_chars=6) == "早安呀"


# ── ④ 方言词汇档（P1 文本层方言 MVP：注入走 style 通道，LLM 缓存键天然隔离）──

def test_dialect_style_line_known_and_unknown():
    from src.ai.voice_colloquial import dialect_style_line
    line = dialect_style_line("chuanyu")
    assert "川渝" in line and "巴适" in line and "绝不堆砌" in line
    assert dialect_style_line("") == ""
    assert dialect_style_line("mars") == ""      # 未知档＝零行为变化


def test_build_voice_style_hint_appends_dialect():
    from src.ai.voice_colloquial import build_voice_style_hint
    s = build_voice_style_hint("温柔", "", dialect="chuanyu")
    assert "声线底色：温柔" in s and "川渝" in s
    s2 = build_voice_style_hint("温柔", "")
    assert "川渝" not in s2                       # 缺省不带（向后兼容）


def test_dialect_packs_mandarin_pronounceable():
    """词表准入红线：不许收粤语书写形等普通话 TTS 念不顺的词（粤语走声学层）。"""
    from src.ai.voice_colloquial import _DIALECT_PACKS
    banned = ("系咯", "唔", "嘅", "冇", "咁")
    for pack in _DIALECT_PACKS.values():
        for b in banned:
            assert b not in pack["words"], (pack["label"], b)


# ── ⑤ auto_stock 拒收仪式问候（每天该不重样的话不能固化成同一段波形）─────────

def test_auto_stock_rejects_ritual_greetings():
    from src.ai.voice_prerender import qualify_auto_stock
    for line in ("早安呀宝", "晚安，做个好梦", "昨晚睡得好吗", "早上好呀"):
        ok, reason = qualify_auto_stock(line, 9)
        assert not ok and reason == "ritual_greeting", line


def test_auto_stock_still_accepts_daily_short_lines():
    from src.ai.voice_prerender import qualify_auto_stock
    for line in ("想你啦", "在忙什么呀", "到家跟我说哦"):
        ok, reason = qualify_auto_stock(line, 9)
        assert ok, (line, reason)
