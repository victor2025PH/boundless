"""口语化改写层门禁 — src/ai/voice_colloquial.py。

守住活人感 P0 的关键不变量：
  - 短句 no-op（保预渲染命中零损耗）
  - 非中文文本跳过（防中文口语词 garble 外语，与「中文声纹念外语」同族红线）
  - 确定性（同文本同结果 = TTS 缓存/预渲染键安全）
  - 情绪门控（庄重情绪只做中性词替换；sad/empathetic 句首让位副语言标记）
  - neutral 也改写（日常对话主路，最需要文字层活人感）
  - 防御式（空/脏输入 → 原文，绝不抛）
"""
from __future__ import annotations

import pytest

from src.ai.avatar_voice_stats import get_avatar_voice_stats
from src.ai.voice_colloquial import (
    _is_chinese_dominant,
    _lexical_swap,
    build_voice_style_hint,
    colloquialize,
    normalize_colloquial_emotion,
    parse_persona_lead_phrases,
)
from src.ai.voice_emotion import EmotionSpec

# 一组中文长句（≥12 字），用于统计口语化命中（crc32 固定 → 结果确定）。
_ZH_LONG = [
    "我今天其实过得挺充实的但是有点累想早点休息",
    "你说的这个方案目前看起来非常不错我们可以立即开始推进",
    "如果你愿意的话我们明天一起去公园走走然后吃个饭",
    "这件事情因此变得复杂起来我需要再仔细想一想才行",
    "谢谢你一直陪着我说这些话让我心里舒服了很多呢",
    "刚刚发生的事情让我十分惊讶我到现在都还没有缓过来",
    "倘若明天不下雨的话我们就按原计划出发去海边玩吧",
    "我一直在想你最近工作是不是特别忙都没怎么联系我",
]


def test_short_text_noop_protects_prerender():
    """短句（<min_chars）原样返回——预渲染库存短句，no-op 保命中率零损耗。"""
    for s in ("好的呀", "早安", "在的在的", "想我了吗"):
        assert colloquialize(s, EmotionSpec("warm"), lead_prob=1.0) == s


def test_defensive_empty_and_dirty():
    """空/None/非字符串 → 安全返回，绝不抛。"""
    assert colloquialize("", EmotionSpec("warm")) == ""
    assert colloquialize(None, EmotionSpec("warm")) == ""  # type: ignore[arg-type]
    # spec 脏输入也不炸
    assert colloquialize("我今天其实过得挺充实的但是有点累", spec=12345) is not None


def test_non_chinese_text_skipped():
    """非中文文本跳过——中文口语词/迟疑词会 garble 外语（安全红线）。"""
    en = "I had a really long and productive day today thanks for asking"
    assert colloquialize(en, EmotionSpec("warm"), lead_prob=1.0) == en
    # 日文（含假名）→ 判非中文，不动
    ja = "今日はとても忙しかったですでも楽しかったですありがとう"
    assert colloquialize(ja, EmotionSpec("warm"), lead_prob=1.0) == ja
    # 韩文谚文 → 不动
    ko = "오늘 정말 바빴어요 그래도 즐거웠어요 고마워요 진짜로"
    assert colloquialize(ko, EmotionSpec("warm"), lead_prob=1.0) == ko


def test_is_chinese_dominant():
    assert _is_chinese_dominant("我今天很开心啊")
    assert _is_chinese_dominant("我今天很happy其实还行")  # 中英混合、中文为主
    assert not _is_chinese_dominant("hello world this is english")
    assert not _is_chinese_dominant("今日はいい天気")       # 含假名
    assert not _is_chinese_dominant("12345 !!! ???")        # 无字母


def test_lexical_safe_swap_any_emotion():
    """SAFE 词替换任何情绪都做（含庄重）：因此→所以、但是→不过、目前→现在。"""
    for emo in ("neutral", "warm", "serious", "apologetic"):
        out = colloquialize(
            "这件事情因此变得复杂但是我们目前还能应付得过来的",
            EmotionSpec(emo), enable_fillers=False)
        assert "因此" not in out and "所以" in out
        assert "但是" not in out and "不过" in out
        assert "目前" not in out and "现在" in out


def test_lexical_casual_only_informal():
    """CASUAL 词替换仅非正式情绪：如果→要是、非常→特别；serious 保持书面。"""
    src = "如果你愿意的话这个方案其实非常不错我们可以试试看效果"
    warm = colloquialize(src, EmotionSpec("warm"), enable_fillers=False)
    assert "要是" in warm and "特别" in warm and "如果" not in warm
    serious = colloquialize(src, EmotionSpec("serious"), enable_fillers=False)
    assert "如果" in serious and "非常" in serious  # 庄重保持书面


def test_deterministic():
    """同文本 + 同参数 → 同结果（TTS 缓存/预渲染键安全）。"""
    for s in _ZH_LONG:
        a = colloquialize(s, EmotionSpec("warm"), lead_prob=0.5)
        b = colloquialize(s, EmotionSpec("warm"), lead_prob=0.5)
        assert a == b


def test_lead_filler_added_for_casual_emotion():
    """非正式情绪 + lead_prob=1.0：一批长句里应有相当比例被加句首迟疑词，
    且加的都是合法 filler 变体 + 逗号。"""
    from src.ai.voice_colloquial import _LEAD_FILLERS

    hits = 0
    for s in _ZH_LONG:
        out = colloquialize(s, EmotionSpec("playful"), enable_lexical=False,
                            lead_prob=1.0)
        if out != s:
            hits += 1
            head = out.split("，", 1)[0]
            assert head in _LEAD_FILLERS["playful"], f"非法句首词: {head}"
    assert hits >= len(_ZH_LONG) // 2  # 95% 概率下大多数应命中


def test_formal_emotion_no_filler():
    """庄重情绪（serious/apologetic）即使 lead_prob=1.0 也不加句首迟疑词。"""
    for emo in ("serious", "apologetic"):
        for s in _ZH_LONG:
            out = colloquialize(s, EmotionSpec(emo), enable_lexical=False,
                                lead_prob=1.0)
            assert out == s, f"{emo} 不应加句首词: {out}"


def test_sad_empathetic_no_lead_filler():
    """sad/empathetic 句首让位给副语言标记 [sigh]，口语化不加句首迟疑词。"""
    for emo in ("sad", "empathetic"):
        for s in _ZH_LONG:
            out = colloquialize(s, EmotionSpec(emo), enable_lexical=False,
                                lead_prob=1.0)
            assert out == s, f"{emo} 句首应让位副语言: {out}"


def test_neutral_is_rewritten():
    """neutral 也改写——日常对话主路走 neutral 保真路径，最需文字层活人感。"""
    # 词替换在 neutral 下生效
    out = colloquialize("这件事情因此拖了很久但是最终还是解决掉了呢",
                        EmotionSpec("neutral"), enable_fillers=False)
    assert "所以" in out and "不过" in out


def test_sentence_final_default_off():
    """句末语气助词默认关（最易做作）——不显式开启则不加。"""
    src = "我今天真的过得特别开心谢谢你陪着我聊了这么久"
    out = colloquialize(src, EmotionSpec("warm"), enable_fillers=False,
                        enable_sentence_final=False)
    assert out == _lexical_swap(src, casual=True)  # 只有词替换，无句末助词


def test_sentence_final_skips_question():
    """开句末助词时，疑问/感叹句不软化（语气助词会削弱原语气）。"""
    q = "你今天过得怎么样呀最近工作是不是特别忙啊？"
    out = colloquialize(q, EmotionSpec("warm"), enable_fillers=False,
                        enable_sentence_final=True, final_prob=1.0)
    # 疑问句末尾不应被追加额外语气助词（结尾仍是 ？）
    assert out.rstrip().endswith("？")


def test_max_inserts_zero_still_lexical():
    """max_inserts=0：不加任何词（句首/句末），但等义词替换仍生效。"""
    src = "如果可以的话我们目前就立即开始这个计划因此不用再等了"
    out = colloquialize(src, EmotionSpec("playful"), max_inserts=0, lead_prob=1.0)
    # 词替换生效
    assert "现在" in out and "马上" in out and "所以" in out
    # 没有加句首迟疑词（结果就是纯词替换版）
    assert out == _lexical_swap(src, casual=True)


def test_normalize_colloquial_emotion():
    assert normalize_colloquial_emotion(None) == ("neutral", 0.6)
    assert normalize_colloquial_emotion("Warm")[0] == "warm"
    e, i = normalize_colloquial_emotion(EmotionSpec("playful", intensity=0.9))
    assert e == "playful" and i == pytest.approx(0.9)


def test_does_not_mutate_core_semantics():
    """改写不丢原文核心内容（宽松：去掉可能的句首 filler 后仍含原文尾部）。"""
    src = "我一直在想你最近工作是不是特别忙都没怎么联系我了"
    out = colloquialize(src, EmotionSpec("warm"), lead_prob=1.0)
    # 原文尾部关键片段仍在（词替换只换连接词，不动主体）
    assert "都没怎么联系我" in out


def test_fillers_disabled_still_lexical():
    """#6：分条非首条 enable_fillers=False → 不加句首迟疑词，但等义词替换仍生效。"""
    src = "如果可以的话我们目前就立即开始这个计划因此不用再等了呢"
    out = colloquialize(src, EmotionSpec("playful"), enable_fillers=False,
                        lead_prob=1.0)
    assert "现在" in out and "马上" in out and "所以" in out   # 词替换在
    assert out == _lexical_swap(src, casual=True)             # 无句首迟疑词


def test_parse_persona_lead_phrases_from_quirks():
    """B：从 quirks 引号段提取短口头禅，过滤过长/非中文。"""
    quirks = '喜欢说"哇！""啊对对对""诶真的吗"，偶尔夹杂日语'
    assert parse_persona_lead_phrases(quirks) == ("哇", "啊对对对", "诶真的吗")
    assert parse_persona_lead_phrases(
        '常说"你说的这点我有三个看法"', catchphrase="其实,话说") == ("其实", "话说")


def test_persona_leads_used_in_colloquialize():
    """B：有人设口头禅时句首优先用口头禅（确定性）。"""
    src = "我今天其实过得挺充实的但是有点累想早点休息"
    out = colloquialize(
        src, EmotionSpec("neutral"), lead_prob=1.0,
        persona_leads=("哇", "啊对对对"))
    assert out.startswith("哇，") or out.startswith("啊对对对，")


def test_build_voice_style_hint_merges_quirks():
    hint = build_voice_style_hint("撒娇", '常说"你知道吗"、"说真的"')
    assert "撒娇" in hint and "你知道吗" in hint


def test_stats_colloquial_paralinguistic_counters():
    """#1：record_synth 的 colloquial/paralinguistic 计数与占比正确（失败不计）。"""
    st = get_avatar_voice_stats()
    st.reset()
    try:
        st.record_synth(ok=True, channel="emotion", emotion="warm",
                        colloquial=True, colloquial_llm=True)
        st.record_synth(ok=True, channel="emotion", emotion="warm",
                        paralinguistic=True)
        st.record_synth(ok=False, colloquial=True)   # 失败合成不计活人感命中
        d = st.dump()
        assert d["synth_ok"] == 2 and d["synth_fail"] == 1
        assert d["colloquial"] == 1 and d["colloquial_llm"] == 1
        assert d["paralinguistic"] == 1
        assert d["colloquial_rate"] == 0.5           # 1 / 2 成功
        assert d["colloquial_llm_rate"] == 0.5
        assert d["paralinguistic_rate"] == 0.5
        prom = st.dump_prom()
        assert "avatar_voice_colloquial_total 1" in prom
        assert "avatar_voice_colloquial_llm_total 1" in prom
        assert "avatar_voice_paralinguistic_total 1" in prom
    finally:
        st.reset()   # 进程级单例：清理避免污染其他测试
