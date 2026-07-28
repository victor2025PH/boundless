"""信息性提问守卫（2026-07-27）——关键词池不劫持「问名词本身」的消息。

实锤背景（2026-07-26 晚测试）：lin_xiaoyu 相册给条目配了「咖啡」等触发词，
测试号问「你喝什么咖啡」（问喜好，不是要图）→ 关键词池命中即发图并短路正文，
17:27-17:41 连发 6 张、问题从未被正面回答，被骂「已读乱回」；
「这是哪里拍的呢，还有樱花」追问上一张也被再发一张（"发图不答话"）。

守卫语义：疑问句且**无求图动词** → 关键词池让路（select_media 收口点，
A 线 skill_manager Stage 0 与 B 线 image_autosend.pick_registered_media 同享）；
陈述句照常命中；显式求图（看看/发一张/有没有…照片）照常命中；
通用池仍由调用方 generic_ok（detect_selfie_request）把门，不受影响。
"""
import pytest

from src.companion.persona_media import (
    POOL_KEYWORD, POOL_NONE, explain_match, is_info_question, select_media,
)


# ── 纯函数：实录事故正例（必须让路）──────────────────────────────────────
@pytest.mark.parametrize("text", [
    "你喝什么咖啡",                                  # 17:27 首发劫持
    "你在喝什么咖啡呢",                              # 17:29 再劫持
    "你是跑了几家咖啡馆",                            # 17:39 问数量也发图
    "我说你喝什么咖啡，比如黑咖啡这些",              # 17:40 三问仍发图
    "你不要已读乱回好嘛",                            # 17:40 抱怨句也发图
    "我以为是抹茶拿铁的照片呢，这是哪里拍的呢，还有樱花",  # 18:41 追问被当要图
    "你给我发了照片嘛",                              # 18:41 确认句（发了=完成态）
    "what coffee do you drink?",
])
def test_info_question_hits(text):
    assert is_info_question(text) is True


# ── 纯函数：反例（不得误伤）────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "我今天在咖啡馆自习",            # 陈述句提及触发词 → 照常命中（设计行为）
    "跳个舞",                        # 设计文档金标例
    "给我跳个舞",
    "你有奶茶照片吗，我看看",        # 疑问但显式求图（看看）
    "有没有咖啡馆的照片呀",          # 有没有…照片 → 求图
    "发一张你的自拍呗",
    "再来一张",
    "show me a pic of your coffee?",
    "",                              # 空文本
])
def test_not_info_question(text):
    assert is_info_question(text) is False


# ── select_media 收口行为 ────────────────────────────────────────────────
_ROWS = [
    {"id": "c1", "media_type": "photo", "enabled": True,
     "triggers": ["咖啡"], "weight": 1, "file_path": "cafe_a_01.jpg"},
    {"id": "g1", "media_type": "photo", "enabled": True,
     "triggers": [], "weight": 1, "file_path": "home-sofa_b_01.jpg"},
]


def test_select_media_question_yields_none():
    """疑问句：关键词池让路；generic_ok=False → 整体 None（交 LLM 答话）。"""
    assert select_media(_ROWS, "你喝什么咖啡", generic_ok=False) is None


def test_select_media_statement_still_fires():
    row = select_media(_ROWS, "我在咖啡馆坐着呢好舒服", generic_ok=False)
    assert row is not None and row["id"] == "c1"


def test_select_media_explicit_request_fires_keyword_pool():
    row = select_media(_ROWS, "有没有咖啡馆的照片呀", generic_ok=False)
    assert row is not None and row["id"] == "c1"


def test_select_media_question_with_generic_ok_falls_to_generic():
    """疑问句 + 调用方已判定是要图（generic_ok=True）→ 回落通用池而非硬拒。"""
    row = select_media(_ROWS, "你喝什么咖啡", generic_ok=True)
    assert row is not None and row["id"] == "g1"


def test_explain_match_mirrors_guard():
    """试触发预览与运行时同口径，且标注 info_question 原因。"""
    res = explain_match(_ROWS, "你喝什么咖啡", generic_ok=False)
    assert res["pool"] == POOL_NONE
    assert res["info_question"] is True
    res2 = explain_match(_ROWS, "我在咖啡馆坐着呢好舒服", generic_ok=False)
    assert res2["pool"] == POOL_KEYWORD
    assert res2["info_question"] is False
