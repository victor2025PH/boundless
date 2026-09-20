"""真人微特征：思考重复词 / 轻笑声（IndexTTS 可念汉字，非 Cosy 标记）。"""
from src.ai.voice_colloquial import (
    _soft_laugh,
    _thinking_repeat,
    colloquialize,
)
from src.ai.voice_emotion import EmotionSpec


def test_thinking_repeat_inserts_word_ellipsis_word():
    import re
    raw = "我觉得你可以再给自己一点时间慢慢来"
    out, hit = _thinking_repeat(raw, seed=12345, prob=1.0, min_chars=10)
    assert hit and "……" in out
    assert re.search(r"([\u4e00-\u9fff]{2})……\1", out)


def test_thinking_repeat_skips_when_prob_zero():
    out, hit = _thinking_repeat("我觉得你可以再给自己一点时间", seed=1, prob=0.0)
    assert not hit and out == "我觉得你可以再给自己一点时间"


def test_soft_laugh_happy_only_single_hei():
    """轻笑只许单音节「嘿」，禁止哈哈/哈哈哈（TTS 念假）。"""
    out, hit = _soft_laugh("今天天气真好啊", "happy", seed=99, prob=1.0)
    assert hit and out.startswith("嘿，")
    assert not out.startswith(("哈哈", "嘿嘿", "呵呵"))
    out2, hit2 = _soft_laugh("今天天气真好啊", "serious", seed=99, prob=1.0)
    assert not hit2
    # 正文已有笑点 → 不再句首加
    out3, hit3 = _soft_laugh("哈哈哈今天太逗了", "happy", seed=99, prob=1.0)
    assert not hit3


def test_colloquialize_human_ticks_mutex():
    """同条轻笑与思考重复互斥（seed 分流）。"""
    spec = EmotionSpec("happy", intensity=0.8)
    text = "今天这事办得特别顺利我觉得超开心的呀"
    a = colloquialize(
        text, spec, min_chars=8, enable_fillers=False,
        enable_thinking_repeat=True, enable_soft_laugh=True,
        think_prob=1.0, laugh_prob=1.0, max_inserts=2)
    has_laugh = a.startswith("嘿，")
    has_think = "……" in a and any(
        f"{w}……{w}" in a for w in ("今天", "这事", "办得", "特别", "顺利", "觉得", "开心")
    )
    assert has_laugh or has_think
    if has_laugh:
        assert "哈哈" not in a[:6]
        assert not has_think or a.count("……") <= 2
