"""真人微特征：思考重复词 / 轻笑声（IndexTTS 可念汉字，非 Cosy 标记）。"""
from src.ai.voice_colloquial import (
    _soft_laugh,
    _thinking_repeat,
    colloquialize,
)
from src.ai.voice_emotion import EmotionSpec


def test_thinking_pause_lands_on_clause_boundary_not_word_repeat():
    import re
    raw = "价格的话，基础版一个月一百九十九，三个号起步"
    out, hit = _thinking_repeat(raw, seed=12345, prob=1.0, min_chars=10)
    assert hit
    assert out.count("嗯……") == 1
    assert re.search(r"[，,]嗯……", out)
    assert out.replace("嗯……", "") == raw
    assert not re.search(r"([\u4e00-\u9fff]{2})……\1", out)


def test_thinking_pause_needs_a_clause_boundary():
    raw = "我觉得你可以再给自己一点时间慢慢来"
    out, hit = _thinking_repeat(raw, seed=12345, prob=1.0, min_chars=10)
    assert not hit and out == raw


def test_thinking_pause_not_stacked():
    raw = "嗯……我想一下，这个其实挺简单的呀"
    out, hit = _thinking_repeat(raw, seed=12345, prob=1.0, min_chars=10)
    assert not hit and out == raw


def test_thinking_repeat_skips_when_prob_zero():
    out, hit = _thinking_repeat("我觉得，你可以再给自己一点时间", seed=1, prob=0.0)
    assert not hit and out == "我觉得，你可以再给自己一点时间"


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
    text = "今天这事办得特别顺利，我觉得超开心的呀"
    a = colloquialize(
        text, spec, min_chars=8, enable_fillers=False,
        enable_thinking_repeat=True, enable_soft_laugh=True,
        think_prob=1.0, laugh_prob=1.0, max_inserts=2)
    has_laugh = a.startswith("嘿，")
    has_think = "嗯……" in a
    assert has_laugh or has_think
    assert not (has_laugh and has_think)
    if has_laugh:
        assert "哈哈" not in a[:6]


def test_remove_stutter():
    from src.ai.voice_colloquial_llm import remove_stutter
    assert remove_stutter("我、我跟你说哦") == "我跟你说哦"
    assert remove_stutter("我觉得……我觉得可以") == "我觉得可以"
    assert remove_stutter("这个，这个其实挺好") == "这个其实挺好"
    assert remove_stutter("智聊‖短 智聊能帮你") == "智聊能帮你"
    # 正常话 / 叠词 / 原文就有的重复不动
    assert remove_stutter("对，对方说可以") == "对，对方说可以"
    assert remove_stutter("谢谢你呀，看看这个") == "谢谢你呀，看看这个"
    assert remove_stutter("好，好，我知道了", "好，好，我知道了") == "好，好，我知道了"
