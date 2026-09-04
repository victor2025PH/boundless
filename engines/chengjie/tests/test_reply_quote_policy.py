"""#37 AI 自动链引用回复决策（I-4 D2，2026-09-04）——纯函数 + autosend 接线门禁。

验收（指令 I-4 §4）：
1. 单条入站 → 不引用
2. 连发 3 条后 AI 回复 → 引用最相关那条（金标句对）
3. 不支持引用的平台/路径 → 不报错不阻塞，普通发送
4. reply_to 目标已删/找不到 → 降级不引用，不抛异常
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.inbox import reply_quote_policy as rqp
from src.inbox.reply_quote_policy import (
    decide_quote, parse_quote_cfg, platform_allows_quote, relevance_score,
    unanswered_burst, is_question,
)

NOW = 1_800_000_000.0


def _m(direction, text, ts, pmid="", **kw):
    row = {"direction": direction, "text": text, "ts": ts,
           "platform_msg_id": pmid, "deleted_at": 0, "translated_text": ""}
    row.update(kw)
    return row


# ── 配置 ────────────────────────────────────────────────────────────────

def test_parse_cfg_defaults_and_clamps():
    c = parse_quote_cfg({})
    assert c["enabled"] is False
    assert c["min_unanswered"] == 2
    c2 = parse_quote_cfg({"inbox": {"l2_autosend": {"quote_reply": {
        "enabled": True, "min_unanswered": 1, "min_relevance": 5,
        "platforms": "Telegram"}}}})
    assert c2["enabled"] is True
    assert c2["min_unanswered"] == 2          # 单条不引用是铁律，配 1 也钳回 2
    assert c2["min_relevance"] == 1.0
    assert c2["platforms"] == ["telegram"]
    c3 = parse_quote_cfg({"inbox": {"l2_autosend": {"quote_reply": True}}})
    assert c3["enabled"] is True


def test_platform_allows_only_orchestrator_path():
    cfg = parse_quote_cfg({})
    assert platform_allows_quote("telegram", cfg, orch_owns=True)
    assert not platform_allows_quote("telegram", cfg, orch_owns=False)   # RPA 回落不收 reply_to
    cfg2 = dict(cfg, platforms=["whatsapp"])
    assert not platform_allows_quote("telegram", cfg2, orch_owns=True)
    assert platform_allows_quote("WhatsApp", cfg2, orch_owns=True)


# ── 验收 1：单条入站一律不引用 ─────────────────────────────────────────

def test_single_inbound_never_quotes():
    msgs = [
        _m("out", "在的", NOW - 300, "10"),
        _m("in", "你明天有空吗？", NOW - 60, "11"),
    ]
    d = decide_quote(msgs, "明天有空呀，下午都可以", now=NOW)
    assert d.quote is False and d.reason == "single_inbound"
    assert d.as_reply_to() is None


def test_no_inbound_at_all():
    d = decide_quote([_m("out", "嗨", NOW - 10, "1")], "回复", now=NOW)
    assert d.quote is False and d.reason == "no_inbound"
    assert decide_quote([], "回复", now=NOW).reason == "no_inbound"


def test_unanswered_burst_stops_at_last_outbound():
    msgs = [
        _m("in", "旧问题", NOW - 1000, "1"),
        _m("out", "旧回答", NOW - 900, "2"),
        _m("in", "a", NOW - 100, "3"),
        _m("system", "x", NOW - 90),          # 未知方向：不计不断
        _m("in", "b", NOW - 80, "4"),
    ]
    b = unanswered_burst(msgs)
    assert [r["text"] for r in b] == ["a", "b"]


# ── 验收 2：连发 3 条 → 引用最相关那条（金标）──────────────────────────

GOLD = [
    # (三条入站, AI 回复, 期望被引用的下标)
    (["今天好累啊", "周末要不要一起去爬山？", "对了你上次说的那家火锅店叫什么"],
     "那家火锅店叫蜀大侠，在春熙路那边，超好吃", 2),
    (["今天好累啊", "周末要不要一起去爬山？", "对了你上次说的那家火锅店叫什么"],
     "爬山好呀！周末我有空，去哪座山？", 1),
    (["I'm so tired today", "Are you free this weekend for hiking?",
      "btw what was that hotpot place called"],
     "The hotpot place is Shu Daxia, near Chunxi Road", 2),
    (["我到家了", "你吃饭了没", "晚上要不要视频"],
     "还没吃呢，正准备点外卖，你吃了吗", 1),
]


@pytest.mark.parametrize("inbound,reply,expect_idx", GOLD)
def test_burst_quotes_most_relevant(inbound, reply, expect_idx):
    msgs = [_m("out", "嗯嗯", NOW - 600, "9")]
    for i, t in enumerate(inbound):
        msgs.append(_m("in", t, NOW - 100 + i * 10, str(20 + i)))
    d = decide_quote(msgs, reply, now=NOW)
    assert d.quote is True, (d.reason, d.candidates)
    assert d.target_index == expect_idx, d.candidates
    rt = d.as_reply_to()
    assert rt["id"] == str(20 + expect_idx)
    assert rt["text"] == inbound[expect_idx]
    assert rt["from_me"] is False
    assert set(rt) == {"id", "from_me", "participant", "text", "sender"}
    assert d.burst_len == 3


def test_burst_low_relevance_does_not_quote():
    """分不清在回哪句（回复与三条都不沾边）→ 宁可不引用。"""
    msgs = [_m("in", "今天好累啊", NOW - 100, "1"),
            _m("in", "周末要不要去爬山", NOW - 90, "2"),
            _m("in", "火锅店叫什么", NOW - 80, "3")]
    d = decide_quote(msgs, "哈哈哈哈哈", now=NOW)
    assert d.quote is False and d.reason == "low_relevance"


def test_burst_tie_prefers_earlier_message():
    msgs = [_m("in", "你几点下班", NOW - 100, "1"),
            _m("in", "你几点下班呀", NOW - 90, "2")]
    d = decide_quote(msgs, "我六点下班", now=NOW)
    assert d.quote and d.target_index == 0


def test_cross_language_uses_translated_text_and_reply_alt():
    """客户英文 / 人设中文：入站 translated_text 与回复原文同语，也能算出相关性。"""
    msgs = [_m("in", "so tired today", NOW - 100, "1", translated_text="今天好累"),
            _m("in", "what is that hotpot place called", NOW - 90, "2",
               translated_text="那家火锅店叫什么")]
    d = decide_quote(msgs, "It's called Shu Daxia", now=NOW,
                     reply_alt="那家火锅店叫蜀大侠")
    assert d.quote and d.target_index == 1


def test_media_only_and_deleted_and_stale_are_not_candidates():
    msgs = [
        _m("in", "", NOW - 100, "1", media_type="image"),           # 无文字
        _m("in", "你吃了吗", NOW - 90, "2", deleted_at=NOW - 50),    # 已删
        _m("in", "你吃了吗", NOW - 3 * 86400, "3"),                 # 太老
    ]
    d = decide_quote(msgs, "我吃了", now=NOW)
    assert d.quote is False and d.reason == "no_candidate" and d.burst_len == 3


def test_question_detection():
    assert is_question("你吃了吗")
    assert is_question("what time?")
    assert is_question("點解唔返工")
    assert not is_question("我到家了")


def test_relevance_score_bounds():
    assert relevance_score("", "x") == 0.0
    assert relevance_score("abc", "") == 0.0
    assert 0.0 <= relevance_score("火锅店叫蜀大侠", "火锅店叫什么") <= 1.0


def test_stats_counters():
    rqp.reset_stats()
    rqp.record_decision(decide_quote([_m("in", "a", NOW, "1")], "b", now=NOW))
    rqp.record_applied({"quote_applied": True})
    rqp.record_applied({"delivered": True})
    rqp.record_fallback_plain()
    s = rqp.stats_snapshot()
    assert s["decided"] == 1 and s["quoted"] == 0
    assert s["skipped"] == {"single_inbound": 1}
    assert s["applied"] == 1 and s["fallback_plain"] == 1
    rqp.reset_stats()


# ── autosend 接线（验收 2/3/4 的发送层半边）────────────────────────────

def _assistant(cfg, rows):
    class _St:
        def list_recent_messages(self, cid, limit=50, **kw):
            return list(rows)

        def record_outreach(self, *a, **kw):
            return 1
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        logger=SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                               warning=lambda *a, **k: None),
        inbox_store=_St(), _web_loop=None,
    )


def _wire(monkeypatch, *, owns=True, fake_send):
    from src.inbox import autosend_helpers as ah
    import src.inbox.channel_adapters as _ca
    import src.integrations.account_orchestrator as _ao

    async def _false(*a, **k):
        return False
    for name in ("autosend_image", "autosend_voice", "autosend_bazi_kline",
                 "autosend_video"):
        monkeypatch.setattr(ah, name, _false)
    monkeypatch.setattr(_ca, "send_via_adapters", fake_send)

    class _Orch:
        def owns(self, platform, account_id):
            return owns
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    return ah


_QCFG = {"inbox": {"l2_autosend": {"quote_reply": {"enabled": True}}}}
_ROWS3 = [
    _m("out", "嗯嗯", NOW - 600, "9"),
    _m("in", "今天好累啊", NOW - 100, "20"),
    _m("in", "周末要不要一起去爬山？", NOW - 90, "21"),
    _m("in", "对了你上次说的那家火锅店叫什么", NOW - 80, "22"),
]


def test_autosend_passes_reply_to_for_burst(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True, "quote_applied": bool(kw.get("reply_to"))}
    ah = _wire(monkeypatch, fake_send=_fake)
    rqp.reset_stats()
    a = _assistant(_QCFG, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠，超好吃"))
    assert res.get("delivered") is True
    assert len(seen) == 1 and seen[0]["id"] == "22"
    assert rqp.stats_snapshot()["applied"] == 1


def test_autosend_single_inbound_sends_plain(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True}
    ah = _wire(monkeypatch, fake_send=_fake)
    a = _assistant(_QCFG, [_m("in", "火锅店叫什么", NOW - 10, "1")])
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    asyncio.run(send_cb("telegram", "acct", "123", "火锅店叫蜀大侠"))
    assert seen == [None]


def test_autosend_disabled_by_default_sends_plain(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True}
    ah = _wire(monkeypatch, fake_send=_fake)
    a = _assistant({}, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠"))
    assert seen == [None]


def test_autosend_rpa_path_never_quotes(monkeypatch):
    """验收 3：编排器不拥有（RPA 回落适配器不收 reply_to）→ 普通发送，不报错。"""
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True}
    ah = _wire(monkeypatch, owns=False, fake_send=_fake)
    a = _assistant(_QCFG, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("messenger", "acct", "123", "那家火锅店叫蜀大侠"))
    assert res.get("delivered") is True and seen == [None]


def test_autosend_quote_failure_falls_back_to_plain(monkeypatch):
    """验收 4：带引用发送抛异常（目标已删/id 无效）→ 去引用重发一次，不抛。"""
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        if kw.get("reply_to"):
            raise RuntimeError("MESSAGE_ID_INVALID")
        return {"delivered": True}
    ah = _wire(monkeypatch, fake_send=_fake)
    rqp.reset_stats()
    a = _assistant(_QCFG, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠"))
    assert res.get("delivered") is True
    assert len(seen) == 2 and seen[0]["id"] == "22" and seen[1] is None
    assert rqp.stats_snapshot()["fallback_plain"] == 1


def test_autosend_store_error_is_swallowed(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append(kw.get("reply_to"))
        return {"delivered": True}
    ah = _wire(monkeypatch, fake_send=_fake)
    a = _assistant(_QCFG, _ROWS3)

    class _Bad:
        def list_recent_messages(self, *a, **k):
            raise RuntimeError("db locked")
    a.inbox_store = _Bad()
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123", "那家火锅店叫蜀大侠"))
    assert res.get("delivered") is True and seen == [None]


def test_autosend_bubbles_only_first_part_quotes(monkeypatch):
    seen = []

    async def _fake(shim, platform, account_id, chat_key, text, adapters, **kw):
        seen.append((text, kw.get("reply_to")))
        return {"delivered": True, "quote_applied": bool(kw.get("reply_to"))}
    ah = _wire(monkeypatch, fake_send=_fake)

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cfg = {"inbox": {
        "l2_autosend": {"quote_reply": {"enabled": True}},
        "reply_style": {"bubbles": {
            "enabled": True, "min_total_chars": 0, "min_tail_chars": 0,
            "gap_sec_lo": 0, "gap_sec_hi": 0, "per_char_sec": 0}},
    }}
    a = _assistant(cfg, _ROWS3)
    send_cb, _ = ah.build_autosend_callbacks(a, SimpleNamespace(state=SimpleNamespace()), True)
    res = asyncio.run(send_cb("telegram", "acct", "123",
                              "那家火锅店叫蜀大侠\n在春熙路那边\n超好吃的"))
    assert res.get("parts_sent") == 3
    assert seen[0][1] is not None and seen[0][1]["id"] == "22"
    assert seen[1][1] is None and seen[2][1] is None
