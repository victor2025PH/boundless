"""会话语言策略单测——直接以 2026-07 三个线上事故为验收基准。

覆盖：
  1. 明确语言请求解析（中/英/日/韩书写 + 否定/能力疑问排除 + 看不懂负向请求）
  2. 中性词剥离（品牌词/短语气词/URL/数字/emoji 不构成语言证据）
  3. 证据强度分级（强=脚本级/成句；弱=含糊拉丁短文本）
  4. 会话决策（优先级链 + 粘滞 + 稳定切换 + 偏好漂移释放）
  5. 历史恢复 latest_explicit_request（无状态产线的偏好持久语义）
"""

from __future__ import annotations

import pytest

from src.ai.lang_policy import (
    EvidenceStrength,
    classify_evidence,
    contains_language_alias,
    latest_explicit_request,
    normalize_lang_code,
    parse_language_request,
    resolve_conversation_language,
    strip_neutral_tokens,
    valid_lang_code,
)


# ── 1. 明确语言请求 ─────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # 中文书写
    ("我们用日语聊吧", "ja"),
    ("说日语", "ja"),
    ("跟我说日语吧", "ja"),
    ("请用日文回复", "ja"),
    ("换成英文", "en"),
    ("改用韩语", "ko"),
    ("切换到西班牙语", "es"),
    ("日文回复我", "ja"),
    ("请说中文", "zh"),
    ("换回中文吧", "zh"),
    ("用英语交流", "en"),
    # 英文书写
    ("please speak japanese", "ja"),
    ("can you speak Japanese?", "ja"),
    ("speak in english please", "en"),
    ("reply in chinese", "zh"),
    ("talk to me in korean", "ko"),
    ("switch to english", "en"),
    ("in japanese please", "ja"),
    ("Can we chat in Spanish", "es"),
    # 日文书写
    ("日本語で話してください", "ja"),
    ("日本語でお願いします", "ja"),
    ("英語で話して", "en"),
    ("中国語にして", "zh"),
    # 韩文书写
    ("한국어로 말해줘", "ko"),
    ("영어로 대답해 주세요", "en"),
    ("일본어로 해줘", "ja"),
])
def test_parse_request_hits(text, expected):
    assert parse_language_request(text) == expected


@pytest.mark.parametrize("text", [
    # 普通聊天，无请求
    "今天天气不错",
    "whatsapp",
    "ok",
    "What did you eat today?",
    "日本語が難しいですね",       # 陈述日语难，不是请求
    # 否定
    "别说日语了",
    "不要用英文",
    "don't speak english to me",
    "stop using japanese",
    # 能力疑问（中文口径：是提问不是指令）
    "你会说日语吗？",
    "会不会日语？",
    # 长文本转述
    "昨天有个客户说要用日语聊，我没理他，后来他又说要用英文，真麻烦" * 2,
])
def test_parse_request_misses(text):
    assert parse_language_request(text) == ""


def test_cant_understand_negative_request():
    """「看不懂中文」写成英文 → 切到消息自身语言（en）。"""
    assert parse_language_request("sorry I can't understand chinese") == "en"
    assert parse_language_request("I don't read Chinese, sorry") == "en"


@pytest.mark.parametrize("text,expected", [
    # 线上事故（小董 85263115820，2026-07-22）：粤语否定「唔系讲英文」+ 正向「讲中文」
    # 同句共存。旧逻辑先匹到「讲英文」误判 en，守卫再把正确中文草稿翻成英文回给客户，
    # 客户连发「你怎么发英文给我 / 乱来啊你」。修复后否定片段被屏蔽、正向请求命中 → zh。
    ("我我都同你讲咗，我唔系讲英文噶，我同你讲啦同讲中文啊，大佬普通话国语啊。", "zh"),
    ("我唔系讲英文，讲中文", "zh"),
    ("我唔係講英文，講中文", "zh"),
    ("不是讲英文，是讲中文", "zh"),
    ("说中文", "zh"),
])
def test_cantonese_negation_then_positive_request(text, expected):
    assert parse_language_request(text) == expected


@pytest.mark.parametrize("text", [
    # 纯否定（无正向请求）仍返回 ""——屏蔽被否定片段后无残留
    "别说日语了",
    "不要用英文",
    "我唔系讲英文噶",       # 只否定英文、没提要说什么 → 无正向请求
])
def test_negation_only_still_misses(text):
    assert parse_language_request(text) == ""


# ── 2. 中性词剥离 ───────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "whatsapp",
    "WhatsApp",
    "telegram",
    "ok",
    "OK!!",
    "usdt",
    "ok thx",
    "https://t.me/abc123",
    "@someone_123",
    "12345",
    "666",
    "👍👍",
    "ok 👍",
    "whatsapp telegram line",
])
def test_neutral_only_strips_to_empty(text):
    assert strip_neutral_tokens(text) == ""


def test_neutral_keeps_substance():
    assert strip_neutral_tokens("加我 whatsapp 聊") == "加我 聊"
    assert "help" in strip_neutral_tokens("please help me with whatsapp").lower()


# ── 3. 证据强度 ─────────────────────────────────────────────────

def test_evidence_neutral_none():
    for t in ("whatsapp", "ok", "👍", "8888", "@user tg"):
        lang, strength = classify_evidence(t)
        assert strength == EvidenceStrength.NONE, t


def test_evidence_strong_scripts():
    assert classify_evidence("日本語わかりますか") == ("ja", EvidenceStrength.STRONG)
    assert classify_evidence("안녕하세요 반가워요") == ("ko", EvidenceStrength.STRONG)
    assert classify_evidence("你在干嘛呢") == ("zh", EvidenceStrength.STRONG)
    assert classify_evidence("Привет как дела") == ("ru", EvidenceStrength.STRONG)


def test_evidence_strong_english_sentence():
    lang, strength = classify_evidence("What did you eat today my friend?")
    assert (lang, strength) == ("en", EvidenceStrength.STRONG)


def test_evidence_weak_short_latin():
    lang, strength = classify_evidence("good morning")  # good 是中性词 → 剩 morning
    assert strength in (EvidenceStrength.WEAK, EvidenceStrength.STRONG)
    lang2, strength2 = classify_evidence("nice")
    assert strength2 == EvidenceStrength.WEAK


# ── 4. 会话决策 ─────────────────────────────────────────────────

_ZH_HISTORY = [
    {"role": "user", "content": "你在干嘛呢"},
    {"role": "assistant", "content": "刚下班～你呢"},
    {"role": "user", "content": "我也刚回家"},
]


def test_incident_2_brand_word_stays_chinese():
    """事故2验收：中文会话里发「whatsapp」→ 语言保持中文，且不可写缓存。"""
    d = resolve_conversation_language("whatsapp", _ZH_HISTORY, prev_lang="zh")
    assert d.lang == "zh"
    assert d.stable is False  # 不允许污染 detected_lang 缓存


def test_incident_2_without_prev_falls_to_window():
    d = resolve_conversation_language("whatsapp", _ZH_HISTORY, prev_lang="")
    assert d.lang == "zh"
    assert d.source == "window"


def test_incident_1_explicit_request_wins_over_message_script():
    """事故1验收：中文书写的「用日语」请求 → 立即日语并可持久。"""
    d = resolve_conversation_language("我们用日语聊吧", _ZH_HISTORY, prev_lang="zh")
    assert d.lang == "ja"
    assert d.source == "explicit_request"
    assert d.request == "ja"
    assert d.stable is True


def test_incident_1_pref_persists_over_neutral_and_zh():
    """偏好持久：设了 ja 偏好后，用户发中文短句/中性词，回复仍日语。"""
    d = resolve_conversation_language("ok", _ZH_HISTORY, prev_lang="ja", lang_pref="ja")
    assert d.lang == "ja"
    assert d.source == "user_pref"
    # 单条中文强证据 + 上一条不是同语言 → 连续段不成立，偏好仍生效
    d2 = resolve_conversation_language(
        "好的知道了", _ZH_HISTORY, prev_lang="ja", lang_pref="ja"
    )
    assert d2.lang == "ja"


def test_pref_released_by_stable_drift():
    """偏好释放：用日语提出请求(pref_input=ja)后连续 ≥2 条中文强证据 → 漂移回中文。"""
    hist = _ZH_HISTORY + [
        {"role": "user", "content": "其实我中文也可以的日语太难了"},
    ]
    d = resolve_conversation_language(
        "刚才那句话我看不懂啊", hist, prev_lang="ja",
        lang_pref="ja", lang_pref_input="ja",
    )
    assert d.lang == "zh"
    assert d.source == "stable_switch"


def test_pref_not_released_when_writing_in_request_language():
    """事故1核心护栏：用中文请求日语(pref_input=zh)后继续打中文 → 偏好绝不释放。"""
    hist = _ZH_HISTORY + [
        {"role": "user", "content": "我们用日语聊吧"},
        {"role": "user", "content": "今天上班好累啊"},
    ]
    d = resolve_conversation_language(
        "晚上想吃火锅", hist, prev_lang="ja",
        lang_pref="ja", lang_pref_input="zh",
    )
    assert d.lang == "ja"
    assert d.source == "user_pref"


def test_genuine_switch_follows_immediately():
    """真实语言切换（强证据）：当条即跟随，不要求连续两条。"""
    d = resolve_conversation_language(
        "What did you eat today my friend?", _ZH_HISTORY, prev_lang="zh"
    )
    assert d.lang == "en"
    assert d.source == "detected"
    assert d.stable is True
    d2 = resolve_conversation_language("日本語わかりますか", _ZH_HISTORY, prev_lang="zh")
    assert d2.lang == "ja"


def test_operator_lock_absolute():
    d = resolve_conversation_language(
        "我们用日语聊吧", _ZH_HISTORY, prev_lang="zh", operator_lock="en"
    )
    assert d.lang == "en"
    assert d.source == "operator_lock"
    assert d.request == "ja"  # 请求仍透出，供打标签/提示运营


def test_weak_evidence_sticky_prev():
    """弱证据永不切换：英文短残余粘住上一轮语言。"""
    d = resolve_conversation_language("nice", _ZH_HISTORY, prev_lang="zh")
    assert d.lang == "zh"
    assert d.source == "sticky"


def test_first_contact_weak_latin_defaults_en():
    d = resolve_conversation_language("nice", None, prev_lang="")
    assert d.lang == "en"
    assert d.source == "weak_detect"
    assert d.stable is False


def test_empty_all_default():
    d = resolve_conversation_language("👍", None, prev_lang="", default="zh")
    assert d.lang == "zh"
    assert d.source == "default"


# ── 5. 历史恢复（无状态产线的偏好持久） ────────────────────────

def test_latest_request_from_history():
    hist = _ZH_HISTORY + [
        {"role": "user", "content": "我们用日语聊吧"},
        {"role": "assistant", "content": "わかりました！"},
        {"role": "user", "content": "ok"},
    ]
    assert latest_explicit_request(hist) == "ja"


def test_latest_request_released_after_drift():
    hist = [
        {"role": "user", "content": "我们用日语聊吧"},
        {"role": "assistant", "content": "わかりました！"},
        {"role": "user", "content": "算了还是中文吧太难了"},
        {"role": "user", "content": "你今天吃了什么"},
    ]
    # 注意：「算了还是中文吧」本身是明确请求(zh)——newest-first 先命中它
    assert latest_explicit_request(hist) == "zh"


def test_latest_request_drift_release_without_new_request():
    hist = [
        {"role": "user", "content": "please speak japanese"},
        {"role": "assistant", "content": "はい！"},
        {"role": "user", "content": "今天上班好累啊感觉不想动"},
        {"role": "user", "content": "晚上想吃火锅你觉得怎么样"},
    ]
    assert latest_explicit_request(hist) == ""  # 连续 2 条中文强证据 → 释放


def test_latest_request_not_released_by_neutral_noise():
    hist = [
        {"role": "user", "content": "我们用日语聊吧"},
        {"role": "assistant", "content": "はい！"},
        {"role": "user", "content": "ok"},
        {"role": "user", "content": "👍"},
    ]
    assert latest_explicit_request(hist) == "ja"


# ── 6. P1 收尾新增辅助（LLM 短判门控 / 语言码归一） ─────────────

def test_contains_language_alias_gate():
    """LLM 短判的廉价门控：提及语言名才放行。"""
    assert contains_language_alias("my chinese is bad sorry")
    assert contains_language_alias("日本語が難しい")
    assert contains_language_alias("说日语的人")
    assert not contains_language_alias("今天天气不错")
    assert not contains_language_alias("whatsapp ok 👍")
    assert not contains_language_alias("")


def test_valid_lang_code_rejects_hallucination():
    """LLM 返回码必须在已知语言集内，防幻觉码流入状态机。"""
    assert valid_lang_code("ja") == "ja"
    assert valid_lang_code("JA") == "ja"
    assert valid_lang_code("zh-cn") == "zh"
    assert valid_lang_code("xx") == ""
    assert valid_lang_code("klingon") == ""
    assert valid_lang_code("") == ""


def test_normalize_lang_code_aliases():
    assert normalize_lang_code("zh-CN") == "zh"
    assert normalize_lang_code("zh_tw") == "zh"
    assert normalize_lang_code("JP") == "ja"
    assert normalize_lang_code("ar_ur") == "ar"
    assert normalize_lang_code("") == ""


def test_operator_lock_normalizes_tts_codes():
    """WA 的 forced_lang 历史上存 xtts 码（zh-cn）→ 策略层归一后不被误判为切换。"""
    d = resolve_conversation_language(
        "hello there my friend how are you", None,
        prev_lang="zh-cn", operator_lock="zh-cn",
    )
    assert d.lang == "zh"
    assert d.source == "operator_lock"
