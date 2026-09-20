# -*- coding: utf-8 -*-
"""#152 F1 门禁：LLM 退化循环（复读机化）出站截断。

证据：skuio 机 82VFQ6 / 截图 _1093（0902 23:2x）——CUDDLESTHECAT 会话一条出站
「越来越多越来越多…」重复数百遍直达客户。漏网机制两处：
① 既有「复读守卫」比的是与历史出站的相似度，对单条内部 token 循环不看；
② `_apply_outbound_text_guard` 末尾 `return cleaned or reply`——截空回落原文照发。

钉住：确定性检测（同 n-gram 连续 ≥5 次且跨度 ≥20 字）→ 截到首次出现；整条循环
截空 → meta.degenerate_empty，调用方返回空不回落；正常口语强调/短叠词不误伤。
"""
from src.ai.outbound_text_guard import (
    apply_outbound_text_guard, detect_degenerate_loop, resolve_cfg,
    strip_degenerate_loop,
)

# 截图 _1093 原形态（4 字单元 × 数百）
GOLD_1093 = "越来越多" * 300
GOLD_1093_WITH_HEAD = "结合TA今天聊到的事，最近黄金交易的火热程度" + "越来越多" * 120


def test_gold_1093_pure_loop_is_detected_and_emptied():
    hit = detect_degenerate_loop(GOLD_1093)
    assert hit is not None
    start, end, unit = hit
    assert start == 0 and unit == "越来越多" and end == len(GOLD_1093)
    out, meta = apply_outbound_text_guard(GOLD_1093, resolve_cfg({}))
    assert out == ""                       # 整条循环 → 空，绝不回落原文
    assert meta["degenerate_empty"] is True
    assert meta["degenerate_unit"] == "越来越多"


def test_gold_1093_with_normal_head_keeps_head_once():
    out, meta = apply_outbound_text_guard(GOLD_1093_WITH_HEAD, resolve_cfg({}))
    assert out == "结合TA今天聊到的事，最近黄金交易的火热程度越来越多"
    assert "degenerate_empty" not in meta


def test_english_word_loop():
    text = "I think that's " + "very " * 12 + "good"
    cleaned, unit = strip_degenerate_loop(text)
    assert unit is not None and "very" in unit
    assert cleaned.startswith("I think that's very")
    assert "good" not in cleaned            # 退化之后的尾巴同属不可信，丢弃


def test_normal_emphasis_and_short_reduplication_not_touched():
    # 口语强调 2-3 次 / 短叠词「哈哈哈哈哈哈」（跨度 <20）都不算退化
    for s in ("真的真的真的很好吃", "哈哈哈哈哈哈哈哈", "好好好，那就这么定了",
              "Yes yes yes, let's do it tomorrow morning at nine.",
              "我今天早上起床看到天空那么美，想你想得要命。"):
        assert detect_degenerate_loop(s) is None, s
        out, meta = apply_outbound_text_guard(s, resolve_cfg({}))
        assert out == s and "degenerate_unit" not in meta


def test_config_switch():
    cfg = resolve_cfg({"companion": {"outbound_text_guard": {"degenerate": False}}})
    assert cfg["degenerate"] is False
    out, meta = apply_outbound_text_guard(GOLD_1093, cfg)
    assert out == GOLD_1093 and "degenerate_unit" not in meta


def test_skill_manager_does_not_fall_back_to_degenerate_text():
    """`_apply_outbound_text_guard` 遇退化截空必须返回空，不得 `cleaned or reply`。"""
    import inspect
    from src.skills import skill_manager
    src = inspect.getsource(skill_manager.SkillManager._apply_outbound_text_guard)
    assert 'meta.get("degenerate_empty")' in src
    # 空稿终局：B 线守卫链之后空稿 → return None（不产草稿）
    src2 = inspect.getsource(skill_manager)
    assert "guard_emptied" in src2
