# -*- coding: utf-8 -*-
"""影子模式 —— 接真实群的真实消息流跑导演循环，但**一条消息都不发**。

## 为什么排练不够、真发又太早

Phase 1 的离线排练（``runtime.rehearse``）是全虚拟的：虚拟时钟、剧本里写死的真人
插话、凭空捏的候选池。它能验编排结构，验不了**这个群**——真人几点活跃、话题密度
多大、我们的让路窗在真实节奏下是太长还是太短、剧本的开场拍会不会撞上群里正热的
另一个话题。这些只有把真实消息流灌进导演循环才知道。

影子模式就是这一环：**真输入 + 真节奏 + 零输出**。导演照常选人、下意图、生成台词、
记账推进弧线，唯独最后一步「发出去」换成「记下来」。跑一天下来，运营看到的是一份
「如果当时开了真发，这个群里会多出这些话」的逐条清单——用零封号风险的代价，换到
真发前唯一能拿到的证据。

## 为什么「沉默」也要记录

一个只记录「本该说什么」的影子模式会**系统性地骗人**：它让人以为戏很活跃，而真相
可能是「这一小时里导演 12 次想开口、11 次因为让路窗而闭嘴」。让路、终止、选不出人
这些**不说话的决策**，恰恰是影子模式最该交付的信息——

- 抑制率太高 ＝ 让路窗设太长 / 群太吵，真发上去也插不进话；
- 抑制率为零 ＝ 让路逻辑根本没被触发，真发时的「不抢话」承诺没有被验证过；
- ``terminate`` 抑制 ＝ 真人自己聊起来了，这在群戏里是**成就**不是失败。

所以 :class:`ShadowDecision` 用 ``suppressed`` 字段把两类决策放在同一条时间线上，
而不是把沉默默默丢掉。

## 架构级安全铁律

本模块**根本不 import 任何发送出口**——物理上不可能把消息发出去，不依赖任何人
记得把开关关上。这条由 ``tests/test_group_show_shadow.py`` 的源码扫描钉死，
和排练模块的那条钉子同源同精神。真发链路（Phase 3）是另一个模块的事。

## 时间源注入

``clock`` 参数让调用方决定「现在几点」：生产传缺省（``time.time``），测试传一个
可控函数，于是「让路窗过了没」「下一拍到点没」这类**纯时间条件**可以被瞬间验证，
不需要在测试里 sleep 真实秒数。
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

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
)
from src.companion.group_show.runtime import (
    GenerateFn,
    group_hint_text,
    stub_generator,
)

logger = logging.getLogger(__name__)

#: 连续多少拍生成不出台词就判定「生成侧挂了」并停演。与排练同口径：影子模式一跑
#: 就是几小时，静默空转比显式失败更坏——人会以为「这个群没机会说话」，其实是
#: LLM 早就挂了。
MAX_CONSECUTIVE_SKIPS = 3

#: ``suppressed`` 的取值前缀（冒号后可带细分原因，如 ``terminate:human_takeover``）
SUPPRESS_YIELD = "yield"
SUPPRESS_TERMINATE = "terminate"
SUPPRESS_NO_SPEAKER = "no_speaker"

#: ``reason`` 的取值：为什么此刻**该**说话（与说没说得成无关）
REASON_BEAT = "beat"
REASON_RESPOND_HUMAN = "respond_human"


@dataclass(frozen=True)
class ShadowDecision:
    """影子模式的一条决策 ＝「此刻本该由谁说什么」。

    ``suppressed`` 为空 ＝ 真发时这条会被发出去（``text`` 是真生成的台词）；
    非空 ＝ 本可发但被抑制了，值是抑制原因，此时 ``text`` 为空（**刻意不生成**——
    既然不发，就不该为一次沉默烧一次 LLM；决策里保留的是「本该轮到谁、哪一拍」）。
    """

    at: float
    account_id: str
    display_name: str
    role: str
    beat_id: str
    text: str
    soft_level: int
    media: str
    reason: str
    suppressed: str = ""

    @property
    def spoken(self) -> bool:
        """真发时这条会不会被发出去。"""
        return not self.suppressed

    @property
    def suppress_kind(self) -> str:
        """抑制原因的大类（``terminate:human_takeover`` → ``terminate``）。"""
        return self.suppressed.split(":", 1)[0] if self.suppressed else ""


class ShadowRunner:
    """一个群的影子模式跑批器。**有状态**（一场戏一个实例），**永不发送**。

    典型用法（CLI / worker）::

        runner = ShadowRunner(playbook, casting, group_key="tg:-100123")
        # 群里来真人消息
        await runner.on_inbound(sender_id="u1", sender_name="老王",
                                text="这个靠谱吗", ts=time.time())
        # 群里没人说话时定时推进
        await runner.tick()
        print(runner.report())

    构造参数
    --------
    playbook / casting:
        剧本与**已选好的**演员表（选角是 :mod:`casting` 的事，本类不重复做——
        影子模式常常要复用「真发时会用的那批号」，由调用方决定更合适）。
    group_key / platform:
        只用于记录与落库归属，本类不会拿它去解析任何发送通道。
    config:
        透传给 :class:`GroupShowDirector` 的导演参数（让路窗、接管阈值…）。
    generate:
        台词生成器（同步/异步均可），缺省用占位生成器——**看编排结构时不必烧 LLM**。
    store:
        :class:`GroupShowStore` 或 None。落库全程软失败：影子模式跑几小时，
        绝不能因为写库出错丢掉整条观察记录之外的东西。
    clock:
        时间源，缺省 ``time.time``。见模块 docstring。
    """

    def __init__(
        self,
        playbook: Playbook,
        casting: Casting,
        *,
        group_key: str,
        platform: str = "telegram",
        config: Optional[Dict[str, Any]] = None,
        generate: Optional[GenerateFn] = None,
        store: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.time
        self._gen: GenerateFn = generate or stub_generator()
        self._store = store
        self._rng = random.Random()

        started = self._now()
        self.session_id = f"shadow_{uuid.uuid4().hex[:12]}"
        self._state = ShowState(
            session_id=self.session_id, group_key=str(group_key),
            playbook=playbook, casting=casting, platform=str(platform),
            dry_run=True, started_at=started, status="pending")
        self._director = GroupShowDirector(self._state, config)

        #: account_id → 显示名（演员表 + 群里见过的真人），供 ECP 投影解析
        self._names: Dict[str, str] = {
            m.account_id: (m.display_name or m.persona_id or m.account_id)
            for m in (casting.members or ())
        }
        self._hint: Optional[str] = None

        self.decisions: List[ShadowDecision] = []
        self._spoken = 0
        self._suppressed_counts: Dict[str, int] = {}
        self._human_count = 0
        self._skipped = 0
        self._consecutive_skips = 0
        self._prev_text_len = 0
        #: 下一拍最早可发时刻——影子模式跑在真实时间上，节奏必须真等（否则记录出来的
        #: 时刻表是假的，自然度的间隔轴也就没有意义）
        self._next_due_at = started
        self._last_at = started
        self._terminate_reason = ""
        #: 已就同一次真人发言报过让路 —— 让路窗内每个 tick 都记一条会把报表刷爆，
        #: 一次沉默只该记一次
        self._yield_reported_ts = -1.0
        self._terminate_reported = False

    # ── 只读态 ───────────────────────────────────────────────────────────

    @property
    def terminated(self) -> bool:
        """这场戏是否已经收场（调用方据此退出轮询循环）。"""
        return bool(self._terminate_reason)

    @property
    def terminate_reason(self) -> str:
        return self._terminate_reason

    @property
    def state(self) -> ShowState:
        """运行时状态（只读用途：看板/回放；改它等于绕过导演）。"""
        return self._state

    # ── 输入 ─────────────────────────────────────────────────────────────

    async def on_inbound(
        self,
        *,
        sender_id: str,
        sender_name: str = "",
        text: str = "",
        ts: Optional[float] = None,
    ) -> Optional[ShadowDecision]:
        """群里来了一条真实消息 → 喂给导演 → 判断此刻该不该接话。

        返回值三态：真决策（真发时这条会被发出去）/ 带 ``suppressed`` 的决策
        （本可说但选择了沉默）/ ``None``（这一刻没什么可记的）。

        **正常情况下这里几乎总是返回 yield 抑制**：真人刚说完话，导演的让路窗
        就是不许秒回。真正的回应发生在窗口过后的某次 :meth:`tick`，那时
        directive 会带上 ``respond_to_human``。这不是缺陷，是「不抢话」这条
        拟人不变量在影子模式下的可见证据。
        """
        now = self._resolve_ts(ts)
        who = str(sender_id or "").strip()
        name = str(sender_name or "").strip()
        # 群里的身份以**显示名**为准：ECP 投影和「先回应真人」指令里出现的都是它，
        # 塞个 account_id 进去会让台词变成「回应 u10086：…」。拿不到名字才回落 id。
        label = name or who or "群友"
        self._names[label] = label
        if who:
            self._names[who] = label

        self._human_count += 1
        try:
            self._director.observe_human(str(text or ""), sender=label, ts=now)
        except Exception:  # noqa: BLE001 —— 观测输入炸了也不能中断影子跑批
            logger.debug("[group_show.shadow] observe_human 失败（已忽略）",
                         exc_info=True)
        else:
            last = self._state.last_event()
            if last is not None:
                self._persist_event(last)

        return await self._decide(now, human_triggered=True)

    async def tick(self, *, now: Optional[float] = None) -> Optional[ShadowDecision]:
        """定时驱动 —— 群里没人说话时，戏也要能自己往下走。

        大多数 tick 返回 ``None``（还没到下一拍的点），这是对的：真人不会每 10 秒
        说一句。到点了才产出决策，节奏由 :mod:`pacing` 按剧本档位算，与真发一致。
        """
        return await self._decide(self._resolve_ts(now), human_triggered=False)

    # ── 汇报 ─────────────────────────────────────────────────────────────

    def report(self) -> Dict[str, Any]:
        """一次影子跑批的完整读数（可直接打印、落盘或喂看板）。

        ``would_send`` 是「真发时会多出多少条」，``suppressed`` 是「我们主动闭嘴
        多少次」——两个数一起看才知道这个群到底给不给戏留缝隙。
        """
        lines = [e for e in self._state.events
                 if e.kind in ("line", "media")]
        try:
            nat = naturalness_score(lines)
        except Exception:  # noqa: BLE001 —— 评测器炸了也要给出报表
            logger.debug("[group_show.shadow] 自然度评测失败（已忽略）", exc_info=True)
            nat = {}

        speakers: Dict[str, int] = {}
        for d in self.decisions:
            if d.spoken and d.account_id:
                speakers[d.account_id] = speakers.get(d.account_id, 0) + 1

        self._persist_session()
        return {
            "session_id": self.session_id,
            "playbook_id": self._state.playbook.id,
            "group_key": self._state.group_key,
            "platform": self._state.platform,
            "dry_run": True,
            "started_at": self._state.started_at,
            "last_at": self._last_at,
            "duration_seconds": round(
                max(0.0, self._last_at - self._state.started_at), 1),
            "decisions": len(self.decisions),
            "would_send": self._spoken,
            "suppressed": sum(self._suppressed_counts.values()),
            "suppressed_by_reason": dict(self._suppressed_counts),
            "human_messages": self._human_count,
            "skipped": self._skipped,
            "terminate_reason": self._terminate_reason,
            "beats_total": len(self._state.playbook.beats),
            "beats_played": self._state.beat_cursor,
            "speakers": speakers,
            "naturalness": nat,
        }

    # ── 决策主循环 ───────────────────────────────────────────────────────

    async def _decide(
        self, now: float, *, human_triggered: bool,
    ) -> Optional[ShadowDecision]:
        """导演循环的一次求值。**唯一出口是一个 dataclass，不是一条消息。**"""
        self._last_at = max(self._last_at, now)

        # ① 收场判定优先：戏都该收了就别再排下一拍（真人接管属于此列，是好结局）
        stop, reason = self._safe_terminate(now)
        if stop:
            self._terminate_reason = reason or "completed"
            self._state.status = "done"
            self._state.ended_at = now
            if self._terminate_reported:
                return None
            self._terminate_reported = True
            return self._suppress(
                now, f"{SUPPRESS_TERMINATE}:{self._terminate_reason}")

        # ② 让路窗：真人刚开口，闭嘴——但要把这次闭嘴记下来（见模块 docstring）
        if self._safe_yield(now):
            human_ts = float(self._state.last_human_ts)
            if human_ts == self._yield_reported_ts and not human_triggered:
                return None
            self._yield_reported_ts = human_ts
            return self._suppress(now, SUPPRESS_YIELD)

        # ③ 节奏闸：真人插话是事件驱动（窗过即答），自发推进则必须等到点
        if not human_triggered and now < self._next_due_at:
            return None

        speaker, directive = self._peek()
        if speaker is None or directive is None:
            return self._suppress(now, SUPPRESS_NO_SPEAKER)

        text = await self._generate(speaker, directive)
        if not text:
            self._on_generation_miss(now, speaker, directive)
            return None
        self._consecutive_skips = 0

        return self._commit(now, speaker, directive, text)

    def _commit(
        self, now: float, speaker: CastMember, directive: BeatDirective,
        text: str,
    ) -> ShadowDecision:
        """记账：推进弧线、排下一拍的点、落库、产出决策。**没有发送这一步。**"""
        ev = ShowEvent(
            seq=self._state.next_seq, ts=now,
            speaker_account=speaker.account_id, role=speaker.slot,
            beat_id=directive.beat_id, text=text,
            kind="media" if directive.media else "line")
        beat = self._state.playbook.beat_at(self._state.beat_cursor) \
            or self._state.current_beat
        try:
            self._director.advance_beat(ev)
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] advance_beat 失败（已忽略）",
                         exc_info=True)

        interval = self._interval(beat, len(text))
        self._next_due_at = now + interval
        self._prev_text_len = len(text)
        self._persist_event(ev)

        decision = ShadowDecision(
            at=now, account_id=speaker.account_id,
            display_name=speaker.display_name or speaker.account_id,
            role=speaker.slot, beat_id=directive.beat_id, text=text,
            soft_level=int(directive.soft_level),
            media=str(directive.media or ""),
            reason=self._reason_for(directive))
        self._spoken += 1
        self.decisions.append(decision)
        return decision

    def _suppress(self, now: float, why: str) -> ShadowDecision:
        """产出一条「本可说但没说」的决策。**不生成台词**（不发就不烧 LLM）。"""
        speaker, directive = self._peek()
        decision = ShadowDecision(
            at=now,
            account_id=speaker.account_id if speaker is not None else "",
            display_name=(speaker.display_name or speaker.account_id)
            if speaker is not None else "",
            role=speaker.slot if speaker is not None else "",
            beat_id=directive.beat_id if directive is not None else "",
            text="",
            soft_level=int(directive.soft_level) if directive is not None else 0,
            media=str(directive.media or "") if directive is not None else "",
            reason=self._reason_for(directive),
            suppressed=why)
        kind = decision.suppress_kind
        self._suppressed_counts[kind] = self._suppressed_counts.get(kind, 0) + 1
        self.decisions.append(decision)
        return decision

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:  # noqa: BLE001 —— 注入的时钟坏了就回落真实时间
            logger.debug("[group_show.shadow] clock 调用失败，回落 time.time",
                         exc_info=True)
            return time.time()

    def _resolve_ts(self, ts: Optional[float]) -> float:
        if ts is None:
            return self._now()
        try:
            return float(ts)
        except (TypeError, ValueError):
            return self._now()

    def _safe_terminate(self, now: float) -> Tuple[bool, str]:
        if self._terminate_reason:
            return True, self._terminate_reason
        try:
            return self._director.should_terminate(now=now)
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] should_terminate 失败（按不终止）",
                         exc_info=True)
            return False, ""

    def _safe_yield(self, now: float) -> bool:
        try:
            return bool(self._director.should_yield_to_human(now=now))
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] should_yield 失败（按不让路）",
                         exc_info=True)
            return False

    def _peek(self) -> Tuple[Optional[CastMember], Optional[BeatDirective]]:
        """问导演「此刻该谁、说什么意图」——只读，不推进弧线。"""
        speaker: Optional[CastMember] = None
        directive: Optional[BeatDirective] = None
        try:
            speaker = self._director.select_next_speaker()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] select_next_speaker 失败", exc_info=True)
        try:
            directive = self._director.next_directive()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] next_directive 失败", exc_info=True)
        return speaker, directive

    @staticmethod
    def _reason_for(directive: Optional[BeatDirective]) -> str:
        if directive is not None and str(
                getattr(directive, "respond_to_human", "") or "").strip():
            return REASON_RESPOND_HUMAN
        return REASON_BEAT

    async def _generate(
        self, speaker: CastMember, directive: BeatDirective,
    ) -> str:
        """跑一次台词生成（ECP 投影 → 生成器）。失败一律返空，绝不上抛。"""
        if self._hint is None:
            try:
                self._hint = group_hint_text()
            except Exception:  # noqa: BLE001
                self._hint = ""
        try:
            messages = project_history_for(
                speaker, self._state.events, directive=directive,
                group_hint=self._hint or "",
                name_resolver=lambda a: self._names.get(a, a))
            out = self._gen(speaker, messages, directive)
            if hasattr(out, "__await__"):
                out = await out
            return str(out or "").strip()
        except Exception as exc:  # noqa: BLE001 —— 单拍生成失败不该掀翻整场观察
            logger.warning("[group_show.shadow] 台词生成失败 beat=%s: %s",
                           getattr(directive, "beat_id", ""), exc)
            return ""

    def _on_generation_miss(
        self, now: float, speaker: CastMember, directive: BeatDirective,
    ) -> None:
        """生成不出台词：记一拍 skip 推进弧线；连着失败就判生成侧挂了并收场。"""
        self._skipped += 1
        self._consecutive_skips += 1
        try:
            self._director.advance_beat(ShowEvent(
                seq=self._state.next_seq, ts=now,
                speaker_account=speaker.account_id, role=speaker.slot,
                beat_id=directive.beat_id, text="", kind="skip"))
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] skip 推进失败（已忽略）", exc_info=True)
        self._next_due_at = now + self._interval(
            self._state.current_beat, 0)
        if self._consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
            self._terminate_reason = "generation_failed"
            self._state.status = "aborted"
            self._state.ended_at = now
            logger.warning(
                "[group_show.shadow] 连续 %d 拍生成不出台词，判定生成侧异常，停止观察",
                self._consecutive_skips)

    def _interval(self, beat: Any, next_len: int) -> float:
        try:
            return float(beat_interval_seconds(
                beat, prev_text_len=self._prev_text_len,
                next_text_len=next_len, rng=self._rng))
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] 节奏计算失败，回落 60s", exc_info=True)
            return 60.0

    def _persist_event(self, ev: Any) -> None:
        if self._store is None:
            return
        try:
            self._store.append_event(self.session_id, ev)
        except Exception:  # noqa: BLE001 —— 落库是旁路能力，坏了不影响观察
            logger.debug("[group_show.shadow] append_event 失败（已忽略）",
                         exc_info=True)

    def _persist_session(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save_session(self._state)
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.shadow] save_session 失败（已忽略）",
                         exc_info=True)


# ── 渲染（CLI / 看板共用，别在调用方各写一份） ────────────────────────────────


def format_decision(decision: ShadowDecision) -> str:
    """一条决策渲染成人能读的一行。抑制的那些**必须一眼可辨**。"""
    stamp = time.strftime("%H:%M:%S", time.localtime(decision.at))
    who = decision.display_name or decision.account_id or "（无人）"
    soft = f" soft{decision.soft_level}" if decision.soft_level else ""
    media = f" 📎{decision.media}" if decision.media else ""
    if decision.suppressed:
        beat = f" {decision.beat_id}" if decision.beat_id else ""
        return (f"[{stamp}] 🤫 [抑制:{decision.suppressed}] "
                f"本该 {who}（{decision.role}{soft}{beat}）"
                f"因「{decision.reason}」开口")
    return (f"[{stamp}] {who}（{decision.role}{soft}{media}"
            f"·{decision.reason}）：{decision.text}")


def format_report(report: Dict[str, Any]) -> str:
    """收尾读数渲染。把「说了多少」和「忍了多少」并排放，两个数要一起看。"""
    rep = report or {}
    out: List[str] = []
    out.append(f"═══ 影子模式收尾：{rep.get('playbook_id', '?')} @ "
               f"{rep.get('group_key', '?')} ═══")
    mins = int(float(rep.get("duration_seconds") or 0) // 60)
    out.append(
        f"观察时长 {mins} 分　真人消息 {rep.get('human_messages', 0)} 条　"
        f"收场：{rep.get('terminate_reason') or '（仍在观察）'}")
    out.append(
        f"本该发出 {rep.get('would_send', 0)} 条　"
        f"主动沉默 {rep.get('suppressed', 0)} 次　"
        f"进度 {rep.get('beats_played', 0)}/{rep.get('beats_total', 0)} 拍"
        + (f"　⚠ 生成失败 {rep.get('skipped')} 拍" if rep.get("skipped") else ""))
    by_reason = rep.get("suppressed_by_reason") or {}
    if by_reason:
        out.append("沉默原因：" + "　".join(
            f"{k}×{v}" for k, v in sorted(by_reason.items())))
    speakers = rep.get("speakers") or {}
    if speakers:
        out.append("发言分布：" + "　".join(
            f"{k}×{v}" for k, v in sorted(speakers.items())))
    nat = rep.get("naturalness") or {}
    if nat.get("ok"):
        out.append(
            f"自然度：{nat.get('score', 0):.2f}（{nat.get('verdict', '?')}）"
            f"　熵 {nat.get('entropy', 0):.2f}"
            f"　间隔CV {nat.get('interval_cv', 0):.2f}"
            f"　均衡 {nat.get('balance', 0):.2f}")
        for issue in (nat.get("issues") or []):
            out.append(f"　· {issue}")
    elif nat:
        out.append(f"自然度：样本不足（{nat.get('samples', 0)} 条），不予打分")
    return "\n".join(out)


__all__ = [
    "MAX_CONSECUTIVE_SKIPS",
    "REASON_BEAT",
    "REASON_RESPOND_HUMAN",
    "SUPPRESS_NO_SPEAKER",
    "SUPPRESS_TERMINATE",
    "SUPPRESS_YIELD",
    "ShadowDecision",
    "ShadowRunner",
    "format_decision",
    "format_report",
]
