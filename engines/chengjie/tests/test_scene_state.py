"""Phase18「场景状态 + 已发媒体日志 + 异步兑现」门禁。

三件事的回归网：
① 场景单一事实源（``resolve_current_scene``/``scene_chat_note``）——聊天文本与
  生图共用同一个「AI 此刻在哪」，从源头消灭"说上班发海边图"打脸；
② 已发媒体日志（``_record_media_sent``/``_media_sent_log``）——"上次那张"可答、
  "我没发过照片"式失忆抵赖不再发生；客户显式点名场景/「跟上次一样」进 directive；
③ A 线异步兑现（``media_promise_guard.async_fulfill``）——承诺发图且预检全过
  → 保留承诺 + 后台真拍真发；失败自动补台阶文本，闭环诚实。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.ai import companion_selfie as cs

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _Store:
    def mark_dirty(self, *a, **k):
        pass

    def flush(self, *a, **k):
        pass


class _SM:
    _selfie_cfg = _SMcls._selfie_cfg
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled
    _promise_guard_cfg = _SMcls._promise_guard_cfg
    _apply_media_promise_guard = _SMcls._apply_media_promise_guard
    _async_fulfill_precheck = _SMcls._async_fulfill_precheck
    _spawn_promise_fulfill_task = _SMcls._spawn_promise_fulfill_task
    _fulfill_promised_selfie_async = _SMcls._fulfill_promised_selfie_async
    _record_media_sent = _SMcls._record_media_sent
    _last_sent_media_scene = _SMcls._last_sent_media_scene
    _inject_scene_state = _SMcls._inject_scene_state
    _record_stage_turn = _SMcls._record_stage_turn
    _MEDIA_SENT_LOG_MAX = _SMcls._MEDIA_SENT_LOG_MAX
    _selfie_persona_for_prompt = _SMcls._selfie_persona_for_prompt
    _get_persona_name_for_context = _SMcls._get_persona_name_for_context
    _bond_level_from_context = _SMcls._bond_level_from_context
    _effective_intimacy = _SMcls._effective_intimacy
    _story_bonus_cap = _SMcls._story_bonus_cap
    _get_selfie_cap = _SMcls._get_selfie_cap
    _record_selfie_event = _SMcls._record_selfie_event
    _selfie_album_key = _SMcls._selfie_album_key

    def __init__(self, *, selfie_cfg=None, guard_cfg=None):
        comp = {}
        if selfie_cfg is not None:
            comp["selfie"] = selfie_cfg
        if guard_cfg is not None:
            comp["media_promise_guard"] = guard_cfg
        self.config = SimpleNamespace(config={"companion": comp})
        self.logger = logging.getLogger("test_scene_state")
        self.ai_client = None
        self._context_store = _Store()


# ── ① 场景单一事实源 ─────────────────────────────────────────────────────────

def test_resolve_current_scene_uses_persona_pool_then_config():
    persona = {"selfie_scenes": ["in the dorm", "at the library"]}
    scfg = {"scene_rotation": ["at the office"], "scene_hint": "fallback"}
    out = cs.resolve_current_scene(persona, scfg)
    assert out in ("in the dorm", "at the library")
    # persona 无池 → config rotation
    out2 = cs.resolve_current_scene({}, scfg)
    assert out2 == "at the office"
    # 全无 → default scene_hint
    assert cs.resolve_current_scene({}, {"scene_hint": "cozy room"}) == "cozy room"
    assert cs.resolve_current_scene({}, {}) == ""


def test_resolve_current_scene_stable_within_time_bucket():
    import datetime
    persona = {"selfie_scenes": ["a", "b", "c", "d", "e"]}
    t1 = datetime.datetime(2026, 7, 14, 13, 0)
    t2 = datetime.datetime(2026, 7, 14, 16, 59)  # 同一时段桶（11-17）
    assert cs.resolve_current_scene(persona, {}, now=t1) == \
        cs.resolve_current_scene(persona, {}, now=t2)  # 同桶恒定，十分钟内不"瞬移"


def test_scene_chat_note_renders_and_empty():
    note = cs.scene_chat_note("in a cozy cafe")
    assert "你此刻的状态" in note and "cozy cafe" in note
    assert "不要每条都汇报" in note  # 防播报措辞
    assert cs.scene_chat_note("") == ""


@pytest.mark.parametrize("text,expect_sub", [
    ("发张你在海边的照片", "beach"),
    ("想看你在办公室的样子，拍一张", "office"),
    ("send me a pic of you at the gym", "gym"),
    ("给我拍张自拍", ""),          # 未点名 → 空（走轮换）
    ("今天天气不错", ""),
])
def test_extract_requested_scene(text, expect_sub):
    out = cs.extract_requested_scene(text)
    if expect_sub:
        assert expect_sub in out
    else:
        assert out == ""


def test_wants_same_scene():
    assert cs.wants_same_scene("再拍一张跟上次一样的") is True
    assert cs.wants_same_scene("same as last time please") is True
    assert cs.wants_same_scene("再来一张") is False


# ── ② 已发媒体日志 + 场景注入 ────────────────────────────────────────────────

def test_record_media_sent_bounded_and_last_scene():
    sm = _SM(selfie_cfg={"enabled": True})
    ctx = {}
    for i in range(8):
        sm._record_media_sent(ctx, note=f"[图片] n{i}",
                              scene=(f"scene{i}" if i % 2 == 0 else ""))
    log = ctx["_media_sent_log"]
    assert len(log) == sm._MEDIA_SENT_LOG_MAX  # bounded=5
    assert log[-1]["note"] == "[图片] n7"
    assert sm._last_sent_media_scene(ctx) == "scene6"  # 最近一条**带场景**的


def test_record_stage_turn_feeds_media_log():
    sm = _SM(selfie_cfg={"enabled": True})
    ctx = {"_stage_media_note": "[图片] 刚拍的",
           "_stage_media_scene": "at the beach"}
    sm._record_stage_turn(ctx, "发张自拍", "")
    assert ctx["last_reply"].startswith("[图片]")
    assert ctx["_media_sent_log"][-1]["scene"] == "at the beach"
    assert "_stage_media_scene" not in ctx  # 已消费


def test_inject_scene_state_gating_and_content():
    scfg = {"enabled": True, "scene_rotation": ["in a cozy cafe"]}
    sm = _SM(selfie_cfg=scfg)
    ctx = {}
    sm._inject_scene_state(ctx)
    assert "cozy cafe" in ctx.get("_current_scene_note", "")
    # 已发媒体日志 → 一并出「最近发过的照片」块
    sm._record_media_sent(ctx, note="[图片] 看我~", scene="in a cozy cafe")
    sm._inject_scene_state(ctx)
    assert "你最近发过的照片" in ctx.get("_media_sent_note", "")
    assert "看我~" in ctx["_media_sent_note"]
    # scene_in_chat=false → 不注场景（日志块也不注：同一开关）
    sm2 = _SM(selfie_cfg=dict(scfg, scene_in_chat=False))
    ctx2 = {}
    sm2._inject_scene_state(ctx2)
    assert "_current_scene_note" not in ctx2
    # selfie 关 → 全不注
    sm3 = _SM(selfie_cfg={"enabled": False})
    ctx3 = {}
    sm3._inject_scene_state(ctx3)
    assert "_current_scene_note" not in ctx3


# ── ②b 显式场景进生图链（A 线 Stage A / B 线 plan+caption）──────────────────

def test_plan_autosend_image_carries_requested_scene():
    from src.inbox import image_autosend as ia
    d = ia.plan_autosend_image("拍一张你在海边的照片嘛", [], {"enabled": True})
    assert d and d["kind"] == "selfie" and "beach" in d.get("scene", "")
    d2 = ia.plan_autosend_image("發個照片給我看看嘛", [], {"enabled": True})
    assert d2 and d2.get("scene", "") == ""  # 未点名 → 空（走轮换）


@pytest.mark.asyncio
async def test_stage_a_requested_scene_reaches_prompt(monkeypatch):
    """A 线 Stage A：「发张你在海边的照片」→ 生图 prompt 含 beach 场景。"""
    from tests.test_selfie_wiring import _SM as _WSM  # 复用完整绑定
    cs.reset_selfie_provider()
    prov = cs.get_selfie_provider({"enabled": True, "backend": "openai"})
    seen = {}

    async def _gen(prompt, **kw):
        seen["prompt"] = prompt
        return cs.SelfieResult(ok=True, image_path="/tmp/x.png", provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)

    async def _send(chat_id, path, caption):
        return True

    try:
        sm = _WSM(selfie_cfg={"enabled": True, "min_bond_level": 0,
                              "provider": {"enabled": True, "backend": "openai"}},
                  gate=False)
        ctx = {"intimacy_score": 60, "entitlement": None,
               "_send_photo_to_chat": _send}
        out = await sm._handle_selfie_request("拍一张你在海边的照片嘛", "u1", ctx, 1)
        assert out == ""
        assert "beach" in seen["prompt"]
        # 场景随媒体注记落日志（供「跟上次一样」）
        assert ctx.get("_stage_media_scene", "") and "beach" in ctx["_stage_media_scene"]
    finally:
        cs.reset_selfie_provider()


async def test_run_autosend_caption_gets_scene(tmp_path, monkeypatch):
    """B 线：生成自拍时 directive.scene 显式化并透传给配文 LLM（图文叙事一体）。"""
    from src.inbox import image_autosend as ia
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    cs.reset_selfie_provider()
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    reset_selfie_cap_tracker()
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/tmp/out.png", "/static/out.png", "image"))
    cfg = {"companion": {"selfie": {
        "enabled": True,
        "scene_rotation": ["in a cozy cafe"],
        "provider": {"enabled": True, "backend": "openai", "api_key": "x"}}}}
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])

    async def _gen(prompt, **kw):
        return cs.SelfieResult(ok=True, image_path=str(tmp_path / "g.png"),
                               provider="openai")

    (tmp_path / "g.png").write_bytes(b"\x89PNGx")
    monkeypatch.setattr(prov, "generate", _gen)
    cap_seen = {}

    async def _caption(kind, subject, scene=""):
        cap_seen.update(kind=kind, scene=scene)
        return "配文来啦"

    sent = []

    async def _send_fn(mp, mu, mt, cap, inbox):
        sent.append(cap)
        return True

    ok = await ia.run_autosend_image(
        cfg, "telegram", "acct", "chat", "lin",
        "發個照片給我看看嘛", [], send_fn=_send_fn, llm_caption=_caption)
    assert ok is True
    assert cap_seen.get("scene") == "in a cozy cafe"  # 场景到达配文回调
    assert sent == ["配文来啦"]
    reset_persona_media_store()
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()


# ── ②c Phase20：行程线（场景从「点」到「线」）────────────────────────────────

def test_build_day_itinerary_four_buckets_deterministic():
    import datetime
    persona = {"selfie_scenes": [
        "sunny park, morning light", "cozy cafe, soft window light",
        "gym, evening workout", "cozy room, late night lamp"]}
    now = datetime.datetime(2026, 7, 14, 13, 0)
    itin = cs.build_day_itinerary(persona, {}, now=now)
    assert len(itin) == 4
    assert [x[0] for x in itin] == ["上午", "下午", "傍晚", "深夜"]
    # 确定性：同日重复计算完全一致
    assert itin == cs.build_day_itinerary(persona, {}, now=now)
    # 各桶经 Phase19 时段过滤：深夜桶不出白天场景
    night_scene = dict(itin)["深夜"]
    assert "morning" not in night_scene and "sunny" not in night_scene


def test_build_day_itinerary_empty_pool():
    assert cs.build_day_itinerary({}, {}) == []


def test_scene_chat_note_with_itinerary_marks_current():
    import datetime
    now = datetime.datetime(2026, 7, 14, 13, 0)  # 下午桶
    itin = [("上午", "park a"), ("下午", "cafe b"),
            ("傍晚", "gym c"), ("深夜", "room d")]
    note = cs.scene_chat_note("cafe b", itin, now=now)
    assert "你今天的动线" in note and "下午(现在)" in note
    assert "过去时" in note or "早些时候" in note  # 时态指引
    # 无行程线（<2 段）→ 回落单点 note（不出动线行）
    note2 = cs.scene_chat_note("cafe b", [("下午", "cafe b")], now=now)
    assert "你今天的动线" not in note2 and "cafe b" in note2


def test_inject_scene_state_includes_itinerary_and_flag():
    scfg = {"enabled": True,
            "scene_rotation": ["park, morning light", "cafe, afternoon",
                               "gym, evening", "room, late night"]}
    sm = _SM(selfie_cfg=scfg)
    ctx = {}
    sm._inject_scene_state(ctx)
    assert "你今天的动线" in ctx.get("_current_scene_note", "")
    # scene_itinerary=false → 只有单点场景
    sm2 = _SM(selfie_cfg=dict(scfg, scene_itinerary=False))
    ctx2 = {}
    sm2._inject_scene_state(ctx2)
    assert "_current_scene_note" in ctx2
    assert "你今天的动线" not in ctx2["_current_scene_note"]


# ── ②d Phase20：B 线媒体日志合流 + requested_scene ───────────────────────────

async def test_run_autosend_on_sent_and_requested_scene(tmp_path, monkeypatch):
    """B 线：requested_scene（「跟上次一样」解析结果）优先于轮换进 directive；
    发出成功后 on_sent(note, scene) 通知调用方（媒体日志合流通道）。"""
    from src.inbox import image_autosend as ia
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/tmp/out.png", "/static/out.png", "image"))
    cfg = {"companion": {"selfie": {
        "enabled": True, "scene_rotation": ["in a cozy cafe"],
        "provider": {"enabled": True, "backend": "openai", "api_key": "x"}}}}
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    (tmp_path / "g.png").write_bytes(b"\x89PNGx")

    async def _gen(prompt, **kw):
        return cs.SelfieResult(ok=True, image_path=str(tmp_path / "g.png"),
                               provider="openai")

    monkeypatch.setattr(prov, "generate", _gen)
    seen = {}

    async def _send_fn(mp, mu, mt, cap, inbox):
        return True

    ok = await ia.run_autosend_image(
        cfg, "telegram", "acct", "chatRS", "lin",
        "發個照片給我看看嘛", [], send_fn=_send_fn,
        requested_scene="at the beach, sea in the background",
        on_sent=lambda note, scene: seen.update(note=note, scene=scene))
    assert ok is True
    assert seen["note"].startswith("[图片]")
    assert "beach" in seen["scene"]  # requested_scene 覆盖了轮换场景
    reset_persona_media_store()
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()


async def test_run_autosend_on_sent_registry_scene_empty(monkeypatch):
    """注册相册现成图：场景未知 → on_sent scene 空串（不误标）。"""
    from src.inbox import image_autosend as ia
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    st = configure_persona_media_store(":memory:")
    st.add("lin", "photo", "/d/p.jpg", "/static/p.jpg", triggers=["跳舞"],
           caption="看我跳~")
    seen = {}

    async def _send_fn(mp, mu, mt, cap, inbox):
        return True

    ok = await ia.run_autosend_image(
        {"companion": {"selfie": {"enabled": True}}}, "telegram", "a", "cReg2",
        "lin", "给我跳舞", [], send_fn=_send_fn,
        on_sent=lambda note, scene: seen.update(note=note, scene=scene))
    assert ok is True
    assert seen["note"].startswith("[图片]") and seen["scene"] == ""
    reset_persona_media_store()


async def test_run_autosend_on_sent_exception_never_breaks_send(monkeypatch):
    """on_sent 回调抛异常 → 吞掉（日志记录绝不影响已完成的发送）。"""
    from src.inbox import image_autosend as ia
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    reset_persona_media_store()
    st = configure_persona_media_store(":memory:")
    st.add("lin", "photo", "/d/p2.jpg", "/static/p2.jpg", triggers=["跳舞"])

    async def _send_fn(mp, mu, mt, cap, inbox):
        return True

    def _boom(note, scene):
        raise RuntimeError("log fail")

    ok = await ia.run_autosend_image(
        {"companion": {"selfie": {"enabled": True}}}, "telegram", "a", "cExc",
        "lin", "给我跳舞", [], send_fn=_send_fn, on_sent=_boom)
    assert ok is True  # 发送结果不受回调异常影响
    reset_persona_media_store()


# ── ③ A 线异步兑现 ───────────────────────────────────────────────────────────

_GEN_ON = {"enabled": True, "min_bond_level": 0, "free_daily": 5,
           "provider": {"enabled": True, "backend": "openai", "api_key": "x"}}


async def _noop_send(*a, **k):
    return True


def _ctx_full():
    return {"intimacy_score": 60, "entitlement": None,
            "_send_photo_to_chat": _noop_send, "_send_to_chat": _noop_send}


@pytest.mark.asyncio
async def test_precheck_pass_and_fail_faces():
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_GEN_ON, guard_cfg={"async_fulfill": True})
        assert sm._async_fulfill_precheck("u1", _ctx_full(), 1) is True
        # selfie 关
        sm2 = _SM(selfie_cfg={"enabled": False}, guard_cfg={"async_fulfill": True})
        assert sm2._async_fulfill_precheck("u1", _ctx_full(), 1) is False
        # provider 非真出图（get_selfie_provider 为进程级单例 → 先 reset 再验）
        cs.reset_selfie_provider()
        sm3 = _SM(selfie_cfg=dict(_GEN_ON, provider={"enabled": False}))
        assert sm3._async_fulfill_precheck("u1", _ctx_full(), 1) is False
        cs.reset_selfie_provider()  # 恢复 _GEN_ON provider 供后续断言
        # 无文本补偿通道 → 拒（失败没法圆场就不敢保留承诺）
        ctx4 = _ctx_full()
        ctx4.pop("_send_to_chat")
        sm4 = _SM(selfie_cfg=_GEN_ON)
        assert sm4._async_fulfill_precheck("u1", ctx4, 1) is False
        # 无图通道 → 拒
        ctx5 = _ctx_full()
        ctx5.pop("_send_photo_to_chat")
        assert sm4._async_fulfill_precheck("u1", ctx5, 1) is False
        # in-flight 去重
        sm5 = _SM(selfie_cfg=_GEN_ON)
        sm5._promise_fulfill_inflight = {"u1"}
        assert sm5._async_fulfill_precheck("u1", _ctx_full(), 1) is False
        # 关系浅（decide_selfie too_soon）→ 拒
        ctx6 = _ctx_full()
        ctx6["intimacy_score"] = 0
        sm6 = _SM(selfie_cfg=dict(_GEN_ON, min_bond_level=3))
        assert sm6._async_fulfill_precheck("u1", ctx6, 1) is False
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_guard_keeps_promise_and_fulfills_async(monkeypatch):
    """async_fulfill 开 + 预检过：守卫保留承诺原文并 spawn 后台任务；
    任务成功 → 媒体日志 + last_reply 追加「照片已随后发出」。"""
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_GEN_ON, guard_cfg={"async_fulfill": True})
        calls = {}

        async def _fake_directive_selfie(scene, uid, ctx, chat_id, scfg, lp=""):
            calls["scene"] = scene
            ctx["_stage_media_note"] = "[图片] 兑现自拍"
            return True

        sm._photo_directive_selfie = _fake_directive_selfie
        monkeypatch.setattr("random.uniform", lambda a, b: 0.0)  # 免等 4-9s
        ctx = _ctx_full()
        ctx["last_reply"] = "等我拍一张给你哈～"
        out = sm._apply_media_promise_guard(
            "等我拍一张给你哈～", ctx, user_id_str="u1", chat_id=1)
        assert out == "等我拍一张给你哈～"  # 承诺保留（不剥）
        # 等 spawn 的任务跑完
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not sm._promise_fulfill_inflight:
                break
        assert calls.get("scene") is not None  # 场景来自 resolve_current_scene（可为空串）
        assert ctx["_media_sent_log"][-1]["note"] == "[图片] 兑现自拍"
        assert "照片已随后发出" in ctx["last_reply"]
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_guard_fulfill_failure_sends_compensation(monkeypatch):
    """任务失败 → 语言对齐台阶补偿文本经 _send_to_chat 发出（闭环诚实）。"""
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_GEN_ON, guard_cfg={"async_fulfill": True})
        sent_texts = []

        async def _fake_directive_selfie(scene, uid, ctx, chat_id, scfg, lp=""):
            return False  # 生成/发送失败

        async def _capture_text(chat_id, text):
            sent_texts.append(text)
            return True

        sm._photo_directive_selfie = _fake_directive_selfie
        monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
        ctx = _ctx_full()
        ctx["_send_to_chat"] = _capture_text
        out = sm._apply_media_promise_guard(
            "等我拍一张给你哈～", ctx, user_id_str="u2", chat_id=1)
        assert out == "等我拍一张给你哈～"
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not sm._promise_fulfill_inflight:
                break
        assert sent_texts and ("改天" in sent_texts[0] or "make it up" in sent_texts[0])
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_guard_async_off_or_precheck_fail_still_retracts():
    """async_fulfill 关（默认）/预检不过 → 保持 P0 撤回行为。"""
    cs.reset_selfie_provider()
    try:
        # 默认关
        sm = _SM(selfie_cfg=_GEN_ON)
        out = sm._apply_media_promise_guard(
            "等我拍一张给你哈～", _ctx_full(), user_id_str="u3", chat_id=1)
        assert "拍一张" not in out and out.strip()
        # 开了但无通道（预检不过）→ 撤回
        sm2 = _SM(selfie_cfg=_GEN_ON, guard_cfg={"async_fulfill": True})
        out2 = sm2._apply_media_promise_guard(
            "等我拍一张给你哈～", {"intimacy_score": 60},
            user_id_str="u4", chat_id=1)
        assert "拍一张" not in out2 and out2.strip()
    finally:
        cs.reset_selfie_provider()


@pytest.mark.asyncio
async def test_guard_skips_refulfill_after_directive_failure():
    """photo_directive 刚试过且失败（_photo_attempt_failed）→ 不再兑现重试，直接撤回。"""
    cs.reset_selfie_provider()
    try:
        sm = _SM(selfie_cfg=_GEN_ON, guard_cfg={"async_fulfill": True})
        spawned = []
        sm._spawn_promise_fulfill_task = (
            lambda *a, **k: spawned.append(1))
        ctx = _ctx_full()
        ctx["_photo_attempt_failed"] = True
        out = sm._apply_media_promise_guard(
            "等我拍一张给你哈～", ctx, user_id_str="u5", chat_id=1)
        assert "拍一张" not in out  # 撤回而非保留
        assert not spawned          # 没有二次兑现（防双倍烧卡）
        assert "_photo_attempt_failed" not in ctx  # 标志已消费
    finally:
        cs.reset_selfie_provider()
