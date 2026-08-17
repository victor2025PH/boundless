"""P0-198（2026-08-03）「英文会话突然发中文」事故回归网——真实语料金标。

事故链（198 ChatX 智聊，tg 7331682689，2026-08-03 16:45-16:46）：
入站落库前 emoji 被系统加注成「Haha 🤣（表情：笑得满地打滚）」，这段**系统注入的
中文**被五个语言决策点全部当成「客户在说中文」的证据：

  1) ``lang_policy.classify_evidence``（剥离器不认全角圆括号加注）→ 强中文
     → 草稿 reply_lang=zh；
  2) LLM 服从指令生成中文「哈哈，你也太逗了。她大概在笑我自己笑自己的梗吧。」；
  3) ``ai_client._guard_reply_language`` 以同一个错误的 reply_lang=zh 为基准
     → 中文回复"完全正确" → 放行；
  4) ``outbound_translate.vote_language`` 对裸存储文本检测 → 会话语言投成 zh
     → 「已是客户语言」跳过翻译 → 中文原样发给英文客户；
  5) 下一轮 ``build_language_switch_hint`` 把（被投毒的）「中文历史 → 英文本条」
     判成客户切换语言，且旧措辞明确指示模型「轻轻点一下这个切换」
     → "Ha, suddenly switching to English?"（反咬全程说英文的客户）。

修复面：毒源截断（annotate 混排不再改写原话）+ 存量剥离（_EMOJI_NOTE_RE）+
语言证据统一入口（evidence_lang，五个消费口收编）+ 切换提示禁止点破 +
锚点新增「我方发错语言」触发 + 草稿兜底语言跟随会话已建立的工作语言。
改动任何一层导致回归，本文件先红。
"""

import pytest

from src.ai.lang_policy import (
    EvidenceStrength,
    classify_evidence,
    evidence_lang,
    strip_neutral_tokens,
)
from src.ai.translation_service import detect_language
from src.inbox.inbound_enrich import (
    build_language_anchor_hint,
    build_language_switch_hint,
    build_reply_lang_mismatch_hint,
)
from src.inbox.outbound_translate import vote_language
from src.inbox.persona_reply import resolve_reply_language
from src.integrations.tg_inbound_text import annotate_inbound_emoji

# ── 事故原文（198 截图逐字）────────────────────────────────────────────
U1 = "Haha yes"
A1 = "Haha, she's lucky I'm such a devoted employee."
U2_RAW = "Haha 🤣"                                  # 客户实际发的
U2_STORED = "Haha 🤣（表情：笑得满地打滚）"           # 旧加注后的落库形态（存量仍在）
A2_WRONG = "哈哈，你也太逗了。她大概在笑我自己笑自己的梗吧。"   # 错发出去的中文
U3 = "I wanner be that cat"


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


def _in(text):
    return {"direction": "in", "text": text}


# ── 1. 毒源截断：混排文本绝不改写 ───────────────────────────────────────

def test_annotate_mixed_text_never_mutated():
    assert annotate_inbound_emoji(U2_RAW) == U2_RAW
    assert annotate_inbound_emoji("好的👍") == "好的👍"
    assert annotate_inbound_emoji("no emoji here") == "no emoji here"


def test_annotate_pure_emoji_still_semantic():
    pytest.importorskip("emoji")
    out = annotate_inbound_emoji("🤣")
    assert out.startswith("[表情]")  # 方括号形态：媒体块与证据剥离都认识


# ── 2. 存量剥离：历史落库的加注不构成语言证据 ───────────────────────────

def test_strip_kills_legacy_emoji_note():
    assert strip_neutral_tokens(U2_STORED) == ""
    # 半角括号 / 截断缺闭括号 一样剥
    assert strip_neutral_tokens("ok (表情: 笑哭了)") == ""
    assert strip_neutral_tokens("Haha 🤣（表情：笑得满") == ""


def test_strip_keeps_real_substance_next_to_note():
    core = strip_neutral_tokens("I love this 🤣（表情：笑得满地打滚）")
    assert "表情" not in core and "笑得满地打滚" not in core
    assert "love" in core.lower()


def test_classify_evidence_incident_message_not_chinese():
    lang, strength = classify_evidence(U2_STORED)
    assert lang != "zh"
    assert strength == EvidenceStrength.NONE


def test_evidence_lang_unified_entry():
    assert evidence_lang(U2_STORED) == ""
    assert evidence_lang(U1) == ""                      # haha/yes 全中性
    assert evidence_lang(U3) == "en"
    assert evidence_lang("你在做什么") == "zh"
    assert evidence_lang("[表情] 笑得满地打滚") == ""      # 系统贴纸描述
    assert evidence_lang("[图片内容] 一只橘猫趴在键盘上") == ""  # 系统识图描述


def test_evidence_lang_never_returns_unknown():
    # drafts.py R1 欢迎语的 `evidence_lang(t) or lang_prior先验` 兜底依赖这一点：
    # 检测落空必须返回 ""——旧链用 detect_language，落空返回 truthy 的 "unknown"，
    # `or _hint` 永不生效（先验兜底形同虚设，还会把 "unknown" 当语言码传下去）。
    assert evidence_lang("") == ""
    assert evidence_lang("😂👍") == ""
    assert evidence_lang("hi") == ""


# ── 3. 草稿语言决策：事故窗口全程应判 en ────────────────────────────────

def test_resolve_reply_language_incident_window_stays_english():
    history = [_u(U1), _a(A1), _u(U2_STORED)]
    assert resolve_reply_language(U2_STORED, history) == "en"


def test_resolve_reply_language_all_neutral_follows_assistant_language():
    # 全中性用户窗口（连串 haha/ok/emoji）→ 兜底跟随会话已建立的工作语言，
    # 而不是静态先验（旧链在这里落到 zh，是事故的放大器）。
    history = [_u("ok"), _a(A1), _u(U1)]
    assert resolve_reply_language("😂", history) == "en"


def test_resolve_reply_language_no_assistant_keeps_static_default():
    # 没有任何已建立的工作语言 → 保持静态 default（旧行为兜底，不乱猜）。
    history = [_u("ok"), _u(U1)]
    assert resolve_reply_language("😂", history, default="zh") == "zh"


def test_resolve_reply_language_true_switch_still_follows():
    # 用户真切中文（强证据）→ 立即跟随，assistant 英文历史不阻挡
    history = [_u(U1), _a(A1)]
    assert resolve_reply_language("你在做什么呀今天", history) == "zh"


# ── 4. 出站翻译的会话语言投票：系统加注不投票 ───────────────────────────

def test_vote_language_ignores_emoji_note_rows():
    msgs = [_in(U1), _in(U2_STORED), _in(U3)]
    assert vote_language(msgs, detect=detect_language) == "en"


def test_vote_language_all_neutral_returns_blank():
    # 全中性/加注窗口 → ""（回落 store 持久值），绝不能投出 zh
    msgs = [_in(U1), _in(U2_STORED)]
    assert vote_language(msgs, detect=detect_language) == ""


# ── 5. 切换提示：不被投毒历史触发，且永不指示点破 ───────────────────────

def test_switch_hint_quiet_on_poisoned_history():
    hist = [_u(U1), _a(A1), _u(U2_STORED), _a(A2_WRONG)]
    hint = build_language_switch_hint(hist, current_lang="en", current_text=U3)
    assert hint == ""


def test_switch_hint_requires_current_evidence():
    # 本条无语言证据（纯语气+emoji）→ 不做任何切换断言
    hist = [_u("你好呀最近怎么样")]
    hint = build_language_switch_hint(hist, current_lang="en", current_text="Haha 😄")
    assert hint == ""


def test_switch_hint_genuine_switch_fires_but_never_instructs_pointing():
    hist = [_u("你好呀最近怎么样")]
    hint = build_language_switch_hint(
        hist, current_lang="zh", current_text="hey what are you up to tonight",
    )
    assert "英语" in hint and "中文" in hint     # 仍然跟随真切换
    assert "不要点破" in hint                    # 契约：禁止点破
    assert "轻轻点" not in hint and "例如" not in hint  # 旧的教唆措辞绝不回潮


# ── 6. 语言锚点：我方发错语言后，下一轮必须钉住事实 ─────────────────────

def test_anchor_fires_after_own_wrong_language_message():
    hist = [_u("can you send me the script"), _a(A2_WRONG)]
    anchor = build_language_anchor_hint(hist, current_text=U3)
    assert "英语" in anchor
    assert "并没有切换语言" in anchor


def test_anchor_quiet_without_risk_context():
    hist = [_u("can you send me the script"), _a("Sure, sending it over tonight.")]
    assert build_language_anchor_hint(hist, current_text=U3) == ""


# ── 7. 工作语言说明：无证据不断言 ───────────────────────────────────────

def test_mismatch_hint_needs_current_evidence():
    assert build_reply_lang_mismatch_hint(
        reply_lang="zh", current_text="Haha 😄") == ""
    assert build_reply_lang_mismatch_hint(
        reply_lang="zh", current_text=U2_STORED) == ""


def test_mismatch_hint_still_fires_on_real_conflict():
    hint = build_reply_lang_mismatch_hint(reply_lang="zh", current_text=U3)
    assert "英语" in hint and "中文" in hint
