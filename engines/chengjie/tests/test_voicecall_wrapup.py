# -*- coding: utf-8 -*-
"""通话收尾闭环门禁：assemble_wrapup（纯计划）+ make_wrapup_hook（接地落库 + follow-up）。

关键不变量：
- 记忆只从**用户转写**抽取并接地（AI 的话永不入记忆，防 Phase8 幻觉链）；
- severe 危机通话不 follow-up（防二次刺激）但仍落库；
- 极短/未接通话不落库不追消息（防噪声/骚扰）。
"""
import asyncio

from src.voicecall.bridge import CallResult
from src.voicecall.wrapup import (
    assemble_wrapup,
    make_deferred_follow_up,
    make_wrapup_hook,
)


def _result(**kw):
    r = CallResult(chat_id=42)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# ── assemble_wrapup 纯函数 ──────────────────────────────────────────────────
def test_assemble_normal_call_stores_and_followups():
    r = _result(accepted=True, duration_sec=180.0,
                user_transcript=["我最近在学吉他", "周末想去爬山"])
    plan = assemble_wrapup(r)
    assert plan.store_memory is True
    assert plan.follow_up is True
    assert "分钟" in plan.follow_up_seed
    assert "学吉他" in plan.user_text


def test_assemble_short_call_no_memory_no_followup():
    # 5 秒误触、无转写 → 不落库不追消息
    r = _result(accepted=True, duration_sec=5.0, user_transcript=[])
    plan = assemble_wrapup(r)
    assert plan.store_memory is False
    assert plan.follow_up is False


def test_assemble_severe_call_stores_but_no_followup():
    # 危机通话：仍落库（记住处境供人工/后续），但不主动 follow-up（防二次刺激）
    r = _result(accepted=True, duration_sec=200.0, max_safety_level="severe",
                user_transcript=["我真的撑不下去了"])
    plan = assemble_wrapup(r)
    assert plan.store_memory is True
    assert plan.follow_up is False


def test_assemble_not_accepted_no_memory():
    r = _result(accepted=False, duration_sec=0.0, user_transcript=[])
    plan = assemble_wrapup(r)
    assert plan.store_memory is False
    assert plan.follow_up is False


# ── make_wrapup_hook：接地落库 ───────────────────────────────────────────────
def test_hook_stores_grounded_facts_from_user_only():
    stored = []

    def _add(key, fact):
        stored.append((key, fact))

    # 抽取器直接回显（模拟），验证接地：与用户原话有重叠的留，凭空的丢
    def _extract(text):
        return ["用户在学吉他", "用户明天要去火星"]   # 后者与原话无重叠 → 应被接地丢弃

    hook = make_wrapup_hook(
        memory_key_fn=lambda ctx: "telegram:42",
        memory_add=_add, extract=_extract)

    class Ctx: pass
    r = _result(accepted=True, duration_sec=120.0,
                user_transcript=["我在学吉他，最近很上头"])
    asyncio.run(hook(Ctx(), r))
    facts = [f for _, f in stored]
    assert "用户在学吉他" in facts           # 与"学吉他"重叠 → 接地保留
    assert "用户明天要去火星" not in facts    # 凭空 → 接地丢弃（防幻觉污染）
    assert all(k == "telegram:42" for k, _ in stored)


def test_hook_no_store_when_short_call():
    stored = []
    hook = make_wrapup_hook(
        memory_key_fn=lambda ctx: "telegram:42",
        memory_add=lambda k, f: stored.append((k, f)),
        extract=lambda t: ["something"])
    r = _result(accepted=True, duration_sec=3.0, user_transcript=[])
    asyncio.run(hook(object(), r))
    assert stored == []                       # 无用户转写 → 不落库


# ── make_wrapup_hook：follow-up ─────────────────────────────────────────────
def test_hook_followup_called_for_normal_call():
    seen = {}

    async def _follow(ctx, plan):
        seen["seed"] = plan.follow_up_seed

    hook = make_wrapup_hook(
        memory_key_fn=lambda ctx: "telegram:42",
        memory_add=lambda k, f: None,
        extract=lambda t: [],
        follow_up=_follow)
    r = _result(accepted=True, duration_sec=90.0, user_transcript=["今天心情不错"])
    asyncio.run(hook(object(), r))
    assert "seed" in seen and seen["seed"]


def test_hook_no_followup_for_severe():
    called = {"n": 0}

    async def _follow(ctx, plan):
        called["n"] += 1

    hook = make_wrapup_hook(
        memory_key_fn=lambda ctx: "telegram:42",
        memory_add=lambda k, f: None,
        extract=lambda t: [],
        follow_up=_follow)
    r = _result(accepted=True, duration_sec=200.0, max_safety_level="severe",
                user_transcript=["撑不下去了"])
    asyncio.run(hook(object(), r))
    assert called["n"] == 0                   # severe → 不追消息


# ── make_deferred_follow_up：走 deferred 队列（继承 kill-switch/安静时段/pacing 护栏）──
def test_deferred_follow_up_enqueues_with_delay():
    enq = []

    def _enqueue(**kw):
        enq.append(kw)
        return 1

    class Ctx:
        account_id = "8244899900"
        chat_key = "8118214990"

    follow = make_deferred_follow_up(
        enqueue=_enqueue, platform="telegram",
        account_id_fn=lambda c: c.account_id,
        chat_key_fn=lambda c: c.chat_key,
        delay_min_sec=180, delay_max_sec=420, rand=lambda: 0.5)
    import time
    r = _result(accepted=True, duration_sec=120.0, user_transcript=["今天很开心"])
    plan = assemble_wrapup(r)
    asyncio.run(follow(Ctx(), plan))
    assert len(enq) == 1
    kw = enq[0]
    assert kw["platform"] == "telegram"
    assert kw["account_id"] == "8244899900"
    assert kw["chat_key"] == "8118214990"
    assert kw["reason"] == "voice_call_followup"
    assert kw["defer_until"] > time.time() + 250   # 延迟 ~300s（不秒回）
    assert kw["extra"]["source"] == "voice_call"


def test_deferred_follow_up_skips_when_no_account():
    enq = []
    follow = make_deferred_follow_up(
        enqueue=lambda **kw: enq.append(kw),
        account_id_fn=lambda c: "", chat_key_fn=lambda c: "x")
    plan = assemble_wrapup(_result(accepted=True, duration_sec=90.0,
                                   user_transcript=["hi"]))
    asyncio.run(follow(object(), plan))
    assert enq == []                              # 无 account_id → 不入队


def test_deferred_follow_up_via_hook_end_to_end():
    # make_wrapup_hook + make_deferred_follow_up 串起来：normal call → 入队一条 follow-up
    enq = []

    class Ctx:
        account_id = "a1"
        chat_key = "c1"

    follow = make_deferred_follow_up(
        enqueue=lambda **kw: enq.append(kw) or 1,
        account_id_fn=lambda c: c.account_id, chat_key_fn=lambda c: c.chat_key,
        rand=lambda: 0.0)
    hook = make_wrapup_hook(memory_key_fn=lambda c: "telegram:c1",
                            memory_add=lambda k, f: None, extract=lambda t: [],
                            follow_up=follow)
    r = _result(accepted=True, duration_sec=120.0, user_transcript=["聊得很开心"])
    asyncio.run(hook(Ctx(), r))
    assert len(enq) == 1 and enq[0]["reason"] == "voice_call_followup"


def test_hook_records_usage_closing_budget_loop():
    # 收尾记账户用量（闭合预算环）：接通过就记，即便很短
    recorded = []

    class Ctx:
        account_id = "acc1"

    hook = make_wrapup_hook(
        memory_key_fn=lambda c: "telegram:c1", memory_add=lambda k, f: None,
        extract=lambda t: [],
        usage_record=lambda ak, dur: recorded.append((ak, dur)),
        account_key_fn=lambda c: "telegram:" + c.account_id)
    r = _result(accepted=True, duration_sec=42.0, user_transcript=["hi"])
    asyncio.run(hook(Ctx(), r))
    assert recorded == [("telegram:acc1", 42.0)]


def test_hook_no_usage_when_not_accepted():
    recorded = []
    hook = make_wrapup_hook(
        memory_key_fn=lambda c: "k", memory_add=lambda k, f: None, extract=lambda t: [],
        usage_record=lambda ak, dur: recorded.append((ak, dur)),
        account_key_fn=lambda c: "telegram:acc1")
    r = _result(accepted=False, duration_sec=0.0)
    asyncio.run(hook(object(), r))
    assert recorded == []                         # 没接通 → 不记账


def test_hook_survives_broken_io():
    # 抽取器/落库/follow-up 全抛异常 → 安全退化不外泄
    def _boom_extract(t): raise RuntimeError("x")

    async def _boom_follow(ctx, plan): raise RuntimeError("y")

    hook = make_wrapup_hook(
        memory_key_fn=lambda ctx: "telegram:42",
        memory_add=lambda k, f: (_ for _ in ()).throw(RuntimeError("z")),
        extract=_boom_extract, follow_up=_boom_follow)
    r = _result(accepted=True, duration_sec=90.0, user_transcript=["hi there"])
    asyncio.run(hook(object(), r))            # 不抛即通过
