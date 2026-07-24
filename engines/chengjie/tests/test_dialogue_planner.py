"""AI Live OS Agent4 — dialogue_planner 表演规划器门禁。"""
from src.ai.dialogue_planner import (
    PerformanceScript,
    emotion_gap_factor,
    plan_voice_performance,
)


# ── emotion_gap_factor：情绪缩放节奏（本 Agent 核心增量）──────────────────────
def test_gap_factor_excited_shorter_than_sad():
    """兴奋抢话（gap 短）< 中性 < 低落拖沓（gap 长）——节奏带情绪。"""
    excited = emotion_gap_factor("excited", 1.1, intensity=0.8)
    neutral = emotion_gap_factor("neutral", 1.1, intensity=0.8)
    sad = emotion_gap_factor("sad", 1.1, intensity=0.8)
    assert excited < neutral < sad


def test_gap_factor_clamped():
    """极端 base + 强情绪也夹在 [0.7, 1.6]（防抢话机械/冷场）。"""
    assert 0.7 <= emotion_gap_factor("excited", 0.5, 1.0) <= 1.6
    assert 0.7 <= emotion_gap_factor("sad", 1.6, 1.0) <= 1.6


def test_gap_factor_intensity_scales_deviation():
    """intensity 越高，偏离基准越多（低强度更接近基准）。"""
    base = 1.1
    weak = emotion_gap_factor("sad", base, intensity=0.0)
    strong = emotion_gap_factor("sad", base, intensity=1.0)
    # sad 是 >1（拉长），强 intensity 应更远离 base
    assert abs(strong - base) > abs(weak - base)


def test_gap_factor_unknown_emotion_is_base():
    assert emotion_gap_factor("???", 1.1, 0.6) == 1.1


# ── plan_voice_performance：分条 + 脚本 ───────────────────────────────────────
_SPLIT = {"min_total_chars": 24, "part_max_chars": 20, "max_parts": 3,
          "min_tail_chars": 4, "gap_factor": 1.1, "gap_jitter_sec": [1.0, 2.5]}


def test_short_text_single_part_no_split():
    s = plan_voice_performance("在的呀", emotion="warm", split_cfg=_SPLIT)
    assert isinstance(s, PerformanceScript)
    assert s.should_split is False
    assert s.part_texts == ["在的呀"]
    assert s.parts[0].is_first and s.parts[0].is_last and s.parts[0].lead


def test_long_text_splits_multi_parts():
    txt = "今天天气特别好呀。我早上去公园跑了会儿步。然后买了杯咖啡慢慢喝。"
    s = plan_voice_performance(txt, emotion="happy", intensity=0.7,
                               split_cfg=_SPLIT)
    assert s.should_split is True
    assert len(s.parts) >= 2
    # 只有首条 lead=True（防连发都「其实，」开头）
    assert s.parts[0].lead is True
    assert all(not p.lead for p in s.parts[1:])
    # 首尾标记正确
    assert s.parts[0].is_first and s.parts[-1].is_last


def test_emotion_affects_script_gap_factor():
    txt = "今天天气特别好呀。我早上去公园跑了会儿步。然后买了杯咖啡慢慢喝。"
    happy = plan_voice_performance(txt, emotion="excited", intensity=0.8,
                                   split_cfg=_SPLIT)
    sad = plan_voice_performance(txt, emotion="sad", intensity=0.8,
                                 split_cfg=_SPLIT)
    assert happy.gap_factor < sad.gap_factor


def test_laugh_hint_only_when_cue_and_emotion():
    # 有笑意 + 开心 → laugh_hint True
    s1 = plan_voice_performance("哈哈哈你太逗了真的笑死我了哈哈", emotion="playful",
                                split_cfg={**_SPLIT, "min_total_chars": 4})
    assert any(p.laugh_hint for p in s1.parts)
    # 有笑意但情绪 sad → 不加笑（不硬笑）
    s2 = plan_voice_performance("哈哈哈你太逗了真的笑死我了哈哈", emotion="sad",
                                split_cfg={**_SPLIT, "min_total_chars": 4})
    assert not any(p.laugh_hint for p in s2.parts)
    # 无笑意 + 开心 → 不加笑
    s3 = plan_voice_performance("嗯我知道了这个我记下来了", emotion="happy",
                                split_cfg={**_SPLIT, "min_total_chars": 4})
    assert not any(p.laugh_hint for p in s3.parts)


def test_empty_text_yields_no_parts():
    s = plan_voice_performance("   ", emotion="warm", split_cfg=_SPLIT)
    assert s.parts == []
    assert s.should_split is False


def test_thinking_delay_nonnegative_and_bounded():
    s = plan_voice_performance("今天真的好累啊，忙了一整天。", emotion="sad",
                               split_cfg=_SPLIT,
                               humanize_cfg={"min_sec": 1.0, "max_sec": 6.0})
    assert 1.0 <= s.thinking_delay_sec <= 6.0


def test_deterministic_segmentation():
    txt = "今天天气特别好呀。我早上去公园跑了会儿步。然后买了杯咖啡慢慢喝。"
    a = plan_voice_performance(txt, emotion="happy", intensity=0.7, split_cfg=_SPLIT)
    b = plan_voice_performance(txt, emotion="happy", intensity=0.7, split_cfg=_SPLIT)
    assert a.part_texts == b.part_texts
    assert a.gap_factor == b.gap_factor
