"""M-1 B（#234，D-M3，2026-09-06）：出站语言闸门禁。

实录：Messenger 客户发 "Hi"，账号数据清除后会话语言记忆清零、单句判不出 en →
目标语言落空 → 走「目标未知→发原文」→ 15:44 中文「Hey! 好久不见了…」直投外国客户；
同窗 30 余条 ``[xlate] src_len=1 … target_lang_mismatch``（表情/单字进了翻译器）。

覆盖：
- ``resolve_outbound_lang`` 五级优先级：手动 > 档案 > 消息证据 > 文字系统 > 出站历史 > 人设；
  全无 → ("", "")；``decided_by`` 如实；
- 文字系统兜底 ``script_language``：拉丁→en（人设拉丁语兜底）/ 假名→ja / 谚文→ko / 泰文→th /
  越南变音→vi / 汉字→zh / 无文字→""；
- ``translate_outbound_text``：清空语言记忆 + 客户 "Hi" → 目标 en（不再 zh 直投）；零证据 →
  HOLD + reason=lang_unknown；target_lang_mismatch / 引擎异常 → HOLD + 原因，不发原文；
  ``[xlate] outbound … decided_by=`` 日志；
- ``TranslationService.translate``：表情 / 单字符 / 纯标点不进引擎（identity）；
- worker 两处 HOLD 文案：``translate_hold:lang_unknown`` / ``translate_hold:<err>``；
- 静态钉：autosend_helpers 把 contacts_store / cfg_root 传进翻译器；worker 两处都走
  ``_translate_hold_message``。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.inbox.outbound_translate import (
    persona_default_lang,
    resolve_outbound_lang,
    script_language,
    translate_outbound_text,
)

_ROOT = Path(__file__).resolve().parents[1]


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _Res:
    def __init__(self, translated, ok=True, provider="ai", error=""):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error


class _TS:
    def __init__(self, res, detect=""):
        self._res = res
        self._detect = detect
        self.calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        self.calls.append((text, target_lang, source_lang))
        return self._res


class _Store:
    def __init__(self, *, language="", outbound_lang="", recent=None, contact_id=""):
        self._language = language
        self._outbound_lang = outbound_lang
        self._recent = list(recent or [])
        self._contact_id = contact_id
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self._language,
                "contact_id": self._contact_id}

    def get_outbound_lang_if_set(self, cid):
        return self._outbound_lang

    def list_recent_messages(self, cid, limit=50, **kw):
        return list(self._recent)

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig, kw))
        return True


class _Contact:
    def __init__(self, lang):
        self.language_hint = lang


class _Contacts:
    def __init__(self, lang="th"):
        self._lang = lang

    def get_contact(self, cid):
        return _Contact(self._lang) if cid else None


def _in(text, ts=1.0):
    return {"direction": "in", "text": text, "ts": ts}


def _out(text, ts=1.0):
    return {"direction": "out", "text": text, "ts": ts}


# ── ① 文字系统兜底 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,lang", [
    ("Hi", "en"), ("hello po", "en"), ("こんにちは", "ja"), ("안녕", "ko"),
    ("สวัสดี", "th"), ("chào bạn nhé", "vi"), ("你好", "zh"), ("привет", "ru"),
    ("東京タワーに行く", "ja"),          # 日文汉字夹假名 → ja 不是 zh
    ("👍👍!!", ""), ("", ""), ("123 ...", ""),
])
def test_script_language(text, lang):
    assert script_language(text) == lang


def test_script_language_latin_default_follows_persona_latin_lang_only():
    assert script_language("Hola", latin_default="es") == "es"
    assert script_language("Hi", latin_default="th") == "en"   # 泰语非拉丁 → 仍 en
    assert script_language("Hi", latin_default="zh") == "en"   # 绝不落 zh
    assert script_language("Hi", latin_default="") == "en"


# ── ② 五级优先级 ─────────────────────────────────────────────────────────────
def test_resolve_manual_wins():
    st = _Store(outbound_lang="ja", recent=[_in("hello there my friend")])
    assert resolve_outbound_lang("messenger:a:1", store=st) == ("ja", "manual")


def test_resolve_manual_auto_falls_through_to_evidence():
    st = _Store(outbound_lang="auto", recent=[_in("hello there my friend how are you")])
    assert resolve_outbound_lang("messenger:a:1", store=st) == ("en", "message")


def test_resolve_profile_language_beats_message_evidence():
    st = _Store(recent=[_in("hello there my friend how are you")], contact_id="c1")
    assert resolve_outbound_lang("messenger:a:1", store=st,
                                 contacts_store=_Contacts("th")) == ("th", "profile")


def test_resolve_single_hi_after_memory_wipe_is_en_not_zh():
    """事故金标：会话语言记忆清零 + 客户只说了 "Hi" → 目标 en（文字系统），绝非落空/zh。"""
    st = _Store(language="unknown", recent=[_in("Hi")])
    assert resolve_outbound_lang("messenger:a:1", store=st) == ("en", "message_script")


def test_resolve_script_uses_persona_latin_language():
    """统计检测判不出的拉丁短句（"Ok"）+ 人设对外语言是西语 → 按人设拉丁语兜底而非恒 en。"""
    st = _Store(recent=[_in("Ok")])
    import src.inbox.outbound_translate as m
    orig = m.persona_default_lang
    m.persona_default_lang = lambda *a, **k: "es"
    try:
        assert resolve_outbound_lang("wa:a:1", store=st) == ("es", "message_script")
    finally:
        m.persona_default_lang = orig


def test_resolve_persona_default_when_customer_silent():
    st = _Store(recent=[_in("[图片]")])
    import src.inbox.outbound_translate as m
    orig = m.persona_default_lang
    m.persona_default_lang = lambda *a, **k: "th"
    try:
        assert resolve_outbound_lang("line:a:1", store=st) == ("th", "persona")
    finally:
        m.persona_default_lang = orig


def test_resolve_outbound_history_before_persona():
    st = _Store(recent=[_out("Good morning! Have a great day today"),
                        _out("Sure, see you tonight then")])
    lang, by = resolve_outbound_lang("tg:a:1", store=st)
    assert (lang, by) == ("en", "outbound_history")


def test_resolve_nothing_returns_empty_never_zh():
    st = _Store(recent=[])
    assert resolve_outbound_lang("tg:a:1", store=st) == ("", "")
    assert resolve_outbound_lang("", store=st) == ("", "")
    assert resolve_outbound_lang("tg:a:1", store=None) == ("", "")


def test_persona_default_lang_tolerates_missing_config():
    assert persona_default_lang(None, "telegram", "a", "1") == ""
    assert persona_default_lang({}, "telegram", "a", "1") == ""


# ── ③ translate_outbound_text 端到端 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_incident_hi_customer_gets_translation_not_chinese():
    ts = _TS(_Res("Hey! Long time no see, still up this late?"), detect="zh")
    st = _Store(language="unknown", recent=[_in("Hi")])
    item = {"conversation_id": "messenger:61584011289581:tazkei",
            "text": "Hey! 好久不见了，这么晚还没睡呀?"}
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == "Hey! Long time no see, still up this late?"
    assert ts.calls[0][1] == "en"
    assert "_xlate_hold" not in item


@pytest.mark.asyncio
async def test_zero_evidence_holds_with_lang_unknown(caplog):
    ts = _TS(_Res("x"))
    st = _Store(language="", recent=[])
    item = {"conversation_id": "messenger:a:1", "text": "好久不见了，这么晚还没睡呀?"}
    with caplog.at_level(logging.INFO, logger="src.inbox.outbound_translate"):
        out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out is None
    assert item["_xlate_hold"]["reason"] == "lang_unknown"
    assert ts.calls == []
    assert any("action=hold" in r.getMessage() and "lang_unknown" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_zero_evidence_holds_even_for_non_cjk_text_and_gate_only():
    """全无目标 → 即便文本是英文、即便 gate_only，也 HOLD：判不出就不该自动发。"""
    ts = _TS(_Res("x"))
    st = _Store(language="", recent=[])
    for go in (False, True):
        item = {"conversation_id": "messenger:a:1", "text": "Hey there, long time no see!"}
        out = await translate_outbound_text(item, translation_service=ts, store=st,
                                            source_lang="zh", gate_only=go)
        assert out is None and item["_xlate_hold"]["reason"] == "lang_unknown"


@pytest.mark.asyncio
async def test_target_lang_mismatch_holds_and_never_sends_original(caplog):
    ts = _TS(_Res("", ok=False, provider="ai", error="target_lang_mismatch"), detect="zh")
    st = _Store(recent=[_in("Hi")])
    item = {"conversation_id": "messenger:a:1", "text": "好久不见了，这么晚还没睡呀?"}
    with caplog.at_level(logging.INFO, logger="src.inbox.outbound_translate"):
        out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out is None
    assert item["_xlate_hold"] == {"reason": "target_lang_mismatch", "target": "en",
                                   "decided_by": "message_script"}
    assert st.recorded == []
    assert any("decided_by=message_script" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_engine_exception_holds_with_reason():
    class _Boom:
        def detect_language(self, t):
            return "zh"

        async def translate(self, *a, **k):
            raise RuntimeError("down")

    st = _Store(recent=[_in("hello there my friend how are you")])
    item = {"conversation_id": "wa:a:1", "text": "好的，明天见"}
    assert await translate_outbound_text(item, translation_service=_Boom(), store=st,
                                         source_lang="zh") is None
    assert item["_xlate_hold"]["reason"] == "translate_exception"


@pytest.mark.asyncio
async def test_emoji_only_still_passes_without_hold():
    ts = _TS(_Res("x"))
    st = _Store(language="", recent=[])
    item = {"conversation_id": "wa:a:1", "text": "👍🙏"}
    assert await translate_outbound_text(item, translation_service=ts, store=st) == "👍🙏"
    assert "_xlate_hold" not in item and ts.calls == []


@pytest.mark.asyncio
async def test_decided_by_logged_on_translate(caplog):
    ts = _TS(_Res("Good morning"), detect="zh")
    st = _Store(outbound_lang="en")
    item = {"conversation_id": "tg:a:1", "text": "早上好呀"}
    with caplog.at_level(logging.INFO, logger="src.inbox.outbound_translate"):
        assert await translate_outbound_text(item, translation_service=ts, store=st,
                                             source_lang="zh") == "Good morning"
    assert any("[xlate] outbound" in r.getMessage() and "decided_by=manual" in r.getMessage()
               and "action=translated" in r.getMessage() for r in caplog.records)


# ── ④ 表情 / 单字符不进翻译器 ─────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["👍", "😂😂", "k", "好", "!!!", "1", " . "])
async def test_translation_service_identity_for_emoji_single_char(text):
    from src.ai.translation_service import TranslationService, _no_translatable_body
    assert _no_translatable_body(text) is True
    svc = TranslationService.__new__(TranslationService)
    svc.default_target_lang = "zh"
    res = await TranslationService.translate(svc, text, target_lang="zh", source_lang="en")
    assert res.ok is True and res.provider == "identity"
    assert res.translated_text == text


def test_no_translatable_body_negative():
    from src.ai.translation_service import _no_translatable_body
    assert _no_translatable_body("Hi") is False
    assert _no_translatable_body("你好") is False
    assert _no_translatable_body("ok 👍") is False


# ── ⑤ worker 两处 HOLD 文案 ───────────────────────────────────────────────────
def test_worker_hold_message_variants():
    from src.inbox.autosend_worker import _translate_hold_message
    assert _translate_hold_message({"_xlate_hold": {"reason": "lang_unknown"}}).startswith(
        "translate_hold:lang_unknown:")
    m = _translate_hold_message({"_xlate_hold": {"reason": "target_lang_mismatch", "target": "en"}})
    assert m.startswith("translate_hold:target_lang_mismatch:") and "待确认" in m and "target=en" in m
    assert _translate_hold_message({}).startswith("translate_hold:")
    assert "原文" in _translate_hold_message({"text": "x"})


def test_worker_both_hold_sites_use_reasoned_message():
    src = (_ROOT / "src" / "inbox" / "autosend_worker.py").read_text(encoding="utf-8")
    assert src.count("raise RuntimeError(_translate_hold_message(item))") == 2
    assert "translate_hold: 出站翻译不可用，已拦截（无兜底纪律，不发原文）\")" not in src


def test_autosend_translate_cb_passes_profile_and_persona_sources():
    src = (_ROOT / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8")
    seg = src[src.index("def build_autosend_translate_cb"):]
    seg = seg[:seg.index("def build_autosend_mark_read_cb")]
    assert "contacts_store=_cstore" in seg and "cfg_root=_cfg_root" in seg
