# -*- coding: utf-8 -*-
"""P0-3（2026-09-21，#343 / #326）：出站译文质量门。

- ``translation_confidence`` 认得 Indic 等脚本：hi 目标却吐英文 / 中文 → 低分；真印地语 → 高分。
- 出站链：引擎 ok 但 conf < min_confidence → 不发，换路由下一引擎重译；仍低 → 全自动会话重起草；
  再不行 HOLD ``translate_hold:low_confidence``，状态带独立文案键 + 人话含置信分。
- 未评分结果（旧引擎 / 测试桩，conf=-1）与 ``min_confidence: 0`` 完全不进门。
"""

from __future__ import annotations

import pytest

from src.ai import translation_confidence as tc
from src.inbox import outbound_translate as ot

HI = "नमस्ते, आप कैसे हैं? आज आपका दिन कैसा रहा?"
BN = "হ্যালো, আপনি কেমন আছেন?"
TA = "வணக்கம், நீங்கள் எப்படி இருக்கிறீர்கள்?"
TE = "హలో, మీరు ఎలా ఉన్నారు?"
MR = "नमस्कार, तुम्ही कसे आहात?"
SRC = "你好，你今天过得怎么样？"


@pytest.mark.parametrize("lang,good", [("hi", HI), ("bn", BN), ("ta", TA), ("te", TE), ("mr", MR)])
def test_indic_targets_scored_by_script(lang, good):
    assert tc.translation_confidence(SRC, good, lang) >= tc.TIER_HIGH
    # 引擎吐英文 / 中文 → 目标脚本占比 0 → 低于 TIER_LOW
    assert tc.translation_confidence(SRC, "Hello, how was your day today?", lang) < tc.TIER_LOW
    assert tc.translation_confidence(SRC, "你好，今天怎么样？", lang) < tc.TIER_LOW


def test_unknown_target_still_not_judged():
    assert tc.confidence_signals(SRC, "xyz", "zz")["script_ratio"] == 1.0


def test_parse_min_confidence():
    assert ot.parse_xlate_min_confidence(None) == tc.TIER_LOW
    assert ot.parse_xlate_min_confidence({"inbox": {"l2_autosend": {"translate": {"min_confidence": 0}}}}) == 0.0
    assert ot.parse_xlate_min_confidence({"inbox": {"l2_autosend": {"translate": {"min_confidence": "x"}}}}) == tc.TIER_LOW
    assert ot.parse_xlate_min_confidence({"inbox": {"l2_autosend": {"translate": {"min_confidence": 7}}}}) == 1.0


# ── 出站链 ────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, translated, ok=True, provider="ai", error="", confidence=-1.0):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error
        self.confidence = confidence


class _TS:
    def __init__(self, first, retries=(), nxt="hymt", detect="zh"):
        self.first = first
        self.retries = list(retries)
        self.nxt = nxt
        self._detect = detect
        self.retry_calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        return self.first

    def next_engine_after(self, failed, target):
        return self.nxt

    async def retry_once(self, text, *, target_lang, source_lang, style="chat", engine=""):
        self.retry_calls.append(engine)
        return self.retries.pop(0) if self.retries else _Res("", ok=False, error="ai:empty")


class _Store:
    def __init__(self, mode="auto_ai", lang="hi"):
        self.kv = {}
        self.mode = mode
        self.lang = lang
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": self.lang, "contact_id": ""}

    def get_outbound_lang_if_set(self, cid):
        return ""

    def list_recent_messages(self, cid, limit=50, **kw):
        return []

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig))
        return True

    def get_automation_mode(self, cid):
        return self.mode

    def get_app_setting(self, k, default=None):
        return self.kv.get(k, default)

    def set_app_setting(self, k, v, updated_by=""):
        if v == "":
            self.kv.pop(k, None)
        else:
            self.kv[k] = v

    def get_conv_tags(self, cid):
        return []

    def get_handoff_meta(self, cid):
        return {}


@pytest.fixture
def _no_gap(monkeypatch):
    import asyncio as _aio

    async def _fast(_s):
        return None
    monkeypatch.setattr(_aio, "sleep", _fast)


def _item(cid="whatsapp:a:raj"):
    return {"conversation_id": cid, "text": SRC, "draft_id": "d343"}


async def test_low_conf_switches_engine_and_delivers(_no_gap):
    ts = _TS(_Res("Hello, how was your day?", provider="ai", confidence=0.3),
             retries=[_Res(HI, provider="hymt", confidence=0.97)])
    st = _Store()
    item = _item()
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == HI
    assert ts.retry_calls == ["hymt"], "低置信不重打同引擎，直接换下一引擎"
    assert "_xlate_hold" not in item and item.get("_xlate_action") == "translated"
    assert not any(k.startswith("xlate_hold:") for k in st.kv)


async def test_low_conf_twice_then_hold_with_reason(_no_gap):
    ts = _TS(_Res("Hello, how was your day?", provider="ai", confidence=0.3),
             retries=[_Res("Hello again", provider="hymt", confidence=0.3)])
    st = _Store(mode="human")   # 非全自动 → 不重起草
    item = _item("whatsapp:a:hold")
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out is None
    hold = item["_xlate_hold"]
    assert hold["reason"] == ot.LOW_CONF_REASON and hold["attempts"] == 2
    assert hold["confidence"] == 0.3 and hold["min_confidence"] == 0.5
    assert st.kv.get("xlate_hold:whatsapp:a:hold"), "状态带 marker 落盘"
    assert st.recorded == []


async def test_low_conf_then_redraft_in_target_lang_when_auto(_no_gap):
    ts = _TS(_Res("Hello, how was your day?", provider="ai", confidence=0.3),
             retries=[_Res("still english", provider="hymt", confidence=0.3)], detect="hi")
    st = _Store(mode="auto_ai")
    got = {}

    async def _redraft(item, target):
        got["target"] = target
        return HI
    item = _item("whatsapp:a:rd")
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh",
                                           redraft=_redraft)
    assert out == HI and got["target"] == "hi" and item.get("_xlate_action") == "redraft"


async def test_unscored_result_not_gated(_no_gap):
    ts = _TS(_Res("Hello, how was your day?", provider="ai"))   # confidence=-1
    st = _Store()
    item = _item("whatsapp:a:legacy")
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == "Hello, how was your day?" and ts.retry_calls == []


async def test_medium_conf_delivers_but_is_sampled(_no_gap):
    """P1（#343）：conf 在 [TIER_LOW, TIER_HIGH) → 照发不重译，但计进硬闸观测
    ``medium_confidence``；高置信 / 未评分不计。"""
    from src.inbox.outbound_lang_stats import get_outbound_lang_stats
    stats = get_outbound_lang_stats()
    before = int(stats.dump().get("medium_confidence", 0))
    ts = _TS(_Res(HI, provider="ai", confidence=0.65))
    st = _Store()
    item = _item("whatsapp:a:mid")
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == HI and ts.retry_calls == [] and "_xlate_hold" not in item
    assert int(stats.dump().get("medium_confidence", 0)) == before + 1
    ev = stats.dump().get("last_events") or []
    assert ev and ev[-1]["outcome"] == "medium_confidence" and ev[-1]["target"] == "hi"
    # 高置信 / 未评分 → 不计
    for conf in (0.97, -1.0):
        ts2 = _TS(_Res(HI, provider="ai", confidence=conf))
        await ot.translate_outbound_text(_item("whatsapp:a:hi2"), translation_service=ts2,
                                         store=_Store(), source_lang="zh")
    assert int(stats.dump().get("medium_confidence", 0)) == before + 1


async def test_min_confidence_zero_disables_gate(_no_gap):
    ts = _TS(_Res("Hello", provider="ai", confidence=0.1))
    st = _Store()
    item = _item("whatsapp:a:off")
    out = await ot.translate_outbound_text(
        item, translation_service=ts, store=st, source_lang="zh",
        cfg_root={"inbox": {"l2_autosend": {"translate": {"min_confidence": 0}}}})
    assert out == "Hello" and ts.retry_calls == []


async def test_low_conf_no_next_engine_holds(_no_gap):
    ts = _TS(_Res("Hello", provider="ai", confidence=0.2), nxt="")
    st = _Store(mode="human")
    item = _item("whatsapp:a:single")
    out = await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out is None and item["_xlate_hold"]["reason"] == ot.LOW_CONF_REASON
    assert ts.retry_calls == []


def test_worker_hold_message_is_human_readable():
    from src.inbox.autosend_worker import _translate_hold_message
    item = {"_xlate_hold": {"reason": "low_confidence", "target": "hi", "attempts": 2,
                            "confidence": 0.3, "min_confidence": 0.5}}
    msg = _translate_hold_message(item)
    assert msg.startswith("translate_hold:low_confidence:")
    assert "0.30" in msg and "0.50" in msg and "重试翻译" in msg and "hi" in msg


async def test_conv_state_uses_low_conf_text_key(_no_gap):
    from src.inbox.conv_state import compute
    from src.web.i18n_packs import send_truth_q39 as pack

    class _Health:
        def dump(self):
            return {"inbox_health": {}, "sessions": {}}
    ts = _TS(_Res("Hello", provider="ai", confidence=0.2), nxt="")
    st = _Store(mode="review")
    item = _item("whatsapp:a:cs")
    assert await ot.translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh") is None
    cs = compute(st, "whatsapp:a:cs", platform="whatsapp", account_id="a", health=_Health(), config={})
    assert cs["state"] == "xlate_hold" and cs["tone"] == "danger"
    assert cs["reason_text_key"] == "inbox.cs.xlate_hold.low_conf" and cs["action"] == "retranslate"
    assert cs["ext"]["xlate_hold"]["reason"] == ot.LOW_CONF_REASON
    for lang_pack in (pack.ZH, pack.EN, pack.ZH_HANT):
        assert "inbox.cs.xlate_hold.low_conf" in lang_pack and "inbox.cs.xlate_hold.low_conf_t" in lang_pack
