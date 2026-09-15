# -*- coding: utf-8 -*-
"""Q-39 发送真相四闸门禁（#327 #326 #323 #324，2026-09-15）。

A（#327 9PYWPG 跟图循环）：① 入站是图 + 客户配文无索图 → 零出图、承诺兑现链不放行（撤回改写）；
   ② 明确「send me a pic」→ 照常出图；③ 同会话跟随冷却 / 每日上限；④ 配文近重复换配文。
B（#326 THBSHN 翻译空串）：① ai:empty → 同引擎重试 → 成功投递；② 三步都失败 → HOLD +
   conv_state 人话 + retranslate 端点 / worker 补投口；③ lang_unknown / target_lang_mismatch 不重试。
C（#323 XG3UZU 要图意图闸）：① 「enjoy the view」→ skip=no_intent 零 miss 零红条；
   ② 「May I see a pic」→ 照常 miss / 红条；③ 同 mid 不跑两次。
D（#324 YS54YK 全部账号筹码）：node 抽源——列表顶渲染调用 _isSysTag / single 不出系统族 /
   chip 带 data-ptag / 文案走 _sysTagLabel；词条 zh / en / 繁体齐平。
红线复核：不改 Q-6 打分（pick_media 签名不动）/ note_album_miss / album_miss_addendum / detect_offer_media。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from src.inbox import image_send_gate as isg

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"


# ── 假件 ─────────────────────────────────────────────────────────────────────

class _KV:
    """InboxStore 的 app_settings 通用 KV 子集（album_miss_marker / xlate_hold_marker 同款）。"""

    def __init__(self, mode="auto_ai"):
        self.kv = {}
        self.mode = mode
        self.writes = 0

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.writes += 1
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_tags(self, cid):
        return []

    def get_handoff_meta(self, cid):
        return {}


class _Health:
    def dump(self):
        return {"inbox_health": {}, "sessions": {}}


def _row(i, **kw):
    # 缺省打 kind:selfie（Q-6 E 通用池按 kind 取；完全无词无标的行只走 random 兜底＝默认关）
    r = {"id": str(i), "enabled": True, "media_type": "photo", "triggers": kw.get("triggers", []),
         "weight": 1, "tags": kw.get("tags", ["kind:selfie"]), "auto_meta": kw.get("auto_meta", {}),
         "min_bond_level": 0, "file_path": "/disk/p%s.jpg" % i, "url": "/static/p%s.jpg" % i,
         "caption": kw.get("caption", "")}
    r.update({k: v for k, v in kw.items() if k not in ("triggers", "tags", "auto_meta", "caption")})
    return r


class _AlbumSt:
    def __init__(self, rows):
        self.rows = rows
        self.hits = []
        self.sends = []

    def list(self, *a, **k):
        return list(self.rows)

    def sent_history(self, *a, **k):
        return {"ids": set(), "series": set(), "file_keys": set(), "items": []}

    def get(self, mid):
        return next((r for r in self.rows if str(r.get("id")) == str(mid)), None)

    def record_hit(self, mid):
        self.hits.append(mid)

    def record_send(self, *a, **k):
        self.sends.append((a, k))


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    isg._reset_for_tests()
    import src.companion.photo_capability as pc
    monkeypatch.setattr(pc, "persona_photos_enabled_by_id", lambda _pid: True)
    yield
    isg._reset_for_tests()


def _cfg(**extra):
    c = {"companion": {"selfie": {"enabled": True}}}
    c.update(extra)
    return c


def _recorder():
    sent = []

    async def send_fn(mp, mu, mt, cap, inbox):
        sent.append({"path": mp, "url": mu, "type": mt, "cap": cap, "inbox": inbox})
        return {"delivered": True, "message_id": "m%d" % len(sent)}
    return sent, send_fn


# ── A / C：意图闸纯函数 ────────────────────────────────────────────────────

def test_inbound_image_detection_by_marker_placeholder_and_media_type():
    assert isg.inbound_is_image("[图片内容] 一名年轻男子在海边的自拍照，戴着墨镜") is True
    assert isg.inbound_is_image("[图片]") is True
    assert isg.inbound_is_image("hello", media_type="image") is True
    assert isg.inbound_is_image("send me a pic") is False
    assert isg.customer_words("[图片内容] 一名男子的自拍") == ""
    assert isg.customer_words("look at me!\n[图片内容] 一名男子的自拍") == "look at me!"
    assert isg.customer_words("[语音转录] send me a pic") == "send me a pic"


def test_intent_gate_vlm_description_is_not_a_request_9pywpg():
    """#327 真闸：识图行里的「自拍」曾被 detect_selfie_request 当索图 → 通用池出图。"""
    g = isg.compute_image_intent("[图片内容] 一名年轻男子的自拍照，站在户外微笑", [],
                                 inbound_media_type="image")
    assert g.intent is False and g.inbound_image is True and g.reason == isg.REASON_INBOUND_IMAGE
    # 承诺兑现 / LLM 指令撞上「客户刚发了图」同样不放行
    g2 = isg.compute_image_intent("[图片内容] 自拍", [], inbound_media_type="image",
                                  assume_intent="selfie")
    assert g2.intent is False and g2.trigger == isg.TRIGGER_COMMITMENT
    g3 = isg.compute_image_intent("[图片内容] 自拍", [], inbound_media_type="image",
                                  directive_override={"kind": "selfie", "scene": "beach"})
    assert g3.intent is False and g3.trigger == isg.TRIGGER_DIRECTIVE
    # 图 + 客户自己敲了索图配文 → 放行（配文才是客户的话）
    g4 = isg.compute_image_intent("send me a pic of you too\n[图片内容] 一名男子的自拍", [],
                                  inbound_media_type="image")
    assert g4.intent is True and g4.trigger == isg.TRIGGER_ASK and g4.words == "send me a pic of you too"


def test_intent_gate_text_paths():
    assert isg.compute_image_intent("send me a pic", []).trigger == isg.TRIGGER_ASK
    assert isg.compute_image_intent("發個照片給我看看嘛", []).trigger == isg.TRIGGER_ASK
    # 承诺兑现（文本入站无索图）照旧放行
    g = isg.compute_image_intent("how was your day", [], assume_intent="selfie")
    assert g.intent is True and g.trigger == isg.TRIGGER_COMMITMENT
    # 运营触发词 = 显式点名（子串口径与 Q-6 _triggers_hit 同）
    g = isg.compute_image_intent("给我跳舞看看", [], trigger_terms=["跳舞"])
    assert g.intent is True and g.trigger == isg.TRIGGER_KEYWORD
    assert isg.compute_image_intent("给我跳舞看看", []).intent is False, "无触发词配置时不是索图"
    # offer-accept 桥
    hist = [{"role": "assistant", "content": "要不要看看我的照片？"}]
    g = isg.compute_image_intent("好呀", hist)
    assert g.intent is True and g.trigger == isg.TRIGGER_OFFER_ACCEPT
    # 闲聊
    g = isg.compute_image_intent("今天心情不错", [])
    assert g.intent is False and g.reason == isg.REASON_NO_INTENT


def test_strict_scene_kind_requires_ask_verb_xg3uzu():
    """#323：「the view」泛匹配 outdoor 不再单独置真；「send me a landscape pic」仍算。"""
    from src.companion.persona_media import requested_scene_kind
    s = "I try to take my time an enjoy the view"
    assert requested_scene_kind(s) == "outdoor", "Q-6 requested_scene_kind 语义不动"
    assert isg.strict_requested_scene_kind(s) == ""
    assert isg.compute_image_intent(s, []).intent is False
    assert isg.strict_requested_scene_kind("send me a landscape pic") == "outdoor"
    assert isg.strict_requested_scene_kind("发张风景照") == "outdoor"
    assert isg.compute_image_intent("May I see a pic?", []).trigger == isg.TRIGGER_ASK


# ── C：pick_registered_media 前闸 + 同 mid ──────────────────────────────────

def test_no_intent_skips_match_miss_and_red_bar(monkeypatch):
    from src.inbox import album_miss_marker as amm
    from src.inbox import image_autosend as ia
    inbox = _KV()
    monkeypatch.setattr(amm, "_default_store", lambda: inbox)
    album = _AlbumSt([_row(1, triggers=["跳舞"], tags=["kind:selfie"])])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    pm_calls = []
    import src.companion.persona_media as pm
    _orig = pm.pick_media

    def _spy(*a, **k):
        pm_calls.append(1)
        return _orig(*a, **k)
    monkeypatch.setattr(pm, "pick_media", _spy)
    ck = "whatsapp:acct1:coop"
    ia.consume_album_miss(ck)
    assert ia.pick_registered_media(_cfg(), "nori", "I try to take my time an enjoy the view",
                                    conv_key=ck) is None
    assert pm_calls == [], "无意图不得跑 Q-6 匹配"
    assert ia.consume_album_miss(ck) == {}, "无意图不写 note_album_miss"
    assert amm.get(ck, store=inbox) is None, "无意图不刷红条"
    # 真索图 → 照常匹配 / miss / 红条
    assert ia.pick_registered_media(_cfg(), "nori", "May I see a pic?", conv_key=ck) is None
    assert pm_calls == [1]
    assert ia.consume_album_miss(ck), "索图无命中 → Q-6 note 照旧"
    assert amm.get(ck, store=inbox) is not None, "索图无命中 → 红条照旧"


def test_same_mid_runs_album_match_once(monkeypatch):
    from src.inbox import album_miss_marker as amm
    from src.inbox import image_autosend as ia
    inbox = _KV()
    monkeypatch.setattr(amm, "_default_store", lambda: inbox)
    album = _AlbumSt([_row(1, triggers=["跳舞"])])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    ck = "whatsapp:acct1:mid"
    q = "send me a pic please"
    assert ia.pick_registered_media(_cfg(), "nori", q, conv_key=ck, inbound_mid="m-1") is None
    n1 = int((amm.get(ck, store=inbox) or {}).get("n") or 0)
    assert n1 == 1
    # 同 mid 第二次（拟稿期自探 + 投递期再跑）→ skip=dup_mid，红条不再 +1
    assert ia.pick_registered_media(_cfg(), "nori", q, conv_key=ck, inbound_mid="m-1") is None
    assert int((amm.get(ck, store=inbox) or {}).get("n") or 0) == 1
    # 新 mid 才再跑
    assert ia.pick_registered_media(_cfg(), "nori", q, conv_key=ck, inbound_mid="m-2") is None
    assert int((amm.get(ck, store=inbox) or {}).get("n") or 0) == 2


# ── A：run_autosend_image 端到端 ──────────────────────────────────────────────

async def test_inbound_image_no_ask_sends_nothing_even_with_commitment(monkeypatch):
    from src.inbox import image_autosend as ia
    album = _AlbumSt([_row(1, caption="")])   # 无触发词 = 通用池（事故里的 Mizuki 相册）
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    sent, send_fn = _recorder()
    desc = "[图片内容] 一名年轻男子的自拍照，戴着帽子在户外微笑"
    ok = await ia.run_autosend_image(_cfg(), "whatsapp", "19892968016", "cam", "mizuki", desc, [],
                                     send_fn=send_fn, inbound_media_type="image", inbound_mid="x1")
    assert ok is False and sent == []
    # 承诺兑现路径同样不出相册（LLM「分享旧照」承诺交撤回改写）
    ok2 = await ia.run_autosend_image(_cfg(), "whatsapp", "19892968016", "cam", "mizuki", desc, [],
                                      send_fn=send_fn, assume_intent="selfie",
                                      inbound_media_type="image", inbound_mid="x1")
    assert ok2 is False and sent == []
    snap = ia.metrics_snapshot()
    assert snap["fallback_reasons"].get(isg.REASON_INBOUND_IMAGE, 0) >= 1


async def test_explicit_ask_still_sends_and_logs_album_send(monkeypatch, caplog):
    import logging
    from src.inbox import image_autosend as ia
    album = _AlbumSt([_row(1, caption="嘿嘿")])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    sent, send_fn = _recorder()
    with caplog.at_level(logging.INFO, logger="src.inbox.image_autosend"):
        ok = await ia.run_autosend_image(_cfg(), "whatsapp", "a", "c", "mizuki",
                                         "send me a selfie of you", [], send_fn=send_fn,
                                         inbound_mid="x2")
    assert ok is True and len(sent) == 1
    line = next((r.getMessage() for r in caplog.records if "[album_send]" in r.getMessage()
                 and "caption_dedup" not in r.getMessage()), "")
    assert line, "每次出图必落 [album_send] 行"
    for k in ("trigger=ask", "intent=1:ask", "candidates=", "picked=1", "caption_src=registry"):
        assert k in line, line


def test_follow_cooldown_only_for_non_explicit_triggers():
    ck = "whatsapp:a:cool"
    t0 = 1_800_000_000.0
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_COMMITMENT, now=t0) == ""
    isg.note_follow_sent(ck, now=t0)
    # 30min 内：承诺兑现 / offer / LLM 指令 → 冷却；客户亲口要 → 放行
    for trig in (isg.TRIGGER_COMMITMENT, isg.TRIGGER_OFFER_ACCEPT, isg.TRIGGER_DIRECTIVE):
        assert isg.follow_budget_check(ck, {}, trig, now=t0 + 600) == isg.REASON_FOLLOW_COOLDOWN, trig
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_ASK, now=t0 + 600) == ""
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_KEYWORD, now=t0 + 600) == ""
    # 过窗放行；冷却可配 0 = 关
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_COMMITMENT, now=t0 + 31 * 60) == ""
    assert isg.follow_budget_check(ck, {"inbox": {"image_autosend": {"follow_cooldown_min": 0}}},
                                   isg.TRIGGER_COMMITMENT, now=t0 + 60) == ""
    # 每日上限对所有触发生效（默认 6）
    for i in range(5):
        isg.note_follow_sent(ck, now=t0 + 100 * (i + 1))
    assert isg.follow_stats(ck, now=t0 + 1000)["today"] == 6
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_ASK, now=t0 + 1000) == isg.REASON_FOLLOW_DAILY_MAX
    # 隔天清零（按本地日）
    assert isg.follow_budget_check(ck, {}, isg.TRIGGER_ASK, now=t0 + 36 * 3600) == ""


async def test_daily_max_end_to_end_then_text_only(monkeypatch):
    from src.inbox import image_autosend as ia
    album = _AlbumSt([_row(i, caption="c%d" % i) for i in range(1, 12)])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    sent, send_fn = _recorder()
    ck = "whatsapp:a:cool"
    cfg = _cfg(inbox={"image_autosend": {"follow_daily_max": 2, "follow_cooldown_min": 30}})
    ok1 = await ia.run_autosend_image(cfg, "whatsapp", "a", "cool", "mizuki", "send me a selfie", [],
                                      send_fn=send_fn)
    ok2 = await ia.run_autosend_image(cfg, "whatsapp", "a", "cool", "mizuki", "another selfie pls", [],
                                      send_fn=send_fn)
    assert ok1 is True and ok2 is True and len(sent) == 2, "显式索图不吃冷却"
    ok3 = await ia.run_autosend_image(cfg, "whatsapp", "a", "cool", "mizuki", "one more selfie", [],
                                      send_fn=send_fn)
    assert ok3 is False and len(sent) == 2, "超每日上限 → 只发文字"
    assert ia.metrics_snapshot()["fallback_reasons"].get(isg.REASON_FOLLOW_DAILY_MAX, 0) >= 1
    assert isg.follow_stats(ck)["today"] == 2


def test_follow_cfg_defaults_and_override():
    assert isg.follow_cfg({}) == {"follow_cooldown_min": 30.0, "follow_daily_max": 6}
    assert isg.follow_cfg({"inbox": {"image_autosend": {"follow_daily_max": 0,
                                                        "follow_cooldown_min": "x"}}}) == {
        "follow_cooldown_min": 30.0, "follow_daily_max": 0}
    assert isg.follow_budget_check("c", {"inbox": {"image_autosend": {"follow_daily_max": 0,
                                                                      "follow_cooldown_min": 0}}},
                                   isg.TRIGGER_COMMITMENT) == ""


def test_caption_dedup_swaps_near_duplicates():
    ck = "wa:a:cap"
    for c in ("Found this old one of me, thought you'd like it",
              "still kinda cute right", "hope you like it"):
        isg.note_caption_sent(ck, c)
    cap, src, sim = isg.dedup_caption(ck, "Found this old one of me, thought you'd like it!", "llm",
                                      [("从相册翻到的旧照", "fixed")])
    assert cap == "从相册翻到的旧照" and src == "fixed" and sim >= 0.8
    # 备选也雷同 → 空配文
    cap2, src2, _ = isg.dedup_caption(ck, "hope you like it :)", "llm",
                                      [("hope you like it", "registry")])
    assert cap2 == "" and src2 == "dedup_empty"
    # 不像 → 原样
    cap3, src3, sim3 = isg.dedup_caption(ck, "刚下班，累瘪了", "llm", [])
    assert cap3 == "刚下班，累瘪了" and src3 == "llm" and sim3 < 0.8
    assert isg.recent_captions(ck) == ["Found this old one of me, thought you'd like it",
                                       "still kinda cute right", "hope you like it"]


async def test_caption_dedup_applied_on_registry_send(monkeypatch, caplog):
    import logging
    from src.inbox import image_autosend as ia
    album = _AlbumSt([_row(1, caption="hope you like it"), _row(2, caption="hope you like it")])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    sent, send_fn = _recorder()
    ck = "whatsapp:a:dd"
    isg.note_caption_sent(ck, "hope you like it")
    with caplog.at_level(logging.INFO, logger="src.inbox.image_autosend"):
        ok = await ia.run_autosend_image(_cfg(), "whatsapp", "a", "dd", "mizuki",
                                         "send me a selfie", [], send_fn=send_fn)
    assert ok is True and len(sent) == 1
    assert sent[0]["cap"] != "hope you like it"
    assert any("[album_send] caption_dedup" in r.getMessage() for r in caplog.records)


def test_fulfill_chain_blocked_for_inbound_image_static():
    """autosend_helpers 兑现链：入站是图且无索图 → 不 fulfill 直接撤回改写（静态钉接线）。"""
    src = (_ROOT / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8")
    assert "_q39_img_no_ask" in src
    assert re.search(r"and _bind_reason != \"directive_unfulfilled\" \\\n\s+and not _q39_img_no_ask", src)
    assert "inbound_media_type=_inbound_mt" in src and "inbound_mid=_inbound_mid" in src
    # 兑现闸判据来自同一函数（compute_image_intent），不另造词表
    assert "compute_image_intent as _q39_gate" in src


def test_persona_reply_prealbum_probe_uses_gate_static():
    src = (_ROOT / "src" / "inbox" / "persona_reply.py").read_text(encoding="utf-8")
    assert "compute_image_intent as _q39_intent" in src
    assert "strict_requested_scene_kind as _q39_scene_kind" in src
    # 旧的泛匹配置真（requested_scene_kind(inbound) or extract_requested_scene(inbound)）已下线
    assert "requested_scene_kind(inbound)\n" not in src


# ── B：翻译空串可恢复 ─────────────────────────────────────────────────────────

class _Res:
    def __init__(self, translated, ok=True, provider="ai", error=""):
        self.translated_text = translated
        self.ok = ok
        self.provider = provider
        self.error = error


class _TS:
    """首发结果 + retry_once 结果序列（按调用顺序弹出）。"""

    def __init__(self, first, retries=(), nxt="hymt", detect="zh"):
        self.first = first
        self.retries = list(retries)
        self.nxt = nxt
        self._detect = detect
        self.calls = []
        self.retry_calls = []

    def detect_language(self, text):
        return self._detect

    async def translate(self, text, *, target_lang, source_lang, style="chat"):
        self.calls.append((text, target_lang, source_lang))
        return self.first

    def next_engine_after(self, failed, target):
        return self.nxt

    async def retry_once(self, text, *, target_lang, source_lang, style="chat", engine=""):
        self.retry_calls.append(engine)
        return self.retries.pop(0) if self.retries else _Res("", ok=False, error="ai:empty")


class _XStore(_KV):
    def __init__(self, mode="auto_ai", recent=None):
        super().__init__(mode)
        self._recent = list(recent or [])
        self.recorded = []

    def get_conversation(self, cid):
        return {"conversation_id": cid, "language": "en", "contact_id": ""}

    def get_outbound_lang_if_set(self, cid):
        return ""

    def list_recent_messages(self, cid, limit=50, **kw):
        return list(self._recent)

    def record_outbound_translation(self, cid, sent, orig, **kw):
        self.recorded.append((cid, sent, orig))
        return True


@pytest.fixture
def _no_gap(monkeypatch):
    import asyncio as _aio
    monkeypatch.setattr(_aio, "sleep", _fast_sleep)


async def _fast_sleep(_s):
    return None


async def test_ai_empty_retries_same_engine_then_delivers(_no_gap):
    from src.inbox.outbound_translate import translate_outbound_text
    ts = _TS(_Res("", ok=False, provider="ai", error="ai:empty"),
             retries=[_Res("Hi Vince, how's your day going?", provider="ai")])
    st = _XStore()
    item = {"conversation_id": "telegram:kj:vince", "text": "嗨 Vince，今天过得怎么样？",
            "draft_id": "d326"}
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == "Hi Vince, how's your day going?"
    assert ts.retry_calls == ["ai"], "先同引擎重试一次"
    assert "_xlate_hold" not in item
    assert "xlate_hold:telegram:kj:vince" not in st.kv


async def test_ai_empty_then_next_engine_rescues(_no_gap):
    from src.inbox.outbound_translate import translate_outbound_text
    ts = _TS(_Res("", ok=False, provider="ai", error="ai:empty"),
             retries=[_Res("", ok=False, provider="ai", error="ai:empty"),
                      _Res("Hi there, long time!", provider="hymt")], nxt="hymt")
    st = _XStore()
    item = {"conversation_id": "telegram:kj:v2", "text": "嗨，好久不见！"}
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh")
    assert out == "Hi there, long time!"
    assert ts.retry_calls == ["ai", "hymt"]


async def test_three_failures_then_redraft_in_target_lang(_no_gap):
    from src.inbox.outbound_translate import translate_outbound_text
    ts = _TS(_Res("", ok=False, provider="ai", error="ai:TimeoutError: 31.6s"),
             retries=[_Res("", ok=False, error="ai:empty"), _Res("", ok=False, error="hymt:empty")])
    ts._detect = "en"   # 重起草产物语种检测 → en
    st = _XStore(mode="auto_ai")
    got = {}

    async def _redraft(item, target):
        got["target"] = target
        return "Hey Vince, how has your day been?"
    item = {"conversation_id": "telegram:kj:v3", "text": "嗨 Vince，今天过得怎么样？", "draft_id": "d3"}
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh",
                                        redraft=_redraft)
    assert out == "Hey Vince, how has your day been?"
    assert got["target"] == "en" and item.get("_xlate_action") == "redraft"
    assert "_xlate_hold" not in item


async def test_redraft_rejected_when_output_not_in_target_lang(_no_gap):
    from src.inbox.outbound_translate import translate_outbound_text
    ts = _TS(_Res("", ok=False, provider="ai", error="ai:empty"),
             retries=[_Res("", ok=False, error="ai:empty"), _Res("", ok=False, error="hymt:empty")])
    st = _XStore(mode="auto_ai")

    async def _redraft(item, target):
        return "嗨 Vince，今天过得怎么样？"   # 仍是中文 → 不许发
    item = {"conversation_id": "telegram:kj:v4", "text": "嗨 Vince", "draft_id": "d4"}
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh",
                                        redraft=_redraft)
    assert out is None and item["_xlate_hold"]["reason"] == "hymt:empty"
    assert item["_xlate_hold"]["attempts"] == 3


async def test_three_failures_hold_with_marker_and_human_message(_no_gap):
    from src.inbox.autosend_worker import _translate_hold_message
    from src.inbox.conv_state import compute
    from src.inbox.outbound_translate import translate_outbound_text
    from src.inbox import xlate_hold_marker as xhm
    ts = _TS(_Res("", ok=False, provider="ai", error="ai:empty"),
             retries=[_Res("", ok=False, error="ai:empty"), _Res("", ok=False, error="hymt:empty")])
    st = _XStore(mode="review")   # 人审档：不重起草，三次即 HOLD
    item = {"conversation_id": "telegram:kj:v5", "text": "嗨 Vince，今天过得怎么样？", "draft_id": "d5"}
    redraft_called = []

    async def _redraft(item, target):
        redraft_called.append(1)
        return "Hi"
    out = await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh",
                                        redraft=_redraft)
    assert out is None and redraft_called == [], "非全自动档不重起草"
    hold = item["_xlate_hold"]
    assert hold["reason"] == "hymt:empty" and hold["attempts"] == 3 and hold["target"] == "en"
    msg = _translate_hold_message(item)
    assert msg.startswith("translate_hold:hymt:empty:") and "翻译引擎没回话" in msg and "已试 3 次" in msg
    rec = xhm.get("telegram:kj:v5", store=st)
    assert rec and rec["draft_id"] == "d5" and rec["attempts"] == 3 and rec["reason"] == "hymt:empty"
    # conv_state：红 + 人话键 + retranslate 动作带 draft_id；不进 sources（Q-26 契约）
    cs = compute(st, "telegram:kj:v5", platform="telegram", account_id="kj", health=_Health(), config={})
    assert cs["state"] == "xlate_hold" and cs["tone"] == "danger" and cs["will_send"] is False
    assert cs["reason_text_key"] == "inbox.cs.xlate_hold" and cs["action"] == "retranslate"
    assert cs["params"]["draft_id"] == "d5" and cs["params"]["attempts"] == 3
    assert "xlate_hold" not in cs["sources"] and cs["ext"]["xlate_hold"]["reason"] == "hymt:empty"
    # 同会话下一次翻译成功 → note 清
    ts2 = _TS(_Res("Hello!", provider="ai"))
    out2 = await translate_outbound_text({"conversation_id": "telegram:kj:v5", "text": "你好！"},
                                         translation_service=ts2, store=st, source_lang="zh")
    assert out2 == "Hello!" and xhm.get("telegram:kj:v5", store=st) is None
    cs2 = compute(st, "telegram:kj:v5", platform="telegram", account_id="kj", health=_Health(), config={})
    assert cs2["state"] != "xlate_hold"


async def test_lang_unknown_and_mismatch_never_retry(_no_gap):
    from src.inbox.outbound_translate import translate_outbound_text
    # lang_unknown：目标判不出 → 直接 HOLD，引擎一次都不打
    ts = _TS(_Res("x"))
    st = _XStore()
    st.get_conversation = lambda cid: {"conversation_id": cid, "language": "", "contact_id": ""}
    item = {"conversation_id": "c:u:1", "text": "好呀好呀"}
    assert await translate_outbound_text(item, translation_service=ts, store=st, source_lang="zh") is None
    assert item["_xlate_hold"]["reason"] == "lang_unknown" and ts.retry_calls == [] and ts.calls == []
    # target_lang_mismatch：内容判定 → 不重试，三键契约原样
    ts2 = _TS(_Res("", ok=False, provider="ai", error="target_lang_mismatch"))
    st2 = _XStore()
    item2 = {"conversation_id": "c:u:2", "text": "好呀好呀"}
    assert await translate_outbound_text(item2, translation_service=ts2, store=st2, source_lang="zh") is None
    assert ts2.retry_calls == []
    assert set(item2["_xlate_hold"]) == {"reason", "target", "decided_by"}, "M-1 B 三键契约不动"
    assert item2["_xlate_hold"]["reason"] == "target_lang_mismatch" and item2["_xlate_hold"]["target"] == "en"


def test_retryable_error_classifier():
    from src.ai.translation_service import TranslationService
    for e in ("ai:empty", "ai:TimeoutError: 31.6s", "provider_unavailable", "hymt:ReadTimeout",
              "ai:unavailable", "translate_failed"):
        assert TranslationService.is_retryable_error(e), e
    for e in ("target_lang_mismatch", "engine_refusal", "ai:unsupported_target:yue",
              "lang_unknown", "cjk_residue", ""):
        assert not TranslationService.is_retryable_error(e), e
    from src.inbox.outbound_translate import is_retryable_xlate_error, parse_xlate_retry_cfg
    assert is_retryable_xlate_error("translate_exception") is True
    assert parse_xlate_retry_cfg({}) == {"enabled": True, "gap_sec": 2.0, "redraft": True}
    assert parse_xlate_retry_cfg({"inbox": {"l2_autosend": {"translate": {"retry": False}}}})["enabled"] is False


async def test_translation_service_retry_once_bypasses_negative_cache():
    """真 TranslationService：首发 empty 进负缓存；retry_once 直打引擎不吃缓存，成功回填。"""
    from src.ai.translation_engines import EngineResult
    from src.ai.translation_service import TranslationService

    class _Eng:
        name = "ai"
        available = True

        def __init__(self):
            self.n = 0

        def supports_target(self, t):
            return True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            self.n += 1
            if self.n == 1:
                return EngineResult("", "ai", False, "empty")
            return EngineResult("Hello there", "ai", True)
    eng = _Eng()
    svc = TranslationService(engines=[eng])
    r1 = await svc.translate("你好呀朋友", target_lang="en", source_lang="zh")
    assert r1.ok is False and "empty" in (r1.error or "")
    r1b = await svc.translate("你好呀朋友", target_lang="en", source_lang="zh")
    assert r1b.ok is False and eng.n == 1, "负缓存命中：translate 不再打引擎（重试若走它＝没试）"
    r2 = await svc.retry_once("你好呀朋友", target_lang="en", source_lang="zh", engine="ai")
    assert r2.ok is True and r2.translated_text == "Hello there" and eng.n == 2
    r3 = await svc.translate("你好呀朋友", target_lang="en", source_lang="zh")
    assert r3.ok is True and r3.translated_text == "Hello there" and eng.n == 2, "成功回填缓存"
    assert svc.next_engine_after("ai", "en") == ""


async def test_worker_deliver_retranslate_reuses_translate_chain():
    from src.inbox.autosend_worker import AutosendWorker

    class _Svc:
        def list_drafts(self, status="pending", limit=200):
            return []

        def resolve_with_audit(self, draft_id, action, by=""):
            return {"ok": True}
    seen = {}

    async def _tx(item):
        seen["origin"] = item.get("origin")
        seen["retx"] = item.get("_retranslate")
        return "Hi Vince!"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}
    w = AutosendWorker(draft_service=_Svc(), send_callback=None, human_send_callback=_send,
                       translate_callback=_tx, deliver_only=True)
    res = await w.deliver_retranslate({"draft_id": "d326", "conversation_id": "telegram:kj:v",
                                       "platform": "telegram", "account_id": "kj", "chat_key": "v",
                                       "final_text": "嗨 Vince！"})
    assert res.get("ok") is True and sent == ["Hi Vince!"]
    assert seen["origin"] is None and seen["retx"] is True, "补投稿必须再译（不标 manual）"
    assert getattr(w, "total_retranslate_delivered", 0) == 1
    # 仍 HOLD → 失败原样回 translate_hold 文案
    async def _tx_hold(item):
        item["_xlate_hold"] = {"reason": "ai:empty", "target": "en", "decided_by": "profile",
                               "attempts": 3}
        return None
    w2 = AutosendWorker(draft_service=_Svc(), send_callback=None, human_send_callback=_send,
                        translate_callback=_tx_hold, deliver_only=True)
    res2 = await w2.deliver_retranslate({"draft_id": "d327", "conversation_id": "telegram:kj:w",
                                         "platform": "telegram", "account_id": "kj", "chat_key": "w",
                                         "final_text": "嗨"})
    assert res2.get("ok") is False and "translate_hold:ai:empty" in str(res2.get("error"))
    assert sent == ["Hi Vince!"]


def test_retranslate_endpoint_registered_only_for_hold_drafts_static():
    src = (_ROOT / "src" / "web" / "routes" / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    i = src.index('@app.post("/api/unified-inbox/drafts/{draft_id}/retranslate")')
    body = src[i:i + 4000]
    assert "xlate_hold_marker" in body and "HTTPException(409" in body
    assert "deliver_retranslate" in body and "err.inbox.retranslate_not_on_hold" in body
    helpers = (_ROOT / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8")
    assert "redraft=_redraft_in_lang" in helpers and "reply_lang=str(target or \"\")" in helpers


# ── D：全部账号视图列表顶筹码（node 抽源）─────────────────────────────────────

def _node():
    import shutil
    return shutil.which("node")


def _strip_fn_src():
    html = _TPL.read_text(encoding="utf-8")
    m = re.search(r"function _renderTagStrip\(\)\{.*?\n\}\n", html, re.S)
    assert m, "_renderTagStrip 不在"
    return m.group(0)


def test_tag_strip_uses_sys_tag_gate_and_ptag_static():
    fn = _strip_fn_src()
    assert "_isSysTag(" in fn, "列表顶那排必须走 _isSysTag（与 Q-31 面板同判定）"
    assert "MULTI_SEAT" in fn and "_fpSysOpen()" in fn
    assert 'data-ptag="${esc(s.tag)}"' in fn and "data-ptag-x" in fn
    assert "_sysTagLabel(s.tag)" in fn
    assert "onclick=\"_uiBeacon('iflt_striptag');setTagFilter(" not in fn, "不再拼 inline onclick"
    html = _TPL.read_text(encoding="utf-8")
    assert "getElementById('tag-strip'); if(!el||el.__ptagBound) return;" in html, "strip 事件委托"
    lbl = re.search(r"function _sysTagLabel\(t\)\{.*?\n\}", html, re.S).group(0)
    for k in ("inbox.systag.dormant", "inbox.systag.risk", "inbox.systag.stop_contact"):
        assert k in lbl
    # 摘标 / 停联处理后重拉统计
    assert html.count("try{ loadTagStrip(); }catch(_){}") >= 2
    # 零新颜色：本段（去掉注释后）没有新增内联色值
    code = re.sub(r"/\*.*?\*/", "", fn, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    assert not re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![0-9a-zA-Z])", code)


@pytest.mark.skipif(not _node(), reason="node 不在 PATH")
def test_tag_strip_single_seat_hides_sys_tags_node(tmp_path):
    """node 直跑 _renderTagStrip：single 不出系统族；multi / 面板打开 / 正按系统标签筛 → 出，
    chip 带 data-ptag + 人话 + 激活 ×。"""
    html = _TPL.read_text(encoding="utf-8")
    fn = _strip_fn_src()
    is_sys = re.search(r"function _isSysTag\(t\)\{.*?\n\}", html, re.S).group(0)
    lbl = re.search(r"function _sysTagLabel\(t\)\{.*?\n\}", html, re.S).group(0)
    tip = re.search(r"function _sysTagTip\(t\)\{.*?\n\}", html, re.S).group(0)
    js = r"""
const _T={'inbox.systag.dormant':'长期未回','inbox.systag.risk':'需留意','inbox.systag.stop_contact':'别再联系',
 'inbox.systag.dormant_t':'d','inbox.systag.risk_t':'r','inbox.systag.stop_contact_t':'s',
 'inbox.filter.chip_clear':'清除','inbox.strip.manage_t':'m','tko.tag.needs_human':'需人工','tko.tag.takeover':'接管','inbox.risk.tag_medium':'风险·中'};
global.window={T:k=>_T[k]||k};
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const _NEEDS_HUMAN_TAG='需人工', _RK_MEDIUM_TAG='risk:medium', _STOP_CONTACT_TAG='客户要求停联';
const _TK_RE=[['tko',/^人工接管中$/],['attn',/^需人工$/]];
function _tagKind(t){ var s=String(t||''); for(var i=0;i<_TK_RE.length;i++){ if(_TK_RE[i][1].test(s)) return _TK_RE[i][0]; } return ''; }
const _tagLibColor={};
let el={cls:new Set(),innerHTML:'',classList:{add(c){el.cls.add(c)},remove(c){el.cls.delete(c)}}};
global.document={getElementById:id=>id==='tag-strip'?el:null};
function _renderPanelTags(){}
let MULTI_SEAT=false, tagFilter='', fpOpen=false;
function _fpSysOpen(){ return fpOpen; }
const _tagStatsCache=[{tag:'dormant:ignored',count:4},{tag:'risk:medium',count:3},{tag:'risk:high',count:1},{tag:'客户要求停联',count:1},{tag:'VIP',count:2}];
""" + is_sys + "\n" + lbl + "\n" + tip + "\n" + fn + r"""
const out={};
_renderTagStrip(); out.single=el.innerHTML;
MULTI_SEAT=true; _renderTagStrip(); out.multi=el.innerHTML; MULTI_SEAT=false;
fpOpen=true; _renderTagStrip(); out.fp=el.innerHTML; fpOpen=false;
tagFilter='risk:medium'; _renderTagStrip(); out.active=el.innerHTML; tagFilter='';
console.log(JSON.stringify(out));
"""
    p = tmp_path / "strip.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run([_node(), str(p)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # single：只剩 VIP + 齿轮，系统族一枚不出，原码不出
    assert "dormant" not in out["single"] and "risk:" not in out["single"] and "停联" not in out["single"]
    assert 'data-ptag="VIP"' in out["single"]
    # multi：系统族出、人话、可点、不晒原码（risk:medium 沿用 Q-17 既有「风险·中」，其余 risk:* → 需留意）
    for label in ("长期未回", "需留意", "别再联系", "风险·中"):
        assert label in out["multi"], label
    assert "dormant:ignored</" not in out["multi"] and 'data-ptag="dormant:ignored"' in out["multi"]
    assert "risk:high</" not in out["multi"] and "risk:medium</" not in out["multi"]
    assert 'data-systag="1"' in out["multi"] and 'title="d"' in out["multi"]
    # 面板显式打开「系统标签」→ single 也出
    assert "长期未回" in out["fp"]
    # single 下正按某系统标签筛 → 该枚强制出且带 ×
    assert 'data-ptag="risk:medium"' in out["active"] and "data-ptag-x" in out["active"]
    assert "长期未回" in out["active"]


def test_q39_i18n_keys_trilingual():
    from src.web.i18n_packs import send_truth_q39 as pk
    assert set(pk.ZH) == set(pk.EN) == set(pk.ZH_HANT)
    for k in ("inbox.cs.xlate_hold", "inbox.cs.xlate_hold_t", "inbox.cs.xlate_hold.other",
              "inbox.cs.act.retranslate", "inbox.cs.act.retranslate_t",
              "inbox.systag.dormant", "inbox.systag.risk", "inbox.systag.stop_contact",
              "err.inbox.retranslate_not_on_hold", "err.inbox.retranslate_worker_missing"):
        assert k in pk.ZH, k
    assert "翻译引擎没回话" in pk.ZH["inbox.cs.xlate_hold"] and "这条没发" in pk.ZH["inbox.cs.xlate_hold"]
    assert pk.ZH["inbox.cs.act.retranslate"] == "重试翻译"
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    for k in pk.ZH:
        assert k in zh, k
    html = _TPL.read_text(encoding="utf-8")
    assert "retranslate:'inbox.cs.act.retranslate'" in html
    assert "/api/unified-inbox/drafts/'+enc(_csDid)+'/retranslate'" in html


# ── 红线复核 ──────────────────────────────────────────────────────────────────

def test_redlines_q6_pick_media_and_offer_media_untouched():
    import inspect
    from src.companion import persona_media as pm
    from src.inbox import commitment_guard as cg
    sig = inspect.signature(pm.pick_media)
    for p in ("generic_ok", "required_scene_kind", "allow_random", "trace", "no_resend"):
        assert p in sig.parameters
    assert "Q-39" not in inspect.getsource(pm)
    assert "Q-39" not in inspect.getsource(cg.detect_offer_media)
    from src.inbox import image_autosend as ia
    assert "Q-39" not in inspect.getsource(ia.note_album_miss)
    from src.inbox import prompt_addenda as pa
    assert "Q-39" not in inspect.getsource(pa)
