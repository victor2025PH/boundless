"""排练引擎（dry-run）—— 完整跑一遍导演循环，但**一条真消息都不发**。

## 为什么先做排练而不是直接上真群

炒群的试错成本是**账号**：一场演砸的戏可能带走几个号，而号是要养的。所以 Phase 1
的交付物是「离线演一遍给你看」——选角、弧线、台词、节奏、真人插话应对全都真跑，
只把最后的投递换成记录。

**安全靠架构而非纪律**：本模块**根本不 import**
``AccountOrchestrator``——物理上不可能发出消息，不依赖任何人记得把开关关上。真发
链路（Phase 3）会是另一个模块，届时唯一出口仍是 ``orchestrator.send``（自带
Kill-Switch / 反封号闸）。

## 虚拟时钟

排练不真等。节奏引擎算出的间隔累加进虚拟时钟，一场 40 分钟的戏在几秒内演完，
输出的时刻表仍是真实节奏——运营看得到「第 7 分钟才有人提产品」。

## 模拟真人插话

``human_script`` 让你在离线就能排练最难的场景：真人在第 N 条后插一句话，戏会怎么
反应（让路 → 响应式选人 → 真实回应而非继续念稿）。真群里这个场景既高频又不可控，
只能靠排练验。
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.companion.group_show.casting import cast_roles, validate_casting
from src.companion.group_show.director import GroupShowDirector
from src.companion.group_show.ecp import project_history_for
from src.companion.group_show.naturalness import naturalness_score
from src.companion.group_show.pacing import beat_interval_seconds
from src.companion.group_show.playbook import (
    BeatDirective,
    CastMember,
    Casting,
    Playbook,
    ShowEvent,
    ShowState,
    validate_playbook,
)

logger = logging.getLogger(__name__)

#: 台词生成器契约：给发言人 + ECP 投影后的 messages + 本拍意图，产出一句台词。
#: 同步或异步均可（runtime 自动 await）。
GenerateFn = Callable[[CastMember, List[Dict[str, str]], BeatDirective], Any]

#: 连续多少拍生成不出台词就判定「生成侧挂了」并终止。
#: 存在的理由：静默演成空场却报告 ``completed`` 是最坏的失败模式——你以为排练
#: 通过了，其实一个字都没生成（2026-07-25 首次真 LLM 排练就踩了：config 没
#: await，全场 0 条却显示收场正常）。宁可显式失败。
MAX_CONSECUTIVE_SKIPS = 3


@dataclass(frozen=True)
class RehearsalLine:
    """排练结果的一行（＝将来真发时的一条消息）。"""

    seq: int
    at_seconds: float
    account_id: str
    display_name: str
    persona_id: str
    role: str
    beat_id: str
    text: str
    soft_level: int = 0
    media: str = ""
    kind: str = "line"


@dataclass
class RehearsalResult:
    """一次排练的完整产物（可直接渲染给运营看，也可落库）。"""

    session_id: str
    playbook_id: str
    group_key: str
    lines: List[RehearsalLine] = field(default_factory=list)
    casting: Optional[Casting] = None
    terminate_reason: str = ""
    naturalness: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = True
    #: 生成不出台词而被跳过的拍数（>0 说明生成侧不稳，结果不能全信）
    skipped: int = 0

    @property
    def line_count(self) -> int:
        return sum(1 for ln in self.lines if ln.kind in ("line", "media"))


# ── 台词生成器 ──────────────────────────────────────────────────────────────


def flatten_messages(messages: Sequence[Dict[str, str]]) -> str:
    """ECP 的 messages → 单条 prompt（给只吃字符串的 LLM 入口用）。

    ``assistant``＝发言人自己说过的话，``user``＝群里别人说的（ECP 已带
    ``[群友 X]`` 前缀），所以这里只需按序拼接、把自己的话标出来即可。
    """
    parts: List[str] = []
    for m in messages or []:
        role = str((m or {}).get("role") or "").strip()
        content = str((m or {}).get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"（你之前说过）{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def stub_generator(marker: str = "") -> GenerateFn:
    """占位生成器：不调 LLM，产确定性台词。

    用来**只验编排结构**（选角/轮换/节奏/让路/终止）——秒出、可断言、CI 可跑。
    看真实台词效果时换 :func:`llm_generator`。
    """

    def _gen(speaker: CastMember, messages: List[Dict[str, str]],
             directive: BeatDirective) -> str:
        tag = f"{marker} " if marker else ""
        if directive.respond_to_human:
            return f"{tag}[{speaker.persona_id}·回应真人] {directive.intent}"
        return f"{tag}[{speaker.persona_id}·{directive.beat_id}] {directive.intent}"

    return _gen


def llm_generator(ai_client: Any, *, temperature: float = 0.9) -> GenerateFn:
    """真 LLM 台词生成器（排练用近路）。

    ⚠ 这里走 ``ai_client.chat(prompt)`` 的**通用入口**，不是该号的人设发声链。
    排练阶段够用（看弧线与语感），但**真发上线（Phase 3）必须改走
    ``persona_reply`` / ``generate_inbox_draft``**，否则台词没有人设声音、也不过
    persona_guard / 语言守卫 / 危机安全网这些出站防线。
    """

    async def _gen(speaker: CastMember, messages: List[Dict[str, str]],
                   directive: BeatDirective) -> str:
        prompt = flatten_messages(messages)
        try:
            out = await ai_client.chat(
                prompt, strategy_overrides={"temperature": temperature})
        except TypeError:
            out = await ai_client.chat(prompt)
        except Exception as exc:  # noqa: BLE001 —— 单拍失败不该炸掉整场排练
            # 但必须留下痕迹：静默返空会演成空场还报「正常收场」
            logger.warning("[group_show] 台词生成失败 beat=%s: %s",
                           directive.beat_id, exc)
            return ""
        return str(out or "").strip()

    return _gen


# ── 群感知提示（复用生产链路的单一事实源）────────────────────────────────────


def group_hint_text() -> str:
    """群聊场景约束——直接复用生产链路的那一份，避免两处漂移。

    ``SkillManager._group_chat_hint_text`` 是 2026-07-25 群聊灰度实测教训的产物
    （进群自我介绍像私聊开场 / 把群里闲聊当「对你说」/ 长篇输出）。群戏面对的是
    同一批陷阱，必须用同一份文本；导入失败才回落内置副本。
    """
    try:
        from src.skills.skill_manager import SkillManager
        return str(SkillManager._group_chat_hint_text() or "").strip()
    except Exception:  # noqa: BLE001
        return (
            "你此刻在一个多人群聊里发言（不是一对一私聊）：\n"
            "- 回复对全体群成员可见；不要自我介绍、不要用私聊式亲昵开场\n"
            "- 群聊回复要简短口语化（一两句为宜），像群友插话，不要长篇输出"
        )


# ── 排练主循环 ──────────────────────────────────────────────────────────────


async def rehearse(
    playbook: Playbook,
    candidates: Sequence[Dict[str, Any]],
    *,
    group_key: str = "rehearsal",
    platform: str = "telegram",
    generate: Optional[GenerateFn] = None,
    config: Optional[Dict[str, Any]] = None,
    seed: int = 0,
    human_script: Optional[Sequence[Tuple[int, str, str]]] = None,
    persona_gender: Optional[Dict[str, str]] = None,
    fingerprint_groups: Optional[Dict[str, str]] = None,
    co_performance: Optional[Dict[Tuple[str, str], int]] = None,
    max_speakers: int = 0,
    session_id: str = "",
    store: Any = None,
) -> RehearsalResult:
    """离线演一场戏，返回完整排练结果。**不发任何真消息。**

    ``human_script``：``[(在第几条我方发言之后, 真人名, 真人说的话), ...]``——
    模拟真人插话，用来排练让路与响应式接话。
    ``store``：传入 :class:`GroupShowStore` 则落库（可回放/续演）；``None``＝纯内存。

    ``co_performance`` / ``max_speakers``：演出矩阵的两个入口（见
    :mod:`~src.companion.group_show.performance`）——前者让选角优先挑「没跟已选演员
    同台开过口」的号，后者按预算封顶本场开口人数。两者都由**调用方**从演出台账
    （``GroupShowStore.performance_ledger()``）算好传进来，与 ``fingerprint_groups``
    同一个姿势：本模块不做 I/O，排练要能在没有库的环境里跑。
    """
    gen: GenerateFn = generate or stub_generator()
    sid = str(session_id or f"reh_{uuid.uuid4().hex[:12]}")
    warnings: List[str] = []

    # ① 剧本体检
    warnings.extend(validate_playbook(playbook))

    # ② 选角
    # seed 传群 key：少了它，同一剧本的第一个槽跨群恒定由同一个号演（第一个槽选人时
    # 场上还没人、同台历史全为 0，排序完全落到确定性轮换键上）。实测代价是安全场次
    # 缩水到四分之一——见 casting._rotation_key。
    casting = cast_roles(
        playbook, candidates,
        fingerprint_groups=fingerprint_groups, persona_gender=persona_gender,
        co_performance=co_performance, max_speakers=max_speakers,
        seed=str(group_key or ""))
    # 复查必须拿到与选角同一份指纹表。漏传会让 validate_casting 走「没表→跳过」分支，
    # 于是同台关联复查静默失效——这行曾经真的漏了，且因为跳过时不出声而一直没被发现。
    warnings.extend(validate_casting(
        casting, playbook,
        fingerprint_groups=fingerprint_groups, persona_gender=persona_gender))

    started = time.time()
    state = ShowState(
        session_id=sid, group_key=str(group_key), playbook=playbook,
        casting=casting, platform=str(platform), dry_run=True,
        started_at=started, status="pending")

    result = RehearsalResult(
        session_id=sid, playbook_id=playbook.id, group_key=str(group_key),
        casting=casting, warnings=warnings, dry_run=True)

    # 硬错（非 warn:）直接不开演——号不够/剧本坏了排练也没意义
    hard = [w for w in warnings if not w.startswith("warn:")]
    if hard or not casting.members:
        result.terminate_reason = "invalid"
        return result

    director = GroupShowDirector(state, config)
    rng = random.Random(int(seed))
    names = {m.account_id: (m.display_name or m.persona_id or m.account_id)
             for m in casting.members}
    hint = group_hint_text()
    humans = _human_cues(human_script)

    clock = 0.0
    prev_len = 0
    guard = 0
    skips = 0   # 连续生成失败计数
    max_iterations = 200  # 死循环兜底（导演逻辑若有回归，宁可截断也不挂住）

    while guard < max_iterations:
        guard += 1
        now = started + clock

        # 模拟真人插话：按「我方已发言条数」触发。同一位置可挂多条——真人连着刷
        # 好几句正是触发「真人接管」的典型场景，接口必须表达得了它。
        spoken = sum(1 for e in state.events if e.kind in ("line", "media"))
        for who, said in humans.pop(spoken, ()):
            director.observe_human(said, sender=who, ts=now)
            result.lines.append(RehearsalLine(
                seq=state.next_seq - 1, at_seconds=round(clock, 1),
                account_id=who, display_name=who, persona_id="", role="human",
                beat_id="", text=said, kind="human"))
            names.setdefault(who, who)

        stop, reason = director.should_terminate(now=now)
        if stop:
            result.terminate_reason = reason
            break

        # 真人刚说完话的让路窗：虚拟时钟快进过去，不抢话
        if director.should_yield_to_human(now=now):
            clock += director.cfg.human_yield_seconds + 1.0
            continue

        speaker = director.select_next_speaker()
        directive = director.next_directive()
        if speaker is None or directive is None:
            result.terminate_reason = "no_speaker"
            break

        messages = project_history_for(
            speaker, state.events, directive=directive,
            group_hint=hint, name_resolver=lambda a: names.get(a, a))

        text = gen(speaker, messages, directive)
        if _is_awaitable(text):
            text = await text
        text = str(text or "").strip()
        if not text:
            # 单拍生成失败：跳过而不是卡死（真发时同理，宁可少说一句）。
            # 但连续失败＝生成侧挂了，必须显式终止——空场报「completed」会骗人。
            skips += 1
            result.skipped += 1
            director.advance_beat(ShowEvent(
                seq=state.next_seq, ts=now, speaker_account=speaker.account_id,
                role=speaker.slot, beat_id=directive.beat_id, text="",
                kind="skip"))
            if skips >= MAX_CONSECUTIVE_SKIPS:
                result.terminate_reason = "generation_failed"
                logger.warning(
                    "[group_show] 连续 %d 拍生成不出台词，判定生成侧异常，中止排练",
                    skips)
                break
            continue
        skips = 0

        beat = state.playbook.beat_at(state.beat_cursor) or state.current_beat
        interval = beat_interval_seconds(
            beat, prev_text_len=prev_len, next_text_len=len(text), rng=rng)
        clock += float(interval)
        ev = ShowEvent(
            seq=state.next_seq, ts=started + clock,
            speaker_account=speaker.account_id, role=speaker.slot,
            beat_id=directive.beat_id, text=text,
            kind="media" if directive.media else "line")
        director.advance_beat(ev)
        prev_len = len(text)

        result.lines.append(RehearsalLine(
            seq=ev.seq, at_seconds=round(clock, 1),
            account_id=speaker.account_id,
            display_name=speaker.display_name or speaker.account_id,
            persona_id=speaker.persona_id, role=speaker.slot,
            beat_id=directive.beat_id, text=text,
            soft_level=directive.soft_level, media=directive.media,
            kind=ev.kind))

        if store is not None:
            _safe(store.append_event, sid, ev)

    else:
        result.terminate_reason = result.terminate_reason or "guard_limit"

    state.status = "done"
    state.ended_at = started + clock
    result.duration_seconds = round(clock, 1)
    result.naturalness = naturalness_score(
        [e for e in state.events if e.kind in ("line", "media")])

    if store is not None:
        _safe(store.save_session, state)

    return result


# ── 渲染 ────────────────────────────────────────────────────────────────────


def format_rehearsal(result: RehearsalResult) -> str:
    """把排练结果渲染成人能读的剧本预览（CLI / 看板 / 交付物都用它）。"""
    out: List[str] = []
    out.append(f"═══ 排练：{result.playbook_id} @ {result.group_key} ═══")
    if result.casting is not None:
        cast_desc = "  ".join(
            f"{m.slot}={m.display_name or m.account_id}({m.persona_id})"
            for m in result.casting.members)
        out.append(f"演员表：{cast_desc or '（空）'}")
        if result.casting.unfilled:
            out.append(f"缺角：{'、'.join(result.casting.unfilled)}")
    for w in result.warnings:
        out.append(("⚠ " if w.startswith("warn:") else "✖ ") + w)
    out.append("")
    for ln in result.lines:
        mm, ss = divmod(int(ln.at_seconds), 60)
        who = ln.display_name or ln.account_id
        if ln.kind == "human":
            out.append(f"[{mm:02d}:{ss:02d}] 👤 {who}：{ln.text}")
            continue
        soft = f" soft{ln.soft_level}" if ln.soft_level else ""
        media = f" 📎{ln.media}" if ln.media else ""
        out.append(f"[{mm:02d}:{ss:02d}] {who}（{ln.role}{soft}{media}）：{ln.text}")
    out.append("")
    nat = result.naturalness or {}
    skipped = f"　⚠ 生成失败 {result.skipped} 拍" if result.skipped else ""
    out.append(
        f"收场：{result.terminate_reason}　"
        f"共 {result.line_count} 条　"
        f"时长 {int(result.duration_seconds // 60)} 分{skipped}")
    if nat:
        out.append(
            f"自然度：{nat.get('score', 0):.2f}（{nat.get('verdict', '?')}）"
            f"　熵 {nat.get('entropy', 0):.2f}"
            f"　间隔CV {nat.get('interval_cv', 0):.2f}"
            f"　均衡 {nat.get('balance', 0):.2f}")
        for issue in (nat.get("issues") or []):
            out.append(f"　· {issue}")
    return "\n".join(out)


# ── 内部 ────────────────────────────────────────────────────────────────────


def _human_cues(script: Optional[Sequence[Tuple[int, str, str]]]
                ) -> Dict[int, List[Tuple[str, str]]]:
    """``[(第几条我方发言之后, 谁, 说了什么)]`` → ``{位置: [(谁, 说了什么), ...]}``。

    同一位置保留**多条**（不是覆盖）：真人连珠炮式刷几句是真实高频场景，也是
    「真人接管、戏该退场」的判定输入，接口层不能把它压扁成一条。
    """
    cues: Dict[int, List[Tuple[str, str]]] = {}
    for item in script or []:
        try:
            after, who, said = item
            cues.setdefault(int(after), []).append(
                (str(who or "群友"), str(said or "")))
        except (TypeError, ValueError):
            continue
    return cues


def _is_awaitable(obj: Any) -> bool:
    return hasattr(obj, "__await__")


def _safe(fn: Callable[..., Any], *args: Any) -> None:
    """落库等副作用一律软失败——排练不能因为写库出错而白演一场。"""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        pass


# ── 人设发声（便利入口）──────────────────────────────────────────────────────


def persona_line_generator(ai_client: Any, **kwargs: Any) -> GenerateFn:
    """人设发声版生成器，即 ``voice.persona_generator``（详见该模块 docstring）。

    与 :func:`llm_generator` 的区别：台词过该号所绑人设 + ``persona_guard`` 出站校验。
    ``rehearse(generate=persona_line_generator(client))`` 即可换声，主循环无需感知。

    延迟 import：``voice`` 依赖本模块的 :func:`flatten_messages` 与
    :data:`GenerateFn`，写在顶部会成环。
    """
    from src.companion.group_show.voice import persona_generator
    return persona_generator(ai_client, **kwargs)
