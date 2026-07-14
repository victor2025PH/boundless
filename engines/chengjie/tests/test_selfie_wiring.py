"""Stage A：skill_manager 形象照请求接线（轻量绑定，免全量 init）。

校验：未开→None、非请求→None、关系浅→搪塞、未解锁→付费引导、准入+provider 关→文字兜底。
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from src.ai.companion_selfie import SELFIE_FEATURE, reset_selfie_provider
from src.utils.companion_funnel_store import (
    get_companion_funnel_store,
    reset_companion_funnel_store,
)
from src.utils.selfie_cap import reset_selfie_cap_tracker

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _selfie_cfg = _SMcls._selfie_cfg
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled
    _handle_persona_media_request = _SMcls._handle_persona_media_request
    _handle_selfie_request = _SMcls._handle_selfie_request
    _handle_contextual_image_request = _SMcls._handle_contextual_image_request
    _record_selfie_event = _SMcls._record_selfie_event
    _get_selfie_cap = _SMcls._get_selfie_cap
    _selfie_upsell_text = _SMcls._selfie_upsell_text
    _try_send_selfie_media = _SMcls._try_send_selfie_media
    _selfie_persona_for_prompt = _SMcls._selfie_persona_for_prompt
    _selfie_album_key = _SMcls._selfie_album_key
    _get_persona_name_for_context = _SMcls._get_persona_name_for_context
    _bond_level_from_context = _SMcls._bond_level_from_context
    _effective_intimacy = _SMcls._effective_intimacy
    _story_bonus_cap = _SMcls._story_bonus_cap
    _stage_lang = _SMcls._stage_lang
    _record_stage_turn = _SMcls._record_stage_turn
    _promise_guard_cfg = _SMcls._promise_guard_cfg
    _apply_media_promise_guard = _SMcls._apply_media_promise_guard
    _selfie_offer_accept_bridge = _SMcls._selfie_offer_accept_bridge

    def __init__(self, *, selfie_cfg=None, gate=False):
        comp = {}
        if selfie_cfg is not None:
            comp["selfie"] = selfie_cfg
        mon = {"enabled": True, "gate": {"enabled": True}} if gate else {}
        self.config = SimpleNamespace(config={"companion": comp, "monetization": mon})
        self.logger = logging.getLogger("test_selfie")
        self.ai_client = None  # _stage_lang 走 reply_lang 回落


_ON = {"enabled": True, "free_daily": 1, "min_bond_level": 2,
       "provider": {"enabled": False}}


@pytest.fixture()
def media_store():
    """隔离的内存版 persona_media store（绝不写 config/persona_media.db）。"""
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    st = configure_persona_media_store(":memory:")
    yield st
    reset_persona_media_store()


@pytest.mark.asyncio
async def test_disabled_returns_none():
    sm = _SM(selfie_cfg={"enabled": False})
    out = await sm._handle_selfie_request("给我看看你", "u1", {"intimacy_score": 60}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_not_a_request_returns_none():
    sm = _SM(selfie_cfg=_ON)
    out = await sm._handle_selfie_request("今天天气不错", "u1", {"intimacy_score": 60}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_too_soon_when_bond_low():
    sm = _SM(selfie_cfg=_ON)
    out = await sm._handle_selfie_request(
        "发张自拍", "u1", {"intimacy_score": 3}, "c1")
    assert out is not None
    assert "亲近" in out or "聊聊" in out


@pytest.mark.asyncio
async def test_locked_gives_upsell_when_gate_on_no_album():
    sm = _SM(selfie_cfg=dict(_ON, free_daily=0), gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("想看你的照片", "u1", ctx, "c1")
    assert out is not None
    assert "专属相册" in out or "解锁" in out


@pytest.mark.asyncio
async def test_allow_free_quota_provider_disabled_fallback_and_count():
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("发张照片", "u1", ctx, "c1")
    assert out is not None
    assert "不太方便" in out or "陪你" in out  # provider 关 → 文字兜底
    # P0 语义修正：客户没收到图 → 不消耗免费额度（额度只在真送达时扣，
    # 否则"要图失败还扣次数"→ 下次直接被推付费引导，既不公平又伤转化）
    assert ctx.get("_selfie_used", 0) == 0


@pytest.mark.asyncio
async def test_allow_owns_album_unlimited_no_count():
    reset_selfie_provider()
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60,
           "entitlement": {"grants": [], "unlocked": [SELFIE_FEATURE]}}
    out = await sm._handle_selfie_request("show me your face", "u1", ctx, "c1")
    assert out is not None
    assert ctx.get("_selfie_used", 0) == 0  # 拥有相册 → 不消耗免费额度


@pytest.mark.asyncio
async def test_gate_off_allows_without_album():
    reset_selfie_provider()
    sm = _SM(selfie_cfg=_ON, gate=False)  # 变现 gate 关 → 不计费、不引导
    ctx = {"intimacy_score": 60, "entitlement": None}
    out = await sm._handle_selfie_request("来张照片", "u1", ctx, "c1")
    assert out is not None
    assert "专属相册" not in out  # gate 关不应出现付费引导


# ── Stage B：埋点接线（准入态 → 自拍漏斗） ────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_records_locked_event():
    reset_companion_funnel_store()
    funnel = get_companion_funnel_store(":memory:")
    try:
        sm = _SM(selfie_cfg=dict(_ON, free_daily=0), gate=True)
        ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
        await sm._handle_selfie_request("想看你的照片", "u_lk", ctx, "c1")
        rows = funnel.selfie_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["contact_key"] == "u_lk"
        assert rows[0]["kind"] == "locked"
    finally:
        reset_companion_funnel_store()


@pytest.mark.asyncio
async def test_funnel_records_delivered_and_too_soon():
    reset_companion_funnel_store()
    funnel = get_companion_funnel_store(":memory:")
    reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_ON, gate=True)
        await sm._handle_selfie_request(
            "发张照片", "u_dl", {"intimacy_score": 60,
                              "entitlement": {"grants": [], "unlocked": []}}, "c1")
        await sm._handle_selfie_request(
            "发张自拍", "u_ts", {"intimacy_score": 3}, "c1")
        kinds = {r["contact_key"]: r["kind"] for r in funnel.selfie_recent(limit=10)}
        assert kinds["u_dl"] == "delivered"
        assert kinds["u_ts"] == "too_soon"
    finally:
        reset_companion_funnel_store()


@pytest.mark.asyncio
async def test_funnel_noop_when_store_not_initialized():
    reset_companion_funnel_store()  # 无单例 → peek 返回 None → 不记录、不报错
    sm = _SM(selfie_cfg=_ON, gate=True)
    ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
    out = await sm._handle_selfie_request("想看你的照片", "u1", ctx, "c1")
    assert out is not None  # 主流程不受埋点缺失影响


# ── Stage D：A 线主客户端 send_photo 直发兜底 ───────────────────────────────

@pytest.mark.asyncio
async def test_try_send_selfie_media_direct_callback():
    sm = _SM(selfie_cfg=_ON)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["args"] = (chat_id, path, caption)
        return True

    # 无 platform/account → 编排器路跳过 → 走 A 线直发回调
    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _fake_send}, 12345, "/tmp/x.png", "hi")
    assert ok is True
    assert sent["args"] == (12345, "/tmp/x.png", "hi")


@pytest.mark.asyncio
async def test_try_send_selfie_media_no_channel_returns_false():
    sm = _SM(selfie_cfg=_ON)
    ok = await sm._try_send_selfie_media({}, 1, "/tmp/x.png", "hi")
    assert ok is False


@pytest.mark.asyncio
async def test_try_send_selfie_media_no_image_returns_false():
    sm = _SM(selfie_cfg=_ON)

    async def _fake_send(c, p, cap):
        return True

    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _fake_send}, 1, "", "hi")
    assert ok is False


@pytest.mark.asyncio
async def test_try_send_selfie_media_callback_failure_soft_false():
    sm = _SM(selfie_cfg=_ON)

    async def _boom(c, p, cap):
        raise RuntimeError("net down")

    ok = await sm._try_send_selfie_media(
        {"_send_photo_to_chat": _boom}, 1, "/tmp/x.png", "hi")
    assert ok is False  # 直发失败软兜底，不抛


@pytest.mark.asyncio
async def test_sender_send_photo_success_failure_and_no_client():
    from src.client.sender import TelegramSenderMixin

    class _Cli:
        def __init__(self, fail=False):
            self.fail = fail
            self.calls = []

        async def send_photo(self, chat_id, photo, caption=""):
            if self.fail:
                raise RuntimeError("rpc")
            self.calls.append((chat_id, photo, caption))

    class _S(TelegramSenderMixin):
        def __init__(self, cli):
            self.client = cli
            self.logger = logging.getLogger("test_sender")
            self.account_id = "a"

    ok = _S(_Cli())
    assert await ok.send_photo(7, "/p.png", "cap") is True
    assert ok.client.calls == [(7, "/p.png", "cap")]
    assert await _S(_Cli(fail=True)).send_photo(7, "/p.png", "cap") is False
    assert await _S(None).send_photo(7, "/p.png") is False
    assert await _S(_Cli()).send_photo(7, "") is False  # 空路径不发


# ── Stage G：send_photo 纳入统一发送护栏/节流/记账（图不绕过风控） ──────────

class _PhotoCli:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_photo(self, chat_id, photo, caption=""):
        if self.fail:
            raise RuntimeError("rpc")
        self.calls.append((chat_id, photo, caption))


def _photo_sender(cli, *, min_interval=0, last_send=0.0):
    from src.client.sender import TelegramSenderMixin

    class _Cfg:
        def get(self, k, d=None):
            if k == "reply":
                return {"split_send": {"min_interval_seconds": min_interval}}
            return d if d is not None else {}

    class _S(TelegramSenderMixin):
        def __init__(self):
            self.client = cli
            self.logger = logging.getLogger("test_sender_g")
            self.account_id = "a"
            self.config = _Cfg()
            self._last_send_wallclock = last_send

    s = _S()
    s._shared_send_limiter = lambda cfg: None  # 不触 DB/限流器副作用
    return s


@pytest.mark.asyncio
async def test_send_photo_blocked_by_presend_guard(monkeypatch):
    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: True)  # 冻结/被闸门拦
    assert await s.send_photo(7, "/p.png", "c") is False
    assert s.client.calls == []  # 护栏拦下，照片未真发（不绕过风控）


@pytest.mark.asyncio
async def test_send_photo_paces_against_shared_wallclock(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _photo_sender(_PhotoCli(), min_interval=5, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    ok = await s.send_photo(7, "/p.png", "c")
    assert ok is True
    assert slept.get("sec") is not None and slept["sec"] > 0  # 距上次<5s→补足节流
    assert s.client.calls == [(7, "/p.png", "c")]
    assert s._last_send_wallclock > 0  # 记账刷新墙钟（下次文本据此排队）


@pytest.mark.asyncio
async def test_send_photo_no_pace_when_interval_zero(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _photo_sender(_PhotoCli(), min_interval=0, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_photo(7, "/p.png", "c") is True
    assert "sec" not in slept  # min_interval=0 → 不节流（行为不变）


# ── Stage H：富媒体外发的出站镜像 + contacts 记账（坐席台/亲密度看见图） ──────

@pytest.mark.asyncio
async def test_send_photo_mirrors_and_records(monkeypatch):
    emitted = {}
    recorded = {}

    def _rec(acc, chat, direction, **kw):
        recorded.update({"acc": acc, "chat": chat, "dir": direction,
                         "prev": kw.get("text_preview", "")})

    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message", _rec)

    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    s._emit_inbox = lambda **kw: emitted.update(kw)

    assert await s.send_photo(7, "/p.png", "看我新裙子") is True
    # 坐席台镜像：带 [图片] 前缀 + 配文，方向 out；msg_id 供回显去重（mock 客户端无 id→空串）
    assert emitted["chat_id"] == 7
    assert emitted["text"] == "[图片] 看我新裙子"
    assert emitted["direction"] == "out"
    assert emitted.get("msg_id") == ""
    # contacts 记账：外发互动计入 IntimacyEngine（mutuality）
    assert recorded["dir"] == "out" and recorded["prev"] == "[图片] 看我新裙子"
    assert recorded["chat"] == 7 and recorded["acc"] == "a"


@pytest.mark.asyncio
async def test_send_photo_empty_caption_preview(monkeypatch):
    emitted = {}
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message", lambda *a, **k: None)
    s = _photo_sender(_PhotoCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    s._emit_inbox = lambda **kw: emitted.update(kw)
    assert await s.send_photo(7, "/p.png", "") is True
    assert emitted["text"] == "[图片]"  # 无配文 → 仅标记


@pytest.mark.asyncio
async def test_postsend_mirror_record_no_emit_attr_still_records(monkeypatch):
    recorded = {}
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "record_relationship_message",
                        lambda *a, **k: recorded.update({"hit": True}))
    s = _photo_sender(_PhotoCli())  # 无 _emit_inbox 属性
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    assert await s.send_photo(7, "/p.png", "hi") is True  # 镜像缺省→优雅跳过、不抛
    assert recorded.get("hit") is True  # contacts 记账照常


# ── Stage F：全局每日出图预算 cap（护出图 API 账单） ──────────────────────

@pytest.mark.asyncio
async def test_global_cap_blocks_second_and_preserves_quota(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    reset_companion_funnel_store()
    reset_selfie_cap_tracker()  # 单例：清前序测试残留计数，保证从 0 起
    funnel = get_companion_funnel_store(":memory:")
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    try:
        sm = _SM(selfie_cfg=dict(_ON, free_daily=5, daily_global_cap=1,
                                 provider={"enabled": True, "backend": "openai"}),
                 gate=True)
        ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
        out1 = await sm._handle_selfie_request("发张照片", "u1", ctx, 1)
        assert out1 is not None and out1 != ""        # 第1次出图成功但无通道→自洽搪塞
        # P0 语义修正：没真送达 → 不扣免费额度（全局 cap 已在生成时消耗，用户额度不动）
        assert ctx.get("_selfie_used", 0) == 0
        out2 = await sm._handle_selfie_request("再发张照片", "u1", ctx, 1)
        assert "明天" in out2                          # 第2次全局额度用尽→capped 兜底
        assert ctx.get("_selfie_used", 0) == 0         # 未再消耗用户免费额度
        kinds = [r["kind"] for r in funnel.selfie_recent(limit=10)]
        assert "capped" in kinds and kinds.count("delivered") == 1
    finally:
        cs.reset_selfie_provider()
        reset_companion_funnel_store()
        reset_selfie_cap_tracker()


@pytest.mark.asyncio
async def test_global_cap_zero_means_unlimited(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png")

    monkeypatch.setattr(prov, "generate", _gen)
    try:
        sm = _SM(selfie_cfg=dict(_ON, daily_global_cap=0,
                                 provider={"enabled": True, "backend": "openai"}),
                 gate=False)
        for _ in range(3):
            out = await sm._handle_selfie_request(
                "发张照片", "u1", {"intimacy_score": 60, "entitlement": None}, 1)
            assert "明天" not in out  # cap=0 → 永不拦
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_global_cap_ignored_when_provider_disabled():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()  # provider disabled → 无出图成本 → 不计 cap
    try:
        sm = _SM(selfie_cfg=dict(_ON, daily_global_cap=1), gate=False)
        for _ in range(3):
            out = await sm._handle_selfie_request(
                "发张照片", "u1", {"intimacy_score": 60, "entitlement": None}, 1)
            assert "明天" not in out  # 恒文字兜底，cap 不介入
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_allow_direct_send_returns_empty_when_photo_sent(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "disabled"})

    async def _fake_gen(prompt, **kw):
        return cs.SelfieResult(ok=True, image_path="/tmp/fake.png", provider="x")

    monkeypatch.setattr(prov, "generate", _fake_gen)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["path"] = path
        return True

    try:
        sm = _SM(selfie_cfg=_ON, gate=False)  # 准入不限
        ctx = {"intimacy_score": 60, "entitlement": None,
               "_send_photo_to_chat": _fake_send}
        out = await sm._handle_selfie_request("发张照片", "u1", ctx, 999)
        assert out == ""  # 媒体已发出 → 空串(不再补普通文字回复)
        assert sent["path"] == "/tmp/fake.png"
    finally:
        cs.reset_selfie_provider()


# ── Stage 0：人设注册相册（DB 预制图/视频，按触发词命中即发） ─────────────────

@pytest.mark.asyncio
async def test_persona_media_disabled_returns_none(media_store):
    media_store.add("lin", "photo", "/d/1.jpg", "/static/1.jpg", triggers=["跳舞"])
    sm = _SM(selfie_cfg={"enabled": False})
    ctx = {"account_persona_id": "lin", "_send_photo_to_chat": None}
    out = await sm._handle_persona_media_request("给我跳舞", "u1", ctx, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_persona_media_keyword_hit_sends_photo(media_store):
    row = media_store.add("lin", "photo", "/d/dance.jpg", "/static/dance.jpg",
                          triggers=["跳舞"], caption="看我跳~")
    sent = {}

    async def _send(chat_id, path, caption):
        sent.update(chat=chat_id, path=path, cap=caption)
        return True

    sm = _SM(selfie_cfg=_ON)
    ctx = {"account_persona_id": "lin", "intimacy_score": 60,
           "_send_photo_to_chat": _send}
    out = await sm._handle_persona_media_request("给我跳舞看看", "u1", ctx, 999)
    assert out == ""  # 已发出 → 短路
    assert sent["path"] == "/d/dance.jpg" and sent["cap"] == "看我跳~"
    assert ctx.get("_persona_media_last") == row["id"]
    assert media_store.get(row["id"])["hits"] == 1  # 命中计数


@pytest.mark.asyncio
async def test_persona_media_no_match_returns_none(media_store):
    media_store.add("lin", "photo", "/d/1.jpg", "/static/1.jpg", triggers=["跳舞"])
    sm = _SM(selfie_cfg=_ON)
    ctx = {"account_persona_id": "lin", "intimacy_score": 60,
           "_send_photo_to_chat": (lambda *a: True)}
    # 非要图闲聊 + 无关键词命中 → None（交后续）
    out = await sm._handle_persona_media_request("今天心情不错", "u1", ctx, 1)
    assert out is None


@pytest.mark.asyncio
async def test_persona_media_generic_pool_on_selfie_request(media_store):
    media_store.add("lin", "photo", "/d/p.jpg", "/static/p.jpg")  # 无触发词=通用池
    sent = {}

    async def _send(chat_id, path, caption):
        sent["path"] = path
        return True

    sm = _SM(selfie_cfg=_ON)
    ctx = {"account_persona_id": "lin", "intimacy_score": 60,
           "_send_photo_to_chat": _send}
    out = await sm._handle_persona_media_request("發個照片給我看看嘛", "u1", ctx, 1)
    assert out == "" and sent["path"] == "/d/p.jpg"


@pytest.mark.asyncio
async def test_persona_media_video_needs_video_callback(media_store):
    media_store.add("lin", "video", "/d/v.mp4", "/static/v.mp4", triggers=["跳舞"])
    sm = _SM(selfie_cfg=_ON)
    # 仅有照片回调 → 视频发不了 → None（不误当照片发，交回落）
    ctx = {"account_persona_id": "lin", "_send_photo_to_chat": (lambda *a: True)}
    out = await sm._handle_persona_media_request("给我跳舞视频", "u1", ctx, 1)
    assert out is None
    # 注入视频回调 → 发出 → 短路
    vsent = {}

    async def _vsend(chat_id, path, caption):
        vsent["path"] = path
        return True

    ctx2 = {"account_persona_id": "lin", "_send_video_to_chat": _vsend}
    out2 = await sm._handle_persona_media_request("给我跳舞视频", "u1", ctx2, 1)
    assert out2 == "" and vsent["path"] == "/d/v.mp4"


@pytest.mark.asyncio
async def test_persona_media_bond_gate(media_store):
    media_store.add("lin", "photo", "/d/1.jpg", "/static/1.jpg",
                    triggers=["跳舞"], min_bond_level=5)
    sm = _SM(selfie_cfg=_ON)
    # 关系浅（bond<5）→ 条目被闸门挡 → None
    ctx = {"account_persona_id": "lin", "intimacy_score": 1,
           "_send_photo_to_chat": (lambda *a: True)}
    out = await sm._handle_persona_media_request("给我跳舞", "u1", ctx, 1)
    assert out is None


# ── Stage B：对话上下文「按需生图」接线（"你煮的面拍张照给我看"） ────────────

_CTX_ON = {"enabled": True, "contextual_images": True, "min_bond_level": 0,
           "provider": {"enabled": True, "backend": "openai"}}


@pytest.mark.asyncio
async def test_ctx_image_disabled_returns_none():
    sm = _SM(selfie_cfg={"enabled": True})  # contextual_images 缺省关
    out = await sm._handle_contextual_image_request(
        "你煮的面拍张照给我看", "u1",
        {"intimacy_score": 60, "_conversation_history": []}, "c1")
    assert out is None


@pytest.mark.asyncio
async def test_ctx_image_not_a_request_returns_none():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_CTX_ON)
        out = await sm._handle_contextual_image_request(
            "今天天气真好", "u1", {"intimacy_score": 60}, "c1")
        assert out is None
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_ctx_image_album_backend_defers_to_text():
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        # album 后端无法凭空生成"你煮的面" → 交普通回复(None)，不硬答
        sm = _SM(selfie_cfg=dict(_CTX_ON,
                                 provider={"enabled": True, "backend": "album"}))
        out = await sm._handle_contextual_image_request(
            "你煮的面拍张照给我看", "u1",
            {"intimacy_score": 60, "_conversation_history": []}, "c1")
        assert out is None
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_ctx_image_generates_from_context_and_sends(monkeypatch):
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(prompt, **kw):
        assert "noodles" in prompt  # 从上下文"我刚煮了面"抽出的主体进了 prompt
        assert not kw.get("base_image")  # 物体图 text2img，不带人设的脸
        return cs.SelfieResult(ok=True, image_path="/tmp/noodles.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    sent = {}

    async def _fake_send(chat_id, path, caption):
        sent["args"] = (chat_id, path, caption)
        return True

    try:
        sm = _SM(selfie_cfg=_CTX_ON)
        ctx = {"intimacy_score": 60,
               "_conversation_history": [{"role": "assistant", "content": "我刚煮了面"}],
               "_send_photo_to_chat": _fake_send}
        out = await sm._handle_contextual_image_request(
            "你煮的拍张照给我看嗎", "u1", ctx, 12345)
        assert out == ""  # 图已发出 → 空串
        assert sent["args"][1] == "/tmp/noodles.png"
        # 媒体轮回写：短路成功后预置 _stage_media_note（供 _record_stage_turn）
        assert ctx.get("_stage_media_note", "").startswith("[图片]")
    finally:
        cs.reset_selfie_provider()


# ── P0：谎言修复 + 媒体轮回写 + offer 桥 + 文案语言对齐 ──────────────────────

@pytest.mark.asyncio
async def test_selfie_send_failure_never_claims_photo_sent(monkeypatch):
    """实锤修复：出图成功但发送失败 → 绝不能把「这是刚拍的，给你看～」当文字发出
    （图根本没到客户手里）。改发自洽的"这轮拍不了"搪塞。"""
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    try:
        sm = _SM(selfie_cfg=dict(_ON, provider={"enabled": True, "backend": "openai"}),
                 gate=False)
        # 无任何发送通道（无编排器 platform/account、无 _send_photo_to_chat 回调）
        ctx = {"intimacy_score": 60, "entitlement": None}
        out = await sm._handle_selfie_request("发张照片", "u1", ctx, 1)
        assert out is not None and out != ""
        assert "刚拍的" not in out and "给你看" not in out  # 不再谎称图已发出
        assert "不太方便" in out or "陪你" in out           # 自洽搪塞
        assert ctx.get("_selfie_used", 0) == 0  # 没送达不消耗免费额度
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_selfie_provider_fail_does_not_consume_quota():
    """provider 未配/失败 → 客户没收到图 → 不消耗免费额度（此前会白扣）。"""
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_ON, gate=True)
        ctx = {"intimacy_score": 60, "entitlement": {"grants": [], "unlocked": []}}
        out = await sm._handle_selfie_request("发张照片", "u1", ctx, "c1")
        assert out is not None
        assert ctx.get("_selfie_used", 0) == 0  # provider 关 → 文字兜底不扣额度
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_selfie_sent_records_media_note_for_history(monkeypatch):
    """媒体轮回写：图真发出后预置 _stage_media_note="[图片] 配文"，
    经 _record_stage_turn 进 last_reply → 下一轮 LLM 知道自己刚发过图。"""
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "disabled"})

    async def _gen(p, **k):
        return cs.SelfieResult(ok=True, image_path="/tmp/ok.png", provider="x")

    monkeypatch.setattr(prov, "generate", _gen)

    async def _send(chat_id, path, caption):
        return True

    try:
        sm = _SM(selfie_cfg=_ON, gate=False)
        ctx = {"intimacy_score": 60, "entitlement": None,
               "_send_photo_to_chat": _send}
        out = await sm._handle_selfie_request("发张照片", "u1", ctx, 1)
        assert out == ""
        note = ctx.get("_stage_media_note", "")
        assert note.startswith("[图片]")
        # 中央块回写：last_reply/last_message 落媒体轮（下一轮进历史窗口）
        sm._record_stage_turn(ctx, "发张照片", out)
        assert ctx.get("last_reply", "").startswith("[图片]")
        assert ctx.get("last_message") == "发张照片"
        assert "_stage_media_note" not in ctx  # 已消费
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_record_stage_turn_with_deflection_text():
    sm = _SM(selfie_cfg=_ON)
    ctx = {}
    sm._record_stage_turn(ctx, "发张自拍", "哎呀，我们才刚开始熟悉呢～")
    assert ctx["last_reply"].startswith("哎呀")
    assert ctx["reply_count"] == 1


@pytest.mark.asyncio
async def test_selfie_offer_accept_bridge_triggers_stage(monkeypatch):
    """offer-接受桥（A 线）：上一轮 AI 提议「要不要我拍一张给你看」、本条只回
    「好呀」→ 视同自拍请求进入 Stage A（此处 provider 关 → 走文字兜底证明已进入）。"""
    from src.ai import companion_selfie as cs
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_ON, gate=False)
        ctx = {"intimacy_score": 60, "entitlement": None,
               "last_reply": "嘿嘿～要不要我拍一张给你看呀？"}
        out = await sm._handle_selfie_request("好呀", "u1", ctx, 1)
        assert out is not None  # 桥命中 → 进入 Stage A（非 None）
        # 无 offer 前文 → 「好呀」不触发（防误伤）
        ctx2 = {"intimacy_score": 60, "entitlement": None,
                "last_reply": "今天好热呀"}
        out2 = await sm._handle_selfie_request("好呀", "u1", ctx2, 1)
        assert out2 is None
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_stage_texts_follow_conversation_language():
    """英文会话的搪塞/兜底不再蹦中文（reply_lang=en → en 模板）。"""
    sm = _SM(selfie_cfg=_ON, gate=False)
    # too_soon（关系浅）英文
    ctx_en = {"intimacy_score": 3, "reply_lang": "en"}
    out = await sm._handle_selfie_request("send me a selfie", "u1", ctx_en, 1)
    assert out is not None
    assert not any("\u4e00" <= c <= "\u9fff" for c in out), out
    # 中文会话仍中文
    ctx_zh = {"intimacy_score": 3, "reply_lang": "zh"}
    out_zh = await sm._handle_selfie_request("发张自拍", "u1", ctx_zh, 1)
    assert any("\u4e00" <= c <= "\u9fff" for c in out_zh)


@pytest.mark.asyncio
async def test_apply_media_promise_guard_strips_and_deflects():
    """A 线出站守卫：本轮无媒体真发，回复承诺「等我拍」→ 句级剥离；
    整句剥空 → 语言对齐兜底话术；正常回复零改动。"""
    sm = _SM(selfie_cfg=_ON)
    ctx = {}
    # 混合句：剥承诺留其余
    out = sm._apply_media_promise_guard("今天好开心！等我拍一张给你～", ctx)
    assert "拍一张" not in out and "开心" in out
    # 纯承诺：剥空 → 兜底话术（中文）
    out2 = sm._apply_media_promise_guard("等我拍一张给你哈～", ctx)
    assert out2.strip() and "拍一张" not in out2
    # 正常回复不动
    normal = "宝贝想我了没？我刚下班～"
    assert sm._apply_media_promise_guard(normal, ctx) == normal
    # 关守卫 → 原样放行
    sm2 = _SM(selfie_cfg=_ON)
    sm2.config.config["companion"]["media_promise_guard"] = {"enabled": False}
    assert sm2._apply_media_promise_guard(
        "等我拍一张给你哈～", ctx) == "等我拍一张给你哈～"
