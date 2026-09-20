"""人性化注入门禁：口头禅 / 幽默 / 脾气 / 亲密脏话 是否被正确注入 + 受关系阶段闸控。

覆盖不变量：
  - quirks(口头禅) / humor / emoji_level:high 无条件注入（真人感基础，之前是死数据）。
  - temperament(脾气) 与 banter_profanity(口头脏话) **仅 intimate/steady 阶段**放开；
    生人/暖场阶段收着 / 完全不放开（防一上来就没大没小 / 骂人）。
  - banter 放开时，硬红线文案（不人身攻击 / 对方难过立即收起）必须一并注入。
纯函数式：直接调 PersonaManager._format_persona_instructions（构造轻量、无 IO 依赖）。
"""
import pytest

from src.utils.persona_manager import PersonaManager


def _fmt(persona, stage=""):
    pm = PersonaManager()
    return pm._format_persona_instructions(persona, funnel_stage=stage)


_PERSONA = {
    "name": "林小雨",
    "role": "大学生",
    "personality": {
        "traits": ["活泼"],
        "style": "轻松",
        "emoji_level": "high",
        "quirks": '喜欢说"哇！""啊对对对"',
        "humor": "爱自嘲、会玩梗",
        "temperament": "被冷落会吃醋闹小脾气",
    },
    "speaking": {"language_follow": True, "banter_profanity": True},
    "identity": {"deny_ai": True},
}


def test_quirks_humor_emoji_always_injected():
    out = _fmt(_PERSONA, stage="warming")
    assert "口头禅" in out and "啊对对对" in out
    assert "幽默感" in out and "玩梗" in out
    assert "emoji 用得很多" in out  # emoji_level:high 现已生效


def test_tastes_block_injected():
    """tastes 断链修复回归（2026-07-27 真机考题实锤：档案写喜欢「Barolo红酒」，
    AI 被问喜欢喝什么酒时即兴编了「Rioja」——tastes 有数据但从不进 prompt）。"""
    p = dict(_PERSONA)
    p["tastes"] = {
        "likes": ["Barolo红酒", "抹茶"],
        "dislikes": ["被控制"],
        "opinions": ["钱要花在体验上"],
    }
    out = _fmt(p)
    assert "你的好恶与观点" in out
    assert "Barolo红酒" in out and "抹茶" in out
    assert "你反感：被控制" in out
    assert "钱要花在体验上" in out
    assert "不要即兴编造" in out
    # 无 tastes / 空 tastes → 不出块
    assert "你的好恶与观点" not in _fmt(_PERSONA)
    p2 = dict(_PERSONA)
    p2["tastes"] = {"likes": [], "dislikes": "", "opinions": None}
    assert "你的好恶与观点" not in _fmt(p2)


def test_age_gender_fact_nail_injected():
    """age/gender 断链修复回归（2026-07-28 真机考题实锤：林小雨档案 age=22，AI 答
    「我今年20岁啦」；赵老师 age=58，AI 答「六十八了」——两个字段在 persona_manager
    里零出现，AI 只能照 background 的模糊暗示瞎猜）。

    要求：结构化 age 是**事实钉子**（与身份硬锁同族），明确压过背景文字；
    gender 更克制，只保证被问到 / 自称时不说错。
    """
    p = dict(_PERSONA)
    p["age"] = 22
    p["gender"] = "female"
    out = _fmt(p)
    assert "【年龄事实·硬锁】你今年22岁" in out
    assert "22岁回答" in out and "绝不说成别的数字" in out
    assert "优先级高于背景故事" in out  # 结构化字段压过背景里的年龄暗示
    assert "【性别事实】你是女性" in out
    # 赵老师式反例：背景像退休老教师，结构化 58 仍必须钉住
    p2 = {"name": "赵老师", "role": "退休语文老师", "age": 58, "gender": "male",
          "background": "在讲台上站了大半辈子，如今在老宅侍弄花草。"}
    out2 = _fmt(p2)
    assert "你今年58岁" in out2 and "你是男性" in out2
    # 缺字段 / 脏数据 → 一行都不注入（绝不出现「你今年None岁」）
    for bad in ({}, {"age": None}, {"age": ""}, {"age": "五十八"}, {"age": 0},
                {"age": 130}, {"age": True}, {"gender": ""}):
        clean = _fmt({**_PERSONA, **bad})
        assert "【年龄事实" not in clean and "你今年" not in clean
        assert "None" not in clean
    assert "【性别事实" not in _fmt(_PERSONA)


def test_context_blocks_injected():
    """context.family / schedule / filipino_connection 断链修复回归——三块都是
    客户会回头核对的强事实（家人姓名/上下班时间），此前一句都没进 prompt。"""
    p = dict(_PERSONA)
    p["context"] = {
        "family": {"father": "林志强，退休 IT 高管", "daughter": "林佳玥，小学四年级"},
        "schedule": {"work_hours": "17:00–05:00 夜班", "night_tone": "温柔话密"},
        "filipino_connection": {"food": "Adobo 和 Sinigang", "language": "偶尔飙 Tagalog"},
    }
    out = _fmt(p)
    assert "【你的家人】" in out and "父亲：林志强，退休 IT 高管" in out
    assert "女儿：林佳玥，小学四年级" in out
    assert "【你的作息】" in out and "上班时间：17:00–05:00 夜班" in out
    assert "【你的在地文化】" in out and "饮食：Adobo 和 Sinigang" in out
    # 前任配偶有中文标签：抽取器（Call2b）会产出 ex_husband，裸键名漏进 prompt
    # 会让 AI 照读英文键——「前夫」是客户高频追问点，标签必须齐
    p3 = {**_PERSONA, "context": {"family": {
        "ex_husband": "José Luis Jerónimo，2018 年离婚",
        "ex_wife": "Elena Rossi，2019 年离婚"}}}
    out3 = _fmt(p3)
    assert "前夫：José Luis Jerónimo，2018 年离婚" in out3
    assert "前妻：Elena Rossi，2019 年离婚" in out3
    assert "ex_husband" not in out3 and "ex_wife" not in out3
    # 空 dict / 缺键 → 整块不出
    p2 = {**_PERSONA, "context": {"family": {}, "schedule": {"work_hours": ""}}}
    assert "【你的家人】" not in _fmt(p2)
    assert "【你的作息】" not in _fmt(p2)
    assert "【你的家人】" not in _fmt(_PERSONA)


def test_appearance_injected_with_language_guard():
    """appearance 此前只喂生图链，聊天侧看不到 → 被问长相只能现编，还会和刚发的
    自拍打脸。注入时必须要求用对话语言转述，别照搬英文锚点。"""
    p = {**_PERSONA, "appearance": "a 22-year-old girl with long dark brown hair"}
    out = _fmt(p)
    assert "【你的外貌】" in out
    assert "long dark brown hair" in out
    assert "不要照搬这段英文原文" in out
    assert "与你发出去的照片保持一致" in out
    assert "【你的外貌】" not in _fmt(_PERSONA)


def test_temperament_gated_open_when_intimate():
    out = _fmt(_PERSONA, stage="intimate")
    assert "真实性情" in out
    assert "吃醋" in out or "拌两句嘴" in out  # 放开的脾气描述
    assert "绝不冷暴力" in out


def test_temperament_reined_in_when_not_intimate():
    for stage in ("", "initial", "warming"):
        out = _fmt(_PERSONA, stage=stage)
        assert "真实性情" in out
        assert "先收着点脾气" in out
        # 未熟阶段不注入「放开脾气」的启用文案（区别于 temperament 描述本身）
        assert "可以像真人一样有小情绪" not in out
        assert "绝不冷暴力" not in out


def test_banter_profanity_open_only_intimate():
    out = _fmt(_PERSONA, stage="steady")
    assert "尺度·亲密闲聊" in out
    # 硬红线必须同时注入
    assert "不人身攻击" in out
    assert "情绪低落" in out and "立刻收起" in out


def test_banter_profanity_blocked_when_not_intimate():
    for stage in ("", "initial", "warming"):
        out = _fmt(_PERSONA, stage=stage)
        assert "尺度·亲密闲聊" not in out


def test_banter_requires_optin():
    """未开 banter_profanity 的人设，即使亲密阶段也不放开脏话。"""
    persona = {**_PERSONA, "speaking": {"language_follow": True}}
    out = _fmt(persona, stage="intimate")
    assert "尺度·亲密闲聊" not in out


def test_no_humanization_fields_is_safe():
    """无 quirks/humor/temperament 的极简人设不应崩、也不注入相关段。"""
    out = _fmt({"name": "A", "role": "助手"}, stage="intimate")
    assert "口头禅" not in out
    assert "真实性情" not in out
    assert "尺度·亲密闲聊" not in out
    assert "你是A" in out
