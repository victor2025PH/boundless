# -*- coding: utf-8 -*-
"""P0-198 翻译干净通道 + 语言幻觉三针（2026-07-31，198 坐席机实锤修复）。

事故 A（人设劫持译文）：AIEngine.translate 经 ai_client.chat 走完整聊天管线，
system 带全局 prompt + 域/默认人设 bio——入站「So you married ?」被 conversion
域人设按 bio 答成「哈哈，你这是在打探我的私事呀 离过婚，现在是单身😄」并写进
inbox 行的 translated_text（198 库里两条实锤，另一条是「我是真人steven啦」）。

事故 B（语言切换幻觉）：坐席把回复工坊钉「中文」+ 客户全程英文 → 模型脑补
「对方突然讲英文」，10:18–10:35 后端日志 8+ 次「Haha, suddenly switched to
English on me」族草稿。

覆盖：
- AIEngine 优先走 generate_reply(_bare_system) 干净通道；老签名桩回落 chat；
- _output_lang_sane 语言完整性护栏（目标 CJK 须含 CJK / 拉丁目标拒高 CJK 占比）；
- AIClient._build_system_instruction 对 _bare_system 早退（不带人设/全局 prompt）；
- build_reply_lang_mismatch_hint 工作语言说明（草稿语言≠客户语言）；
- build_language_anchor_hint 反向锚点（英文客户 + 历史语言点评 → 锚定没换语言）；
- apply_inbound_enrichments：mismatch 命中时压制 switch hint（两者指令矛盾）。
"""

from __future__ import annotations

import pytest

from src.ai.translation_engines import AIEngine, _output_lang_sane
from src.inbox.inbound_enrich import (
    apply_inbound_enrichments,
    build_language_anchor_hint,
    build_reply_lang_mismatch_hint,
)


# ── AIEngine 干净通道 ────────────────────────────────────────────────────────
class _CleanStubAI:
    """新契约桩：带 generate_reply（记录 context）+ chat（不应被调用）。"""

    def __init__(self, reply="所以你结婚了？"):
        self._reply = reply
        self.gen_calls = []
        self.chat_calls = 0

    async def generate_reply(self, prompt, context=None, conversation_history=None,
                             strategy_overrides=None, *, _skip_quality_check=False):
        self.gen_calls.append({
            "prompt": prompt, "context": dict(context or {}),
            "skip_quality": _skip_quality_check,
        })
        return self._reply

    async def chat(self, prompt, strategy_overrides=None):
        self.chat_calls += 1
        return self._reply


class _LegacyStubAI:
    """旧契约桩：只有 chat（历史测试桩形态），必须保持可用。"""

    def __init__(self, reply="所以你结婚了？"):
        self._reply = reply
        self.chat_calls = 0

    async def chat(self, prompt, strategy_overrides=None):
        self.chat_calls += 1
        return self._reply


class _OldSigStubAI:
    """generate_reply 存在但不认新参（TypeError）→ 必须回落 chat。"""

    def __init__(self, reply="所以你结婚了？"):
        self._reply = reply
        self.chat_calls = 0

    async def generate_reply(self, prompt):  # 旧签名：不收 context
        raise AssertionError("不该带参调用")

    async def chat(self, prompt, strategy_overrides=None):
        self.chat_calls += 1
        return self._reply


@pytest.mark.asyncio
async def test_ai_engine_uses_bare_system_channel():
    ai = _CleanStubAI()
    res = await AIEngine(ai).translate(
        "So you married ?", source_lang="en", target_lang="zh")
    assert res.ok and res.text == "所以你结婚了？"
    assert ai.chat_calls == 0, "有干净通道时不得再走 chat 全聊天管线"
    assert len(ai.gen_calls) == 1
    ctx = ai.gen_calls[0]["context"]
    assert ctx.get("_skip_lang_guard") is True
    assert "translation engine" in str(ctx.get("_bare_system") or "").lower()
    assert ai.gen_calls[0]["skip_quality"] is True


@pytest.mark.asyncio
async def test_ai_engine_falls_back_to_chat_for_legacy_stub():
    ai = _LegacyStubAI()
    res = await AIEngine(ai).translate(
        "So you married ?", source_lang="en", target_lang="zh")
    assert res.ok and res.text == "所以你结婚了？"
    assert ai.chat_calls == 1


@pytest.mark.asyncio
async def test_ai_engine_old_generate_reply_signature_falls_back():
    ai = _OldSigStubAI()
    res = await AIEngine(ai).translate(
        "hi", source_lang="en", target_lang="zh")
    assert res.ok and ai.chat_calls == 1


# ── 语言完整性护栏 ──────────────────────────────────────────────────────────
def test_output_lang_sane_cjk_target_requires_cjk():
    assert _output_lang_sane("所以你结婚了？", "zh") is True
    # 目标中文却回了英文答句（答非所译/没翻）→ 拒
    assert _output_lang_sane("Divorced, actually.", "zh") is False


def test_output_lang_sane_latin_target_rejects_cjk_identity():
    # 目标英文却整段中文（identity 回显 / 人设答问）→ 拒
    assert _output_lang_sane("哈哈，你这是在打探我的私事呀", "en") is False
    # 少量 CJK 专名放行（比例阈值）
    assert _output_lang_sane("I met 小雨 at the cafe yesterday, lovely chat.", "en") is True
    assert _output_lang_sane("Sure, sounds great!", "en") is True


def test_output_lang_sane_unknown_target_passes():
    assert _output_lang_sane("whatever", "") is True


@pytest.mark.asyncio
async def test_ai_engine_rejects_wrong_language_output():
    # 目标 zh 但模型回了英文（典型答非所译形态）→ EngineResult 失败，路由可换下家
    ai = _CleanStubAI(reply="Divorced, actually. It's been a few years now.")
    res = await AIEngine(ai).translate(
        "So you married ?", source_lang="en", target_lang="zh")
    assert not res.ok and res.error == "target_lang_mismatch"


# ── AIClient._bare_system 早退（不带人设/全局 prompt）────────────────────────
class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"},
              "ai": {"system_prompt": "你是顾嘉，无界科技的方案顾问"}}

    def get_ai_config(self):
        return {}


def test_build_system_instruction_bare_system_short_circuits():
    from src.ai.ai_client import AIClient
    client = AIClient(_Cfg())
    bare = "You are a machine translation engine."
    out = client._build_system_instruction({"_bare_system": bare})
    assert out == bare
    assert "顾嘉" not in out and "人设" not in out


def test_build_system_instruction_without_bare_keeps_old_behavior():
    from src.ai.ai_client import AIClient
    client = AIClient(_Cfg())
    out = client._build_system_instruction({"reply_lang": "zh"})
    assert "顾嘉" in out  # 常规链路仍带全局 system prompt


# ── 工作语言说明（草稿语言 ≠ 客户语言）───────────────────────────────────────
def test_reply_lang_mismatch_hint_fires_for_zh_draft_en_customer():
    hint = build_reply_lang_mismatch_hint(
        reply_lang="zh", current_text="What style do you like?")
    assert "工作语言说明" in hint
    assert "英语" in hint and "中文" in hint
    assert "切换" in hint  # 明示「不是对方切换语言」


def test_reply_lang_mismatch_hint_silent_when_langs_match():
    assert build_reply_lang_mismatch_hint(
        reply_lang="en", current_text="What style do you like?") == ""
    assert build_reply_lang_mismatch_hint(
        reply_lang="zh", current_text="今天想聊点什么呀") == ""


def test_reply_lang_mismatch_hint_silent_on_undetectable():
    assert build_reply_lang_mismatch_hint(reply_lang="zh", current_text="👍") == ""
    assert build_reply_lang_mismatch_hint(reply_lang="", current_text="hello there") == ""


# ── 反向语言锚点（英文客户被说成「突然讲英文」）──────────────────────────────
def _en_history_with_ai_comment():
    return [
        {"role": "user", "content": "Do you cook at home often?"},
        {"role": "assistant", "content": "Haha, suddenly switched to English on me 😄"},
        {"role": "user", "content": "I went shopping today"},
        {"role": "assistant", "content": "Oh nice, clothes shopping is always fun"},
    ]


def test_anchor_reverse_fires_for_stable_english_with_ai_comment():
    hint = build_language_anchor_hint(
        _en_history_with_ai_comment(),
        current_text="What style do you like?")
    assert "锚定" in hint and "英语" in hint
    assert "没有切换语言" in hint


def test_anchor_reverse_silent_without_ai_language_comment():
    hist = [
        {"role": "user", "content": "Do you cook at home often?"},
        {"role": "assistant", "content": "Yes! I make pasta a lot."},
    ]
    assert build_language_anchor_hint(
        hist, current_text="What style do you like?") == ""


def test_anchor_reverse_silent_on_real_switch():
    # 真的切换了（历史主导 zh → 本条 en）→ 不锚定，交给 switch hint
    hist = [
        {"role": "user", "content": "今天在家做饭了吗"},
        {"role": "assistant", "content": "Haha, suddenly switched to English on me 😄"},
    ]
    assert build_language_anchor_hint(
        hist, current_text="What style do you like?") == ""


def test_anchor_original_zh_case_still_works():
    # Phase8 原有场景：本条中文 + 历史含日文 → 中文锚点（行为不变）
    hist = [
        {"role": "user", "content": "こんにちは、元気ですか"},
        {"role": "assistant", "content": "元気だよ！"},
    ]
    hint = build_language_anchor_hint(hist, current_text="好呀好呀")
    assert "中文" in hint and "没有切换语言" in hint


def test_lang_comment_re_matches_198_variants():
    # 198 实录的 AI 点评句式必须被识别为「风险语境」
    from src.inbox.inbound_enrich import _LANG_COMMENT_RE
    for s in (
        "哈哈，突然跟我讲起英文来了 😅",
        "哦，突然跟我说英语了？😅",
        "Haha, suddenly switched to English on me 😄",
        "Ah, you switched to English mid-sentence, that's cute. 😄",
        "突然换成日语啦？",
    ):
        assert _LANG_COMMENT_RE.search(s), s


# ── apply_inbound_enrichments：mismatch 压制矛盾的 switch hint ────────────────
def test_enrich_mismatch_suppresses_switch_hint():
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="What style do you like?",
        history=[
            {"role": "user", "content": "今天在家做饭了吗"},
            {"role": "assistant", "content": "做了呀，做了意面"},
        ],
        reply_lang="zh",
    )
    hint = ctx.get("_topic_switch_hint") or ""
    assert "工作语言说明" in hint
    # switch hint 的「请用英语回复并点一下切换」指令与锁定中文矛盾，必须被压制
    assert "自然承接" not in hint


def test_enrich_without_mismatch_keeps_switch_hint():
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="What style do you like?",
        history=[
            {"role": "user", "content": "今天在家做饭了吗"},
            {"role": "assistant", "content": "做了呀，做了意面"},
        ],
        reply_lang="en",  # 跟随客户 → 无 mismatch → switch hint 照旧
    )
    hint = ctx.get("_topic_switch_hint") or ""
    assert "自然承接" in hint
