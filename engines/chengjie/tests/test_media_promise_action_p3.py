# -*- coding: utf-8 -*-
"""#259 #252 P-3 媒体承诺执行型——「要么发，要么拒，不许说发了」门禁。

6SRA2B 实录（WhatsApp，2026-09-08 14:49–14:51）：14:49:20 真发一张（回执 mid）→
14:49:51「Just took this one for you—」第二句照片配文体放行但无图 → 客户「Where? I
didn't get the other picture」（lie_caught 认出但只给 hint）→ 14:50「Ah, here it is—
maybe it took a moment to load on your end」→ 客户「Where honey? No picture」（未识别）
→ 14:51「Sorry love, I thought it went through—let me try sending it again now」。
两道守卫都「看见了却没拦」：识别函数在，动作权没有。本文件守五段接线：

- A 文字绑动作：[PHOTO] 请求失败 → 正文强制进承诺链改写（``[media-bind]``）；
- B 承诺句无已回执 send_media → strip → rewrite → review（``[media_promise]``）；
- C lie_caught 固定动作：重发上一张 / 强制实话 / 复诉转人工 + 复诉词补全；
- D photos=false → 索图 20 例零承诺（诚实拒绝模板）；
- E 同会话 5 分钟内第二句照片配文体必须附图，否则改写。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from src.ai import outbound_promise_guard as g
from src.ai.companion_selfie import detect_media_complaint
from src.inbox import autosend_helpers, image_autosend as ia
from src.inbox.autosend_helpers import build_autosend_callbacks
from src.inbox.normalizer import conv_id

_T1 = "Just took this one for you—hope you like it 😊"
_T2 = "Ah, here it is—maybe it took a moment to load on your end…"
_T3 = "Sorry love, I thought it went through—let me try sending it again now…"


# ── 词表：B 段承诺 / 断言 / 假声明每条 + 客户原话引用不误伤 ─────────────────────
@pytest.mark.parametrize("text", [
    _T1, _T2, _T3,
    "here it is!", "I sent you the pic", "took this for you just now",
    "just took this, what do you think?", "let me send it over",
    "check your phone babe", "it went through on my side", "should be there by now",
    "发你了呀", "看手机～", "刚拍的哈", "给你发了啦",
])
def test_caption_claim_wordlist_hits(text):
    assert g.detect_photo_caption_claim(text) == "image"


@pytest.mark.parametrize("text", [
    "we went through a lot together this year",
    "here we are at the beach lol",
    "took a moment to think about what you said",
    "别老看手机啦，早点睡",
    'you asked "did you send it?" — haha, patience~',
    "did you send me your address?",
    "宝贝想我了没？我刚下班～",
])
def test_caption_claim_wordlist_no_false_positive(text):
    assert g.detect_photo_caption_claim(text) == ""


def test_strip_photo_caption_claims_keeps_rest():
    out = g.strip_photo_caption_claims(_T1 + ". Anyway, how was your day?")
    assert "took this" not in out and "how was your day" in out


def test_fixed_templates_pass_all_detectors():
    for fn in (g.honest_no_photo_line, g.lie_caught_honest_line):
        for smp in ("你好", "hello", "こんにちは", "안녕"):
            t = fn(smp)
            assert t.strip()
            assert g.detect_photo_caption_claim(t) == ""
            # C 段②禁「加载慢 / 再试 / 网络」三类借口
            low = t.lower()
            for bad in ("load", "try again", "network", "signal", "加载", "再试", "网络", "信号"):
                assert bad not in low


# ── C 段复诉词 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Where? I didn't get the other picture", "Where honey? No picture",
    "still nothing", "nothing came through", "I never got it",
    "没收到啊", "什么都没收到", "图在哪", "还是没有",
])
def test_lie_caught_wordlist(text):
    assert detect_media_complaint(text) == "lie_caught"


def test_lie_caught_weak_words_need_pending():
    assert detect_media_complaint("Where?") == ""
    assert detect_media_complaint("Where?", media_pending=True) == "lie_caught"
    assert detect_media_complaint("哪呢", media_pending=True) == "lie_caught"
    assert detect_media_complaint("没有啊", media_pending=True) == "lie_caught"
    # 弱词不误伤日常问句
    assert detect_media_complaint("where are you from?", media_pending=True) == ""
    assert detect_media_complaint("我明天去上海，你在哪个城市呀", media_pending=True) == ""
    assert detect_media_complaint("nothing much, you?", media_pending=True) == ""


# ── image_autosend：回执契约 + 账本 ──────────────────────────────────────────────
def test_send_result_and_receipt_ledger(monkeypatch):
    assert ia._send_result(True) == (True, "")
    assert ia._send_result({"delivered": True, "message_id": "3EB0"}) == (True, "3EB0")
    assert ia._send_result({"delivered": False}) == (False, "")
    ck = "wa:acc:peer-receipt"
    ia.note_media_receipt(ck, media_id="m1", path="/tmp/a.jpg", mid="3EB0")
    rec = ia.last_media_receipt(ck)
    assert rec["mid"] == "3EB0" and rec["path"] == "/tmp/a.jpg"
    assert ia.resolve_last_sent_media(ck)["media_id"] == "m1"
    # 跨重启回落：相册投放账本 → 条目文件
    class _St:
        def sent_history(self, conv_key):
            return {"items": [{"id": "old", "ts": 1.0}, {"id": "new", "ts": 2.0}]}
        def get(self, mid):
            return {"id": mid, "file_path": f"/album/{mid}.jpg", "url": "", "media_type": "photo"}
    import src.companion.persona_media_store as _pms
    monkeypatch.setattr(_pms, "get_persona_media_store", lambda: _St())
    got = ia.resolve_last_sent_media("wa:acc:never-in-memory")
    assert got["media_id"] == "new" and got["media_type"] == "image"
    assert ia.resolve_last_sent_media("") == {}


# ── deliver 编排回放 ────────────────────────────────────────────────────────────
class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.tags = {}
        self.handoff = {}

    def list_recent_messages(self, cid, limit=20):
        return list(self.rows)[-limit:]

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)

    def set_handoff_meta(self, cid, meta):
        self.handoff[cid] = meta


def _assistant(rows, cfg=None, ai=None):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg or {}),
        logger=logging.getLogger("p3"),
        inbox_store=_Store(rows), _web_loop=None, ai_client=ai,
    )


def _web_app():
    return SimpleNamespace(state=SimpleNamespace())


class _Orch:
    def __init__(self):
        self.media_calls = []
        self.deliver = True

    def owns(self, platform, account_id):
        return False

    def owns_media(self, platform, account_id):
        return True

    async def send_media(self, platform, account_id, chat_key, **kw):
        self.media_calls.append(kw)
        return {"delivered": self.deliver, "message_id": "3EB0FB70" if self.deliver else ""}


def _patch(monkeypatch, orch, image_result=False):
    async def _fake_send_via(shim, platform, account_id, chat_key, text, adapters, **kw):
        return {"ok": True, "delivered_as": "text", "echo": text}
    import src.inbox.channel_adapters as _ca
    monkeypatch.setattr(_ca, "send_via_adapters", _fake_send_via)
    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: orch)
    calls = []

    async def _fake_image(assistant, platform, account_id, chat_key, text,
                          assume_intent="", assume_scene="", directive_override=None):
        calls.append({"assume": assume_intent, "directive": directive_override})
        res = image_result(directive_override, assume_intent) if callable(image_result) else image_result
        if res:
            ia.note_media_receipt(f"{platform}:{account_id}:{chat_key}",
                                  media_id="38197800", path="/album/38197800.jpg",
                                  mid="3EB0FB70")
        return bool(res)

    async def _false(*a, **k):
        return False
    monkeypatch.setattr(autosend_helpers, "autosend_image", _fake_image)
    monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
    monkeypatch.setattr(autosend_helpers, "autosend_bazi_kline", _false)
    monkeypatch.setattr(autosend_helpers, "autosend_song", _false)
    monkeypatch.setattr(autosend_helpers, "autosend_video", _false)
    return calls


_P, _A, _C = "whatsapp", "acc", "peer6SRA2B"


def _run(a, text, orig=None):
    send_cb, _ = build_autosend_callbacks(a, _web_app(), True)
    return asyncio.run(send_cb(_P, _A, _C, text, original_text=orig))


def test_replay_6sra2b_turn1_directive_sent_binds_caption(monkeypatch):
    """第一轮：模型 [PHOTO] 请求 → 图链真发（回执 mid）→ 图带配文出站。"""
    now = time.time()
    rows = [{"direction": "in", "text": "send me a picture", "ts": now - 5}]
    orch = _Orch()
    calls = _patch(monkeypatch, orch, image_result=lambda d, a: bool(d))
    cfg = {"companion": {"selfie": {"enabled": True}}}
    res = _run(_assistant(rows, cfg), _T1 + "\n[PHOTO selfie cozy room, evening]")
    assert res["delivered_as"] == "image"
    assert calls[0]["directive"] and calls[0]["directive"]["kind"] == "selfie"
    assert ia.last_media_receipt(f"{_P}:{_A}:{_C}")["mid"] == "3EB0FB70"


def test_replay_6sra2b_turn2_second_caption_without_image_rewritten(monkeypatch):
    """第二轮（14:49:51）：5 分钟内刚真发过、又一句配文体但图链没发 → 兑现失败 →
    改写为无承诺（E 段 + B 段）。"""
    now = time.time()
    rows = [
        {"direction": "in", "text": "send me a picture", "ts": now - 40},
        {"direction": "out", "text": "[图片] " + _T1, "media_type": "image", "ts": now - 31},
        {"direction": "in", "text": "wow you look great", "ts": now - 10},
    ]
    orch = _Orch()
    calls = _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), _T1)
    assert res["delivered_as"] == "text"
    assert g.detect_photo_caption_claim(res["echo"]) == ""
    assert "took this" not in res["echo"]
    assert [c["assume"] for c in calls] == ["", "selfie"]   # 先常规、后兑现（附第二张）


def test_replay_6sra2b_turn3_lie_caught_resends_same_photo(monkeypatch):
    """第三轮：客户「Where? I didn't get the other picture」→ 固定动作①重发上一张
    （模板配文，不经 LLM），模型那句「here it is—maybe it took a moment to load」不出站。"""
    now = time.time()
    ck = f"{_P}:{_A}:{_C}"
    ia.note_media_receipt(ck, media_id="38197800", path="/album/38197800.jpg", mid="3EB0FB70")
    rows = [
        {"direction": "in", "text": "send me a picture", "ts": now - 90},
        {"direction": "out", "text": "[图片] " + _T1, "media_type": "image", "ts": now - 80},
        {"direction": "out", "text": "hehe glad you like it", "ts": now - 40},
        {"direction": "in", "text": "Where? I didn't get the other picture", "ts": now - 5},
    ]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), _T2)
    assert res["delivered_as"] == "image"
    assert len(orch.media_calls) == 1
    kw = orch.media_calls[0]
    assert kw["media_path"] == "/album/38197800.jpg"
    assert kw["caption"] == g.lie_caught_resend_caption(_T2)
    assert "load" not in kw["caption"]


def test_replay_6sra2b_turn4_second_lie_caught_handoff(monkeypatch):
    """第四轮：「Where honey? No picture」连续第二次 lie_caught → 转人工 + 标签，
    模型那句「let me try sending it again」不出站（无第三轮谎话）。"""
    now = time.time()
    rows = [
        {"direction": "out", "text": "[图片] " + _T1, "media_type": "image", "ts": now - 120},
        {"direction": "in", "text": "Where? I didn't get the other picture", "ts": now - 60},
        {"direction": "out", "text": "[图片] " + g.lie_caught_resend_caption("x"),
         "media_type": "image", "ts": now - 50},
        {"direction": "in", "text": "Where honey? No picture", "ts": now - 5},
    ]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    a = _assistant(rows)
    res = _run(a, _T3)
    assert res["delivered_as"] == "suppressed_media_complaint_handoff"
    assert orch.media_calls == []
    cid = conv_id(_P, _A, _C)
    assert "需人工" in a.inbox_store.tags.get(cid, [])
    assert a.inbox_store.handoff[cid]["reason"] == "media_lie_caught_repeat"


def test_lie_caught_never_sent_forces_honest_template(monkeypatch):
    """C 段②：AI 承诺过但从未真发 → 客户「没收到」→ 强制实话模板（非加载慢 / 再试）。"""
    now = time.time()
    ck = f"{_P}:{_A}:{_C}"
    ia._LAST_RECEIPT.pop(ck, None)
    import src.companion.persona_media_store as _pms
    monkeypatch.setattr(_pms, "get_persona_media_store", lambda: None)
    rows = [
        {"direction": "in", "text": "send me a pic", "ts": now - 60},
        {"direction": "out", "text": "sending you a photo now~", "ts": now - 50},
        {"direction": "in", "text": "I didn't get anything", "ts": now - 5},
    ]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), _T3)
    assert res["delivered_as"] == "text"
    assert res["echo"] == g.lie_caught_honest_line(_T3)
    assert orch.media_calls == []


def test_lie_caught_weak_word_ignored_without_pending(monkeypatch):
    """「Where?」在没有任何发图悬置的会话里不是投诉 → 正常链，正文原样。"""
    now = time.time()
    rows = [
        {"direction": "out", "text": "guess where I am right now", "ts": now - 50},
        {"direction": "in", "text": "Where?", "ts": now - 5},
    ]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), "at the little cafe near my place~")
    assert res["echo"] == "at the little cafe near my place~"


def test_directive_unfulfilled_forces_rewrite_even_if_wordlist_misses(monkeypatch):
    """A 段：[PHOTO] 请求失败 → 即使正文措辞词表认不出，也回喂模型重生一句无照片措辞。"""
    seen = {}

    class _AI:
        async def chat(self, prompt):
            seen["prompt"] = prompt
            return "hehe you always know how to make me smile, what are you up to?"
    now = time.time()
    rows = [{"direction": "in", "text": "hey", "ts": now - 5}]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    text = "This is me right now, cozy at home 😊\n[PHOTO selfie cozy home]"
    res = _run(_assistant(rows, ai=_AI()), text)
    assert res["delivered_as"] == "text"
    assert "照片" in seen["prompt"] and "没有发出去" in seen["prompt"]
    assert res["echo"].startswith("hehe you always")
    assert "[PHOTO" not in res["echo"]


def test_directive_unfulfilled_no_llm_falls_back_honest(monkeypatch):
    now = time.time()
    rows = [{"direction": "in", "text": "hey", "ts": now - 5}]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), "This is me right now 😊\n[PHOTO selfie cozy home]")
    assert res["delivered_as"] == "text"
    assert res["echo"] == g.honest_no_photo_line("This is me")


def test_review_when_rewrite_still_claims(monkeypatch):
    """B 段：剥空 → LLM 重写仍命中 → review：不发 + 「需人工」。"""
    class _AI:
        async def chat(self, prompt):
            return "ok sending you a photo right now!"
    now = time.time()
    rows = [{"direction": "in", "text": "send me a selfie", "ts": now - 5}]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    # 人设 photos 开（否则走 D 段诚实模板而非 review）
    import src.companion.photo_capability as _pc
    monkeypatch.setattr(_pc, "persona_photos_enabled_by_id", lambda pid: True)
    a = _assistant(rows, ai=_AI())
    res = _run(a, "I'll send you a photo right now~")
    assert res["delivered_as"] == "suppressed_media_promise_review"
    cid = conv_id(_P, _A, _C)
    assert "需人工" in a.inbox_store.tags.get(cid, [])


_ASK_20 = [
    "等我拍一张给你～", "我这就去拍", "拍好啦发你", "照片马上发你", "给你翻张新的",
    "等我找找看有没有存图", "发你一张照片哈", "给你看看我的照片", "刚拍的～",
    "这就给你发过去", "I'll send you a selfie now", "let me take a photo for you",
    "sending you a pic~", "photo is on the way!", "here's a photo of me",
    "let me look through my photos for you", "I'm gonna snap a selfie real quick",
    "just took this one for you", "here it is!", "I just sent it, check your phone",
]


@pytest.mark.parametrize("text", _ASK_20)
def test_photos_off_zero_promises_20(monkeypatch, text):
    """D 段：capabilities.photos=false（无人设＝关）→ 索图 20 例出站零承诺。"""
    now = time.time()
    rows = [{"direction": "in", "text": "send me a photo / 发张照片", "ts": now - 5}]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    res = _run(_assistant(rows), text)
    assert res["delivered_as"] == "text"
    assert g.detect_photo_caption_claim(res["echo"]) == ""
    assert res["echo"].strip()


def test_second_caption_window_zero_disables_e(monkeypatch):
    """E 段可配：second_caption_window_sec=0 → 回旧行为（词表判不出的配文体照发）。"""
    now = time.time()
    rows = [
        {"direction": "in", "text": "send me a picture", "ts": now - 40},
        {"direction": "out", "text": "[图片] x", "media_type": "image", "ts": now - 31},
        {"direction": "in", "text": "nice", "ts": now - 10},
    ]
    orch = _Orch()
    _patch(monkeypatch, orch, image_result=False)
    cfg = {"companion": {"media_promise_guard": {"second_caption_window_sec": 0}}}
    res = _run(_assistant(rows, cfg), _T1)
    assert res["echo"] == _T1
