"""导演循环 —— 群戏的决策中枢（纯决策，不做 I/O、不发消息）。

导演只回答五个问题，把「怎么发、发不发得出去」全留给 runtime 与编排器：

1. :meth:`GroupShowDirector.select_next_speaker` —— 这一拍谁说话？
2. :meth:`GroupShowDirector.next_directive` —— 给它什么**意图**（不是台词）？
3. :meth:`GroupShowDirector.should_yield_to_human` —— 真人在说话，要不要让路？
4. :meth:`GroupShowDirector.should_terminate` —— 这场戏该收了吗？
5. :meth:`GroupShowDirector.advance_beat` —— 记账并推进弧线。

这套五方法接口与 Microsoft Agent Framework 的 ``GroupChatManager``（
select_next_agent / should_request_user_input / should_terminate）同构——**刻意
对齐**：将来若要换成外部编排框架，只需替换本类实现，上层 runtime 不动。但我们自建
而不引入框架，是因为本仓的承重需求（反封号护栏、账号隔离、人设发声链）全在框架的
抽象之外，框架反而会挡在中间。

## 四处高于原始设计的决策（都是「像不像真人」的关键）

**① 拍队列而非游标**：剧本 beats 装进队列，导演可临场重排。真人群里几乎没人自己
接自己的话——若本拍角色恰是上一个发言者，导演会把队列里第一个不同角色的拍提前
（``_reorder_avoid_self_reply``）。游标模型做不到这件事。

**② 响应式选人覆盖**：真人插话且句子里带疑问/产品信号时，优先让 ``advocate``（懂
产品的）或 ``skeptic`` 接话，而不是机械念下一拍。播放器按顺序播，导演看场子。

**③ 软广衰减**：本场每提及一次产品，后续拍的软广强度自动降档
（``soft_decay_per_mention``）。「越聊越像广告」是群戏第二常见的翻车方式（第一是
台词模板化）。

**④ 真人接管＝最好的结局**：真人自己热聊起来后继续念稿是最蠢的行为。连续真人发言
达阈值即 ``terminate("human_takeover")``——这个 reason 在看板上应该是绿色成就，
不是失败。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.companion.group_show.playbook import (
    DEFAULT_MUST_NOT,
    Beat,
    BeatDirective,
    CastMember,
    ShowEvent,
    ShowState,
)

# 角色顶替优先级：核心槽缺位时用谁顶（种草 > 抛问 > 质疑 > 附和）
_SUBSTITUTE_ORDER: Tuple[str, ...] = ("advocate", "asker", "skeptic", "bystander")

# 真人插话里的「疑问 / 产品关切」信号——命中则响应式选人接管本拍
_HUMAN_QUESTION_RE = re.compile(
    r"[?？]|怎么|如何|多少|能不能|可不可以|是不是|有没有|真的吗|靠谱|"
    r"贵不贵|价格|多久|安全|封号|试用|哪里|什么"
)


@dataclass(frozen=True)
class DirectorConfig:
    """导演行为参数（全部可经配置覆盖，给的都是保守缺省）。"""

    #: 真人发言后多少秒内不抢话（让路窗）
    human_yield_seconds: float = 45.0
    #: 连续多少条真人发言即判定「真人接管」，优雅退场
    human_takeover_lines: int = 3
    #: 单场最多发言条数（预算闸，防失控刷屏）
    max_lines: int = 40
    #: 单号占本场发言比例上限（超过则本拍换人，防一个号刷屏）
    max_share_per_account: float = 0.45
    #: 每提及一次产品，后续软广强度衰减多少
    soft_decay_per_mention: int = 1
    #: 软广强度地板（衰减不会低于此值）
    soft_floor: int = 1
    #: 单场最长时长（秒），超时收尾
    max_duration_seconds: float = 3600.0

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> "DirectorConfig":
        """从配置 dict 解析（缺键/脏值一律回落缺省，绝不抛）。"""
        d = cfg or {}

        def _f(key: str, default: float) -> float:
            try:
                return float(d.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(d.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            human_yield_seconds=_f("human_yield_seconds", 45.0),
            human_takeover_lines=_i("human_takeover_lines", 3),
            max_lines=_i("max_lines", 40),
            max_share_per_account=_f("max_share_per_account", 0.45),
            soft_decay_per_mention=_i("soft_decay_per_mention", 1),
            soft_floor=_i("soft_floor", 1),
            max_duration_seconds=_f("max_duration_seconds", 3600.0),
        )


class GroupShowDirector:
    """一场戏的导演。**有状态**（持有 :class:`ShowState`），但不做任何 I/O。"""

    def __init__(self, state: ShowState,
                 config: Optional[Dict[str, Any]] = None) -> None:
        self.state = state
        self.cfg = DirectorConfig.from_config(config)
        # 待演拍队列（可被导演临场重排；已演的出队）
        self._queue: List[Beat] = list(state.playbook.beats[state.beat_cursor:])
        # 待回应的真人原话（让路窗结束后由下一拍真实回应，而不是继续念稿）
        self._pending_human: str = ""
        self._pending_human_name: str = ""
        # 连续真人发言计数（我们的号一发言就清零）
        self._human_streak: int = 0

    # ── 观测输入 ─────────────────────────────────────────────────────────

    def observe_human(self, text: str, *, sender: str = "",
                      ts: Optional[float] = None) -> None:
        """真人在群里说话了。

        导演据此让路、记待回应原话、累计接管计数。真人一开口就把「继续念稿」的
        惯性打断——这是群戏与广告机器人最直观的区别。
        """
        now = float(ts if ts is not None else time.time())
        self.state.last_human_ts = now
        self._human_streak += 1
        t = str(text or "").strip()
        if t:
            self._pending_human = t
            self._pending_human_name = str(sender or "")
        self.state.append_event(ShowEvent(
            seq=self.state.next_seq, ts=now,
            speaker_account=str(sender or "human"), role="human",
            beat_id="", text=t, kind="human"))

    # ── 五方法接口 ───────────────────────────────────────────────────────

    def should_yield_to_human(self, *, now: Optional[float] = None) -> bool:
        """真人刚说完话的让路窗内不抢话。

        真人在群里发言后，机器人秒回是最刺眼的机械特征（何况可能人家在跟别人说
        话）。窗内返回 True＝本 tick 什么都不做；窗过后下一拍会带上
        ``respond_to_human`` 真实回应那句话，而不是装作没看见继续念稿。
        """
        if self.state.last_human_ts <= 0:
            return False
        t = float(now if now is not None else time.time())
        return (t - self.state.last_human_ts) < self.cfg.human_yield_seconds

    def should_terminate(self, *, now: Optional[float] = None
                         ) -> Tuple[bool, str]:
        """该收场了吗？返回 ``(是否终止, 原因)``。

        原因取值：``completed``（演完）/ ``human_takeover``（真人自己聊起来了＝
        **最好的结局**）/ ``budget``（条数预算）/ ``timeout`` / ``aborted``。
        """
        if self.state.status == "aborted":
            return True, "aborted"
        # 真人接管：连续真人发言达阈值——戏的目的就是把场子点着，点着了就该退场
        if self._human_streak >= self.cfg.human_takeover_lines:
            return True, "human_takeover"
        spoken = sum(1 for e in self.state.events if e.kind in ("line", "media"))
        if spoken >= self.cfg.max_lines:
            return True, "budget"
        t = float(now if now is not None else time.time())
        if (self.state.started_at > 0
                and t - self.state.started_at > self.cfg.max_duration_seconds):
            return True, "timeout"
        if not self._queue:
            return True, "completed"
        return False, ""

    def select_next_speaker(self) -> Optional[CastMember]:
        """这一拍谁说话？返回 None＝没人能上（选角全空/全被刷屏闸挡下）。

        三层决策，从强到弱：
        ① **响应式覆盖**：有待回应的真人提问 → 让 advocate/skeptic 接（懂产品的
           人回答问题才自然，让 bystander 去答技术疑问是穿帮点）；
        ② **自我接话规避**：本拍角色恰是上一个发言者 → 队列里第一个不同角色的拍
           提前（真人群里几乎没人自己接自己）；
        ③ **刷屏闸**：某号占比超 ``max_share_per_account`` → 本拍换人。
        """
        if not self._queue:
            return None
        cast = self.state.casting
        if not cast.members:
            return None

        # ① 响应式覆盖
        responder = self._responsive_pick()
        if responder is not None:
            return responder

        # ② 自我接话规避（重排队列，不丢拍）
        self._reorder_avoid_self_reply()
        beat = self._queue[0]
        member = cast.by_slot(beat.role) or self._substitute_for(beat.role)
        if member is None:
            return None

        # ③ 刷屏闸
        if self._over_share(member.account_id):
            alt = self._least_spoken_other(member.account_id)
            if alt is not None:
                return alt
        return member

    def next_directive(self) -> Optional[BeatDirective]:
        """给本拍发言者的意图指令（**没有台词字段**——台词由人设 LLM 现场生成）。"""
        if not self._queue:
            return None
        beat = self._queue[0]
        soft = self._effective_soft(beat)
        # 第一拍无从承接；有真人待回应时也不该"顺着上一条"而应回应真人
        reference_last = bool(self.state.events) and not self._pending_human
        human_line = ""
        if self._pending_human:
            who = self._pending_human_name or "群友"
            human_line = f"{who}：{self._pending_human}"
        return BeatDirective(
            beat_id=beat.id,
            intent=beat.intent,
            product=beat.product,
            media=beat.media,
            soft_level=soft,
            must_not=DEFAULT_MUST_NOT,
            reference_last=reference_last,
            respond_to_human=human_line,
        )

    def advance_beat(self, event: ShowEvent) -> None:
        """本拍已发出：记账、清真人待回应、出队推进。"""
        self.state.append_event(event)
        if event.kind in ("line", "media"):
            self._human_streak = 0
            self._pending_human = ""
            self._pending_human_name = ""
        if self._queue and (not event.beat_id
                            or event.beat_id == self._queue[0].id):
            self._queue.pop(0)
        elif self._queue and event.beat_id:
            # 响应式覆盖可能消费了队列中间的拍，按 id 精确出队
            self._queue = [b for b in self._queue if b.id != event.beat_id]
        self.state.beat_cursor = (
            len(self.state.playbook.beats) - len(self._queue))
        if self.state.status == "pending":
            self.state.status = "running"

    # ── 内部决策 ─────────────────────────────────────────────────────────

    def _last_speaker_account(self) -> str:
        """**我们的号**里最后一个发言的 account_id（没有则空串）。

        必须回溯而不能只看 ``last_event()``：真人插话会追加一条 ``kind="human"``
        的事件顶在最后，只看末条时「上一个发言者」恒为空，两条防自问自答的护栏
        （抢答规避、自我接话规避）就都变成死代码——而真人刚说完话，**恰恰是这两条
        护栏最该生效的时刻**（同一个号在真人一句话前后连发两条，是最典型的机器痕迹）。
        """
        for ev in reversed(self.state.events):
            if ev.kind in ("line", "media"):
                return str(ev.speaker_account or "")
        return ""

    def _responsive_pick(self) -> Optional[CastMember]:
        """真人提问时的响应式选人：让最合适的人回答，而不是念下一拍。"""
        if not self._pending_human:
            return None
        if not _HUMAN_QUESTION_RE.search(self._pending_human):
            return None
        cast = self.state.casting
        last_acct = self._last_speaker_account()
        for slot in ("advocate", "skeptic", "bystander", "asker"):
            m = cast.by_slot(slot)
            # 同一个号连着回两条＝抢答，宁可换个人接
            if m is not None and m.account_id != last_acct:
                return m
        # 找不到「不是刚发言那个号」的人（例如单号演出）→ 不做响应式覆盖，
        # 回落正常拍流程。那条路上还有刷屏闸兜着，绕过去反而会让单号连发。
        return None

    def _reorder_avoid_self_reply(self) -> None:
        """队头角色＝上一个发言者时，把第一个不同角色的拍提前。"""
        if len(self._queue) < 2:
            return
        last_acct = self._last_speaker_account()
        if not last_acct:
            return
        head = self.state.casting.by_slot(self._queue[0].role)
        if head is None or head.account_id != last_acct:
            return
        for i in range(1, len(self._queue)):
            cand = self.state.casting.by_slot(self._queue[i].role)
            if cand is not None and cand.account_id != last_acct:
                self._queue.insert(0, self._queue.pop(i))
                return

    def _substitute_for(self, slot: str) -> Optional[CastMember]:
        """角色槽没配上人时找替补（号不够时的降级演出）。"""
        cast = self.state.casting
        for s in _SUBSTITUTE_ORDER:
            if s == slot:
                continue
            m = cast.by_slot(s)
            if m is not None:
                return m
        return cast.members[0] if cast.members else None

    def _over_share(self, account_id: str) -> bool:
        spoken = sum(1 for e in self.state.events if e.kind in ("line", "media"))
        if spoken < 4:  # 样本太少谈占比没意义
            return False
        share = self.state.lines_by_account(account_id) / float(spoken)
        return share > self.cfg.max_share_per_account

    def _least_spoken_other(self, exclude: str) -> Optional[CastMember]:
        cands = [m for m in self.state.casting.members
                 if m.account_id != exclude]
        if not cands:
            return None
        return min(cands, key=lambda m: self.state.lines_by_account(m.account_id))

    def _effective_soft(self, beat: Beat) -> int:
        """软广衰减：本场每提一次产品，后续强度降档（防越聊越像广告）。

        ``soft == 0`` 的拍**原样透传，不吃地板**：剧本写 0 是「这拍纯闲聊、一个字
        产品都不许提」的硬意图（所有模板的开场拍都是 0——一上来就推＝广告）。
        地板的本意是「别把种草拍衰减成哑巴」，把明写 0 的拍抬到 1 是越权。
        2026-07-25 首场真 LLM 排练实测发现：开场拍被标成 soft1。
        """
        base = beat.effective_soft(self.state.playbook.soft_ad_level)
        if base <= 0:
            return 0
        mentions = self._product_mentions()
        decayed = base - mentions * max(0, self.cfg.soft_decay_per_mention)
        return max(self.cfg.soft_floor, decayed)

    def _product_mentions(self) -> int:
        """本场已发生的「带产品的拍」数（按 beat_id 回查剧本）。"""
        by_id = {b.id: b for b in self.state.playbook.beats}
        n = 0
        for e in self.state.events:
            if e.kind not in ("line", "media"):
                continue
            b = by_id.get(e.beat_id)
            if b is not None and b.product:
                n += 1
        return n
