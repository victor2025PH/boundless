"""通话收尾闭环 —— 转写 → 记忆（过接地护栏）→ 挂断后 follow-up（可测装配 + 注入型 IO）。

目标：让「刚刚电话里你说的那个事…」在后续聊天成立，且**不把 AI 自己的话/系统行为污染成
用户事实**（Phase8 幻觉事故的教训）。

分两层：
  - ``assemble_wrapup``（纯函数）：CallResult → ``WrapupPlan``（记忆候选源文本=**仅用户转写**、
    follow-up 种子、时长/安全标记、是否值得落库）——可确定性单测。
  - ``make_wrapup_hook``：把纯计划接上注入的 IO（记忆库 add_fact / 事实抽取器 / follow-up 发送器），
    返回 bridge 的 ``on_wrapup`` async 回调。所有 IO 缺失/异常安全退化，绝不抛。

**接地铁律**：事实只从 ``transcript.user``（用户亲口说的）抽取并**用用户原话接地**
（``filter_grounded_facts``）——``transcript.assistant``（AI 的话）永不入记忆，从源头杜绝
「AI 臆测被存成用户事实 → 下轮更确信」的自我强化幻觉链。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WrapupPlan:
    """收尾计划（纯数据，无 IO）。"""
    chat_id: int
    user_text: str                       # 拼接的用户转写（事实抽取 + 接地的**唯一**来源）
    duration_sec: float
    turns: int                           # 用户轮数（值得落库/follow-up 的信号）
    safety_level: str = "none"           # none|elevated|severe（危机通话收尾更克制）
    store_memory: bool = False           # 是否值得抽事实落库
    follow_up: bool = False              # 是否值得挂断后 follow-up
    follow_up_seed: str = ""             # follow-up 消息的上下文种子（供 LLM 生成）


def assemble_wrapup(result: Any, *, min_turns_for_memory: int = 1,
                    min_sec_for_followup: float = 20.0) -> WrapupPlan:
    """CallResult → WrapupPlan（纯函数）。

    - 记忆：有用户转写且轮数达标才落库（极短/空通话不值得，避免噪声）；
    - follow-up：接通且时长达标才发（几秒的误触通话不追消息，防骚扰）；
    - severe 危机通话：**不 follow-up**（刚经历危机对话，主动追消息可能二次刺激；
      安全侧已在通话中拉人，收尾交人工），但仍落库（记住 TA 的处境，供人工与后续参考）。
    """
    user_lines: List[str] = [s for s in (getattr(result, "user_transcript", None) or [])
                             if str(s).strip()]
    turns = len(user_lines)
    user_text = "\n".join(user_lines).strip()
    dur = float(getattr(result, "duration_sec", 0.0) or 0.0)
    accepted = bool(getattr(result, "accepted", False))
    level = str(getattr(result, "max_safety_level", "none") or "none")

    store_memory = bool(accepted and turns >= max(1, int(min_turns_for_memory)) and user_text)
    follow_up = bool(accepted and turns >= 1 and dur >= float(min_sec_for_followup)
                     and level != "severe")
    seed = ""
    if follow_up:
        mins = max(1, int(round(dur / 60.0)))
        seed = f"刚和对方通了大约{mins}分钟电话，自然地延续通话里聊到的话题关心一下。"
    return WrapupPlan(
        chat_id=int(getattr(result, "chat_id", 0) or 0),
        user_text=user_text, duration_sec=dur, turns=turns,
        safety_level=level, store_memory=store_memory,
        follow_up=follow_up, follow_up_seed=seed)


# 注入型 IO 签名
ExtractFn = Callable[[str], List[str]]                     # 用户文本 → 候选事实（默认启发式）
MemoryAddFn = Callable[[str, str], Any]                    # (memory_key, fact) → 落库
FollowUpFn = Callable[[Any, "WrapupPlan"], Awaitable[None]]  # (ctx, plan) → 挂断后 follow-up


def make_wrapup_hook(
    *,
    memory_key_fn: Callable[[Any], str],
    memory_add: Optional[MemoryAddFn] = None,
    extract: Optional[ExtractFn] = None,
    follow_up: Optional[FollowUpFn] = None,
    usage_record: Optional[Callable[[str, float], Any]] = None,
    account_key_fn: Optional[Callable[[Any], str]] = None,
    min_turns_for_memory: int = 1,
    min_sec_for_followup: float = 20.0,
) -> Callable[[Any, Any], Awaitable[None]]:
    """装配 bridge 的 ``on_wrapup`` 回调。

    - ``memory_key_fn(ctx) -> str``：把 CallContext 映射到记忆键（与草稿/浏览器通话同口径，
      通常 ``platform:chat_key``）；
    - ``memory_add(key, fact)``：落库器（由 wiring 注入真实记忆库的 add_fact；缺省 None=只跑
      接地过滤不落库——本仓记忆库随 app 构造传入，无进程级 getter，故不猜全局单例）；
    - ``extract(user_text) -> facts``：事实抽取（缺省启发式纯函数；LLM 抽取可注入）；
    - ``follow_up(ctx, plan)``：挂断后 follow-up 发送（缺省无=不发）；
    - ``usage_record(account_key, duration_sec)`` + ``account_key_fn(ctx)``：**闭合预算环**——
      每通接通的电话在收尾时记账户用量（``CallUsageStore.record_call``），下次来电的预算闸
      （calls_today/minutes_today）才有数可读。两者需同时提供才记；缺则跳过（旧行为）。
    """
    if extract is None:
        try:
            from src.utils.memory_heuristic import extract_heuristic_facts
            extract = extract_heuristic_facts
        except Exception:
            extract = lambda _t: []  # noqa: E731

    async def _hook(ctx: Any, result: Any) -> None:
        # 用量记账优先（即便后续记忆/follow-up 异常也不漏账，防预算闸失效）：
        # 只要接通过就计入（哪怕 5 秒——它占用了主机 + 是 userbot 信号）。
        if usage_record is not None and account_key_fn is not None \
                and bool(getattr(result, "accepted", False)):
            try:
                ak = str(account_key_fn(ctx) or "")
                if ak:
                    usage_record(ak, float(getattr(result, "duration_sec", 0.0) or 0.0))
            except Exception:
                logger.debug("[voicecall] wrapup 用量记账失败", exc_info=True)
        try:
            plan = assemble_wrapup(result, min_turns_for_memory=min_turns_for_memory,
                                   min_sec_for_followup=min_sec_for_followup)
        except Exception:
            logger.debug("[voicecall] wrapup 装配失败", exc_info=True)
            return
        if plan.store_memory:
            await _store_grounded(ctx, plan, memory_key_fn, memory_add, extract)
        if plan.follow_up and follow_up is not None:
            try:
                await follow_up(ctx, plan)
            except Exception:
                logger.debug("[voicecall] wrapup follow-up 失败", exc_info=True)

    return _hook


async def _store_grounded(ctx: Any, plan: "WrapupPlan",
                          memory_key_fn: Callable[[Any], str],
                          memory_add: Optional[MemoryAddFn],
                          extract: ExtractFn) -> None:
    """抽事实 → 用用户原话接地 → 落库（source=user_stated）。全程防御式。"""
    try:
        facts = list(extract(plan.user_text) or [])
    except Exception:
        facts = []
    if not facts:
        return
    try:
        from src.ai.memory_grounding import filter_grounded_facts
        kept, dropped = filter_grounded_facts(facts, plan.user_text)
    except Exception:
        kept, dropped = facts, []
    if dropped:
        logger.debug("[voicecall] wrapup 接地丢弃 %d 条", len(dropped))
    if not kept or memory_add is None:
        # 无落库器（未接线）→ 不落库，但接地过滤已跑（可观测丢弃）。记忆库实例由 wiring
        # 侧注入（app.state.episodic_memory / skill_manager._episodic_store 的 add_fact），
        # 本模块不猜全局单例（本仓记忆库是随 app 构造传入，无进程级 getter）。
        return
    try:
        key = memory_key_fn(ctx)
    except Exception:
        key = ""
    if not key:
        return
    for fact in kept:
        try:
            memory_add(key, fact)
        except Exception:
            logger.debug("[voicecall] wrapup 落库失败", exc_info=True)


def make_deferred_follow_up(
    *,
    enqueue: Callable[..., Any],
    platform: str = "telegram",
    account_id_fn: Callable[[Any], str],
    chat_key_fn: Callable[[Any], str],
    compose: Optional[Callable[[Any, "WrapupPlan"], str]] = None,
    delay_min_sec: float = 180.0,
    delay_max_sec: float = 420.0,
    rand: Optional[Callable[[], float]] = None,
) -> FollowUpFn:
    """把 wrapup 的挂断后 follow-up 做成 **deferred 队列入队**（而非自建发送）。

    为什么走 deferred：`companion_proactive` 的 deferred outbox 自带 kill-switch / 安静时段 /
    pacing / staleness 全套护栏——通话后 follow-up 复用它，就**天然继承**这些护栏，绝不绕过
    （自建发送极易漏掉安静时段/风控闸，把「关心」发成「骚扰」）。

    - ``enqueue``：deferred store 的 enqueue（关键字参数 platform/account_id/chat_key/
      reply_text/defer_until/reason/extra）；
    - ``compose(ctx, plan) -> text``：生成 follow-up 文案（缺省用 plan.follow_up_seed 作
      **给下游 LLM 的种子**，标 reason=voice_call_followup，由 drain 侧按人设生成/直发）；
    - 延迟 ``[delay_min,delay_max]`` 随机（真人挂了电话不会秒回消息）。
    """
    _rand = rand or __import__("random").random

    async def _follow(ctx: Any, plan: "WrapupPlan") -> None:
        try:
            acct = str(account_id_fn(ctx) or "")
            chat = str(chat_key_fn(ctx) or "")
            if not acct or not chat:
                return
            text = (compose(ctx, plan) if compose else plan.follow_up_seed) or ""
            if not str(text).strip():
                return
            lo = max(0.0, float(delay_min_sec))
            hi = max(lo, float(delay_max_sec))
            delay = lo + (hi - lo) * min(1.0, max(0.0, float(_rand())))
            import time as _t
            enqueue(platform=platform, account_id=acct, chat_key=chat,
                    reply_text=str(text), defer_until=_t.time() + delay,
                    reason="voice_call_followup",
                    extra={"source": "voice_call", "seed": plan.follow_up_seed})
        except Exception:
            logger.debug("[voicecall] deferred follow-up 入队失败", exc_info=True)

    return _follow


__all__ = ["WrapupPlan", "assemble_wrapup", "make_wrapup_hook", "make_deferred_follow_up"]
