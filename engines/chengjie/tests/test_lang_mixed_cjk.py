"""#139（2026-09-01 钧）回归：中文客户 AI 草稿出英文——混排文本语言证据修正。

实锤链：detect_language 对「拉丁字母数 > 汉字数」的混排文本恒回 en →
classify_evidence 旧口径按成句拉丁判**强证据 en** → 草稿链（resolve_reply_language
→ resolve_conversation_language）当场切英文拟稿；同一 detect 喂给
vote_language，客户语言画像（peer_language_hint）跟着记 en。
中文客户聊菲律宾电话卡（SMART/Globe/unli data promo 一类品牌套餐词）是
天然触发场景（994 图工单）。

修正口径（latin_mixed_zh，与泰文碎片 _SCRIPT_STRONG_MIN_SHARE 同族）：
剥离中性内容后，汉字 ≥2 且占「汉字+拉丁」≥30% → zh 强证据；
汉字 ≥2 但占比不足（英文句引用汉字）→ en 证据封顶为弱，永不触发切换。
"""

from __future__ import annotations

import pytest

from src.ai.lang_policy import (
    EvidenceStrength,
    classify_evidence,
    evidence_lang,
    latin_mixed_zh,
    resolve_conversation_language,
)
from src.ai.translation_service import detect_language
from src.inbox.outbound_translate import vote_language


# ── 工单实锤形态：中文句夹英文品牌/套餐词 ─────────────────────────────

MIXED_ZH_SAMPLES = [
    "你帮我看下 SMART 的 unli data promo",          # 电话卡话术（#139 场景）
    "那个 Globe 的 SIM card 信号怎么样",
    "菲律宾口音太重了 heavy accent 真的听不懂",
    "帮我查一下这个 package 多少钱一个月",
]


@pytest.mark.parametrize("text", MIXED_ZH_SAMPLES)
def test_mixed_zh_is_strong_zh(text):
    lang, strength = classify_evidence(text)
    assert (lang, strength) == ("zh", EvidenceStrength.STRONG), text


@pytest.mark.parametrize("text", MIXED_ZH_SAMPLES)
def test_mixed_zh_evidence_lang(text):
    assert evidence_lang(text) == "zh", text


def test_english_quoting_hanzi_never_strong():
    """英文句引用两个汉字（占比 ≈6%）：不判 zh，也绝不升 en 强证据。"""
    text = "how do you pronounce 谢谢 in english my friend"
    lang, strength = classify_evidence(text)
    assert lang == "en"
    assert strength == EvidenceStrength.WEAK


def test_pure_english_still_strong():
    lang, strength = classify_evidence("I just finished dinner with some rice")
    assert (lang, strength) == ("en", EvidenceStrength.STRONG)


def test_pure_chinese_unaffected():
    lang, strength = classify_evidence("我刚吃完晚饭，烤鱼配米饭")
    assert (lang, strength) == ("zh", EvidenceStrength.STRONG)


def test_latin_mixed_zh_requires_stripped_core():
    assert latin_mixed_zh("你帮我看下 SMART 的 unli data promo") is True
    assert latin_mixed_zh("hello there my friend") is False
    assert latin_mixed_zh("看下 SMART unli data promo plan") is False  # 汉字碎片不够句骨架
    assert latin_mixed_zh("谢") is False           # 单字碎片不构成
    assert latin_mixed_zh("") is False


# ── 会话决策：混排消息不得把中文会话切到英文 ─────────────────────────

def test_resolution_stays_zh_on_mixed_message():
    history = [
        {"role": "user", "content": "我想办一张菲律宾的电话卡"},
        {"role": "assistant", "content": "好呀，你人在马尼拉还是宿务？"},
        {"role": "user", "content": "你帮我看下 SMART 的 unli data promo"},
    ]
    d = resolve_conversation_language(
        "你帮我看下 SMART 的 unli data promo", history, default="zh")
    assert d.lang == "zh"
    assert d.source == "detected"   # zh 强证据，不是回落默认


def test_draft_chain_resolves_zh_on_mixed_message():
    """B 线草稿链入口（resolve_reply_language）：中文夹外文词的最后入站 → zh 拟稿。

    994 图事故形态：即便此前几轮 AI 草稿已经错发英文（assistant 侧英文历史会
    抬高 eff_default），本条 zh 强证据也必须把正文语言拉回中文。
    """
    from src.inbox.persona_reply import resolve_reply_language
    history = [
        {"role": "user", "content": "我想办一张菲律宾的电话卡"},
        {"role": "assistant", "content": "That's good to hear, which network?"},
        {"role": "user", "content": "你帮我看下 SMART 的 unli data promo"},
    ]
    assert resolve_reply_language(
        "你帮我看下 SMART 的 unli data promo", history) == "zh"


def test_resolution_weak_en_sticks_to_prev_zh():
    """英文句引用汉字（弱 en）：粘住上一轮 zh，不切换。"""
    d = resolve_conversation_language(
        "how do you pronounce 谢谢 in english", None, prev_lang="zh", default="zh")
    assert d.lang == "zh"
    assert d.source == "sticky"


# ── 画像投票（peer_language_hint 的证据层）─────────────────────────────

def test_vote_language_mixed_counts_as_zh():
    msgs = [
        {"direction": "in", "text": "你帮我看下 SMART 的 unli data promo"},
        {"direction": "in", "text": "那个 Globe 的 SIM card 信号怎么样"},
        {"direction": "in", "text": "好的谢谢"},
    ]
    assert vote_language(msgs, detect=detect_language) == "zh"


def test_vote_language_real_english_still_en():
    msgs = [
        {"direction": "in", "text": "September already, time flies so fast"},
        {"direction": "in", "text": "good morning what are you doing today"},
    ]
    assert vote_language(msgs, detect=detect_language) == "en"
