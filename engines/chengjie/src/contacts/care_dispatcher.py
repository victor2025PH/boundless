"""Phase O3：主动关怀到期派发器。

读 `CareScheduleStore.list_due(now)` → 对每条拉上下文 + topic 构造 LLM prompt（强制引用
具体事，无上下文就 skip 不发空话）→ 经**注入的 send_callback（reactivation 同款 deferred
队列）**发送（自动享 gate / pacing / quiet_hours / kill-switch / staleness）→ 成功 mark_sent。

设计（与 reactivation_loop 同范式、可注入、可单测）：
- 不做平台身份查找——`care_schedule` 入库时已存 platform/account_id/chat_key，直接用。
- O3 改进①：派发前可选 `already_discussed(contact_key, topic)` 复查，近期已聊过该事 → skip
  （`mark_skipped`，防"机器到点打卡"）。
- O3 改进②：发送时刻命中 quiet_hours → **顺延到安静时段结束**（而非跳过，关怀该送只是择时）。
- dry_run：只生成 + log，不真 enqueue、不 mark_sent（灰度第一阶段看 LLM 质量）。

默认关：上层 `companion.proactive_care.enabled` 控；本类只是机制，不自启。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.contacts.care_schedule import (
    CRISIS_CARE_TOPIC,
    GOAL_CARE_NORM_PREFIX,
    care_verbatim_text,
    is_verbatim_care,
    CareScheduleStore,
)

logger = logging.getLogger(__name__)

# send_callback：(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra) -> row_id
SendCallback = Callable[[str, str, str, str, float, str, float, dict], Awaitable[int]]
# context_provider：(contact_key) -> 最近对话/episodic 要点文本（供 prompt 引用）
ContextProvider = Callable[[str], str]
# already_discussed：(contact_key, topic) -> bool（近期是否已主动聊过该事）
AlreadyDiscussed = Callable[[str, str], bool]
# proactive_allowed：(contact_key) -> bool（变现配额门控；False=免费用户超额不主动）
ProactiveAllowed = Callable[[str], bool]
# cfg_provider：() -> 实时 companion.proactive_care 配置 dict（P0 2026-08-01 配置热闸）。
# 注入后 enabled/dry_run/max_per_tick 每 tick 从这里读——overlay 热重载即生效，无需重启；
# enabled 缺省按 False（与配置 schema 默认一致）→ 关闸时 run_once 空转零副作用。
CfgProvider = Callable[[], dict]
# budget_gate：(contact_key) -> bool（P3 每联系人主动预算；False=今日已被摸够/间隔太近，
# 本条跳过 note=contact_budget）。只在**真发**路径生效（dry_run 不拦——样本要流动），
# 危机关怀豁免（伦理优先），gate 异常按放行（预算 fail-open，不因账本抖动漏发关怀）。
BudgetGate = Callable[[str], bool]
# sent_hook：(item dict) -> None（真发 enqueue 成功后回调；background 侧用它把 care
# 触达落 outreach_log 共享账本，对周报/其它预算消费方可见）。异常绝不影响已完成的发送。
SentHook = Callable[[dict], None]
# peer_filter：(platform, account_id, chat_key) -> bool（True=对端是 bot/自家账号，
# 该跳过不派发）。bot 不该收任何主动消息——**危机关怀也不豁免**（给 @SpamBot 发
# 「我一直都在」比日常寒暄更荒谬）。best-effort，异常按放行（增量护栏 fail-open）。
PeerFilter = Callable[[str, str, str], bool]
# prompt_extras_provider：(item dict) -> {"persona_line","memory_block","goal_block"}
# （实施84 P0-2：拟稿注入人设口吻/长期记忆/工作目标背景——此前 care 文案只有
# 最近 8 条原文，产出「怎么样啦」式泛泛话术）。None/异常 → 空增强（旧行为）。
PromptExtrasProvider = Callable[[dict], dict]
# user_clock_provider：(item dict) -> UserClock|None（实施84 P0-6：安静时段按客户
# 当地时间顺延——「事件日 20:00 服务器时间」对跨时区客户可能是凌晨三点）。
# None/解析不出 → 服务器本地钟（旧行为）。
UserClockProvider = Callable[[dict], Any]
# goal_row_policy：(item dict) -> {"exempt_budget","ignore_quiet","jitter","live"}
# （P0 2026-08-30 冲刺推进器）：**只对 goal:* 行**征询的豁免策略——限时冲刺是用户
# 显式拍板的全力决定，联系人预算/安静时段顺延/慢抖动这些「骚扰型」闸门按
# 配置放行；危机/opt-out/对端 bot 等「安全型」闸门不在此列（绝不豁免）。
# ``live=True``（D1b P0-1 2026-09-05）＝该行**不吃 care 自己的灰度门**：
# ``proactive_care.enabled=false`` 时仍派、``dry_run=true`` 时仍真发——冲刺拍
# 只是借 care 的管线投递，care 的「灰度第一阶段看 LLM 质量」不是冲刺的灰度
# （冲刺有自己的 ``goals.sprint.enabled`` / ``goals.sprint.dry_run``）。此前
# 两者耦合＝桌面种子 care dry_run:true 让所有冲刺目标卡写「自动推进」却一拍不发。
# None/异常/非 goal 行 → {}（全默认，旧行为）。
GoalRowPolicy = Callable[[dict], dict]
# profile_provider：(item dict) -> 客户档案 dict（M-1 A #218，2026-09-06：
# {country, residence, language, known_since, known_days, display_name}）。LLM 档
# 拟稿必注入（【客户档案】块）并在出稿后过 ``check_care_reply``：与档案矛盾
# （把本地人当外国人 / 老客当新客）→ 拦下 skip；首次真发 / 含地名 / 含人名 →
# 强制预览（行留 pending 待运营确认）。None/异常 → 空档案（只剩强制预览判定）。
ProfileProvider = Callable[[dict], dict]
# deliver_now：(deferred row_id, platform) -> {"delivered", "status", "reason",
# "sent_at"?, "retry_at"?}（N-1 A #243，D-N4 ①）：运营「立即发」入队后**当场**投这一行，
# 不等 drain tick。由 background 侧接到多平台 deferred 队列的
# ``DeferredDispatcher.deliver_now``；None（旧调用方）或 messenger（浏览器队列，无同步
# 路径）→ 只入队，按「排队中」回话。
DeliverNow = Callable[[int, str], Awaitable[dict]]

# 运营「立即发」的决策码（send_now 返回 ``decision``；前端据此渲染卡片状态）
SEND_NOW_DECISIONS = ("sent", "queued", "dry_sampled", "skipped", "held",
                      "failed", "not_pending", "missing_route")

_IDENTITY_LEAK = ("作为AI", "作为一个AI", "AI助手", "as an AI", "i'm an ai", "i am an ai")

# B110（实施74 四批，0826 _527 实录「通用抒情模板达不到效果」）：
# ①具体锚点硬要求 ②结尾轻问题钩子 ④已发关怀负样本（recent_sent 追加块）。
# ③关系阶段分档留待 intimacy provider 接线（见实施74 备案），危机专线不动。
_CARE_PROMPT = """你是「{ai_name}」，正在和对方私聊。对方之前提到过 **{topic}**（{when_desc}），
现在是主动关心这件事的好时机。

【对方当时的原话】
{source_text}

【你们最近的对话要点（可自然引用）】
{context_block}
{extra_blocks}
请用对方习惯的语言（{lang}）发**一条**主动关心的消息：
- 像朋友一直惦记着这件事（"你之前说的{topic}…怎么样啦？"）
- **紧扣「{topic}」这件具体事**，不要泛泛的"在吗 / 最近好吗"
- 至少自然指涉一个上面「原话/对话要点」里的**具体事实**（工作、宠物、上次提的计划…）；
  要点为空时，就围绕「{topic}」写一个具体的小场景或小细节，
  **绝不**写"想你了 / 睡得好吗 / 今天醒得早又想起你"这类空泛抒情模板
- **结尾带一个轻巧的小问题**（好回答、不逼问），让对方能随手回一句；不要单向抒情收尾
- 短小亲切 1-2 句，可含一个 emoji
- 不要说"我是AI"或身份相关的话，不发链接、不索要联系方式

直接输出消息文本，不要前后缀、不要解释。"""

# ④防重负样本块（有已发记录才追加，零记录零 token）
_CARE_RECENT_BLOCK = """

【你最近已发过的关怀（绝不重复这些句子的开场、句式或比喻，换全新的说法）】
{recent_lines}"""

# 实施84 P1-2：目标推进型关怀（topic_norm=goal:* 的排期行）——框架是「你们在
# 推进的事到节点了」，不是「对方之前提到过」（那是约定回访的叙事，对目标行
# 是张冠李戴）。方向与分寸由 extra_blocks 里的【工作目标背景】进一步框定。
_GOAL_CARE_PROMPT = """你是「{ai_name}」，正在和对方私聊。你们之间有一件正在推进的事：**{topic}**（{source_text}），
现在是自然跟进一步的好时机。

【你们最近的对话要点（可自然引用）】
{context_block}
{extra_blocks}
请用对方习惯的语言（{lang}）发**一条**自然的消息：
- 以关心对方近况开场，顺势把「{topic}」轻轻往前带一步
- 绝不硬销、不催促、不报价、不甩链接；感觉对方兴致不高就只陪伴
- 尽量自然指涉一个「对话要点」里的具体事实；没有要点就围绕近况写个小切入
- **结尾带一个轻巧的小问题**（好回答、不逼问）
- 短小亲切 1-2 句，可含一个 emoji
- 不要说"我是AI"或身份相关的话，不索要联系方式

直接输出消息文本，不要前后缀、不要解释。"""

# P0 2026-08-30 冲刺推进器：带相位后缀的目标行（topic_norm=goal:{gid}:p{n}）
# 用「限时推进」专线——与普通目标行的差异是刻意的：这是用户显式拍板的限时
# 冲刺，收口拍**允许**明确说法/报价/给下一步动作（普通行的「不催促不报价」
# 刹车在这里松开）；体面底线仍在（对方明确拒绝就收住、不纠缠、不越界）。
_GOAL_SPRINT_PROMPT = """你是「{ai_name}」，正在和对方私聊。你们之间有一件**限时推进**的事：**{topic}**（{window_line}），
现在轮到你主动推进一步。

【本拍要做的事】
{phase_line}

【你们最近的对话要点（可自然引用）】
{context_block}
{extra_blocks}
请用对方习惯的语言（{lang}）发**一条**自然的消息：
- 按「本拍要做的事」推进：可以说得具体、给明确的说法或下一步；收口拍可以大方求一个答复
- 语气像真人朋友，自然体面；对方明确拒绝或情绪低落就体面收住，绝不纠缠
- 尽量自然指涉一个「对话要点」里的具体事实；结尾给一个好回答的小问题或明确的下一步
- 短小 1-2 句，可含一个 emoji
- 不要说"我是AI"或身份相关的话；绝不承诺线下见面/私下转账等越界内容

直接输出消息文本，不要前后缀、不要解释。"""


def _compose_extra_blocks(persona_line: str = "", memory_block: str = "",
                          goal_block: str = "", profile_block: str = "") -> str:
    """拟稿增强块（实施84 P0-2）：人设口吻 / 长期记忆 / 工作目标背景 /
    客户档案（M-1 A #218）。全空 → ""（prompt 与旧版逐字一致，零 token 开销）。
    档案块排最前：它是硬事实，LLM 读到风格/记忆之前先知道对方是谁。"""
    parts = []
    pb = str(profile_block or "").strip()
    if pb:
        parts.append(f"\n{pb}")
    p = str(persona_line or "").strip()
    if p:
        parts.append(f"\n【你的说话风格（保持这个人的口吻）】\n{p}")
    m = str(memory_block or "").strip()
    if m:
        parts.append("\n【你记得的关于对方的事（可自然引用一件，别一次全说）】"
                     f"\n{m}")
    g = str(goal_block or "").strip()
    if g:
        parts.append(f"\n{g}")
    return "".join(parts)

# Phase ④续¹⁰：危机来源关怀的「克制陪伴」模板——对方近期情绪低谷/危机信号，主动护栏
# 拦下了普通打扰、转这条关怀兜底。绝不能用日常约定回访那套轻快寒暄（"你之前说的X怎么样啦~"），
# 否则是二次伤害。这里只做安静、不追问、不带压力的陪伴感（"我在""不用急着回我"）。
_CRISIS_CARE_PROMPT = """你是「{ai_name}」，正在和对方私聊。对方最近情绪很低落、可能正经历一段难熬的时光。
现在你想轻轻地、不带任何压力地，让对方知道「你一直都在」。

请用对方习惯的语言（{lang}）发**一条**温柔的主动关心消息：
- 语气克制、温暖、安静：像一个静静陪在身边的人，而**不是**热情寒暄、不是查岗
- **不要**追问发生了什么、不要让对方"汇报近况"、不要提任何具体某件事
- **不要**用"最近怎么样啦 / 在吗 / 好点没"这类轻快或催促口吻，也**不要**催对方回复
- 可以传达"我在""不用急着回我""你不是一个人"这样的陪伴感
- 短小柔和 1-2 句，至多一个温柔的 emoji（也可以不用）
- 不评判、不给医疗/心理诊断建议，不发链接、不索要联系方式
- 不要说"我是AI"或任何身份相关的话

直接输出消息文本，不要前后缀、不要解释。"""


def shift_out_of_quiet_hours(
    ts: float, *, start_hour: float, end_hour: float, clock: Any = None,
) -> float:
    """命中安静时段则顺延到其结束时刻；否则原样返回。start==end 表示无安静窗。

    ``clock``（实施84 P0-6，可选 UserClock）：安静窗按**客户当地时间**判定与
    顺延——「事件日 20:00 服务器时间」对跨时区客户可能是凌晨三点。全程在
    客户本地 naive 时间里做差再折回 epoch（``user_now`` 铁律返回 naive）；
    换算异常回落服务器本地钟＝旧行为。
    """
    if start_hour == end_hour:
        return ts
    if clock is not None:
        try:
            from src.companion.user_clock import user_now
            udt = user_now(clock, ts)
            h = udt.hour + udt.minute / 60.0
            overnight = start_hour > end_hour
            in_quiet = (
                (not overnight and start_hour <= h < end_hour)
                or (overnight and (h >= start_hour or h < end_hour))
            )
            if not in_quiet:
                return ts
            target = udt.replace(hour=int(end_hour) % 24, minute=0,
                                 second=0, microsecond=0)
            if overnight and h >= start_hour:
                target = target + timedelta(days=1)
            if target <= udt:
                target = target + timedelta(days=1)
            return ts + (target - udt).total_seconds()
        except Exception:
            logger.debug("user clock 安静窗换算异常（回落服务器钟）", exc_info=True)
    dt = datetime.fromtimestamp(ts)
    h = dt.hour + dt.minute / 60.0
    overnight = start_hour > end_hour
    in_quiet = (
        (not overnight and start_hour <= h < end_hour)
        or (overnight and (h >= start_hour or h < end_hour))
    )
    if not in_quiet:
        return ts
    target = dt.replace(hour=int(end_hour) % 24, minute=0, second=0, microsecond=0)
    if overnight and h >= start_hour:
        target = target + timedelta(days=1)  # 深夜 → 次日早晨结束点
    if target.timestamp() <= ts:
        target = target + timedelta(days=1)
    return target.timestamp()


def build_care_prompt(
    item: dict,
    *,
    context_block: str = "",
    recent_sent: Optional[List[str]] = None,
    ai_name: str = "她",
    lang: str = "zh",
    now: Optional[float] = None,
    persona_line: str = "",
    memory_block: str = "",
    goal_block: str = "",
    profile_block: str = "",
) -> str:
    """由一条 care_schedule 行构造派发 prompt（危机主题自动切「克制陪伴」专线；
    ``topic_norm=goal:*`` 的排期行切「目标推进」专线）。

    ``profile_block``（M-1 A #218）＝客户档案硬事实块（``care_profile.profile_block``），
    派发与预览同源注入；危机专线同样不吃（克制陪伴不引用任何事实）。

    P2 2026-08-01 抽出为公共函数：派发器与 ``/api/care/schedule/{sid}/preview``
    预览端点共用——「先看后发」看到的就是真发同一句 prompt 口径，预判与行为
    永远一致（与草稿预判徽标同一设计纪律）。

    ``recent_sent``（B110④，实施74 四批）＝该联系人最近真发过的关怀话术
    （store.recent_sent_texts），作防重负样本追加进 prompt；空/None 零追加。
    ``persona_line`` / ``memory_block`` / ``goal_block``（实施84 P0-2）＝人设
    口吻 / 长期记忆要点 / 工作目标背景，全空零追加。危机专线刻意不吃任何
    增强与负样本（克制陪伴不需要花样，也绝不回放低谷内容）。
    """
    n = float(now if now is not None else time.time())
    topic = str(item.get("topic") or "").strip()
    if topic == CRISIS_CARE_TOPIC:
        return _CRISIS_CARE_PROMPT.format(ai_name=ai_name, lang=lang)
    # J-8 #182：记忆要点按**事件相关性**筛（人鱼潘事故：整块记忆无差别喂给
    # LLM → 无关记忆被硬塞进话术）。保留与 topic/source_text 有内容词重叠的条目，
    # 一条不剩 → 整块不注入。派发与预览共用本函数 → 同口径。
    if memory_block:
        try:
            from src.contacts.care_intent import filter_relevant_memory
            memory_block = filter_relevant_memory(
                memory_block, topic, str(item.get("source_text") or ""))
        except Exception:
            memory_block = ""
    extra_blocks = _compose_extra_blocks(
        persona_line=persona_line, memory_block=memory_block,
        goal_block=goal_block, profile_block=profile_block)
    is_goal_care = str(item.get("topic_norm") or "").startswith(
        GOAL_CARE_NORM_PREFIX)
    # 冲刺相位行（goal:{gid}:p{n}）→ 限时推进专线；解析失败回落普通目标行
    sprint_phase: Optional[int] = None
    if is_goal_care:
        try:
            from src.companion.goals.sprint_ticker import parse_goal_care_norm
            _gid, sprint_phase = parse_goal_care_norm(item.get("topic_norm"))
        except Exception:
            sprint_phase = None
    if is_goal_care and sprint_phase is not None:
        try:
            from src.companion.goals.sprint_ticker import (
                phase_directive,
                remaining_phrase,
            )
            _pl = phase_directive(int(sprint_phase))
            _dl = float(item.get("event_at") or 0)
            _win = (f"剩余{remaining_phrase(_dl - n)}" if _dl > n
                    else "窗口即将结束")
        except Exception:
            _pl = str(item.get("source_text") or "自然推进一步")[:120]
            _win = "时间窗有限"
        out = _GOAL_SPRINT_PROMPT.format(
            ai_name=ai_name,
            topic=topic or "那件事",
            window_line=_win,
            phase_line=_pl,
            context_block=context_block or "(无具体要点)",
            extra_blocks=extra_blocks,
            lang=lang,
        )
    elif is_goal_care:
        out = _GOAL_CARE_PROMPT.format(
            ai_name=ai_name,
            topic=topic or "那件事",
            source_text=(str(item.get("source_text") or "") or "(无)")[:200],
            context_block=context_block or "(无具体要点)",
            extra_blocks=extra_blocks,
            lang=lang,
        )
    else:
        out = _CARE_PROMPT.format(
            ai_name=ai_name,
            topic=topic or "那件事",
            when_desc=_when_desc(float(item.get("event_at") or n), n),
            source_text=(str(item.get("source_text") or "") or "(无)")[:200],
            context_block=context_block or "(无具体要点)",
            extra_blocks=extra_blocks,
            lang=lang,
        )
    lines = [s.strip() for s in (recent_sent or []) if str(s or "").strip()]
    if lines:
        out += _CARE_RECENT_BLOCK.format(
            recent_lines="\n".join(f"- {s[:80]}" for s in lines[:5]))
    return out


def _when_desc(event_at: float, now: float) -> str:
    """事件相对 now 的口语化描述（供 prompt：今天/昨天/这两天/即将）。"""
    try:
        ev_day = datetime.fromtimestamp(event_at).date()
        now_day = datetime.fromtimestamp(now).date()
    except Exception:
        return "最近"
    delta = (ev_day - now_day).days
    if delta == 0:
        return "就在今天"
    if delta == -1:
        return "昨天"
    if delta < -1:
        return "前几天"
    if delta == 1:
        return "明天"
    return "这几天"


class CareDispatcher:
    # max_per_tick=0（不限）时单 tick 最多取多少到期待办——只是查询页大小，
    # 不是业务上限（下一 tick 继续取余量）。
    _UNLIMITED_TICK_FETCH = 200

    def __init__(
        self,
        *,
        store: CareScheduleStore,
        ai_client: Any,
        send_callback: SendCallback,
        context_provider: Optional[ContextProvider] = None,
        already_discussed: Optional[AlreadyDiscussed] = None,
        proactive_allowed: Optional[ProactiveAllowed] = None,
        ai_name: str = "她",
        default_lang: str = "zh",
        max_per_tick: int = 3,
        interval_sec: float = 600.0,
        skip_if_no_context: bool = True,
        quiet_start_hour: float = 23.0,
        quiet_end_hour: float = 8.0,
        send_jitter_sec: tuple = (60.0, 1200.0),
        staleness_sec: float = 86400.0,
        dry_run: bool = False,
        expire_grace_days: float = 1.0,
        cfg_provider: Optional[CfgProvider] = None,
        budget_gate: Optional[BudgetGate] = None,
        sent_hook: Optional[SentHook] = None,
        peer_filter: Optional[PeerFilter] = None,
        prompt_extras_provider: Optional[PromptExtrasProvider] = None,
        user_clock_provider: Optional[UserClockProvider] = None,
        dry_resample_hours: float = 24.0,
        goal_row_policy: Optional[GoalRowPolicy] = None,
        profile_provider: Optional[ProfileProvider] = None,
        reply_gates: Optional[bool] = None,
        deliver_now: Optional[DeliverNow] = None,
    ) -> None:
        self._store = store
        self._ai = ai_client
        self._send = send_callback
        self._context_provider = context_provider
        self._already_discussed = already_discussed
        self._proactive_allowed = proactive_allowed
        self._ai_name = ai_name or "她"
        self._default_lang = default_lang or "zh"
        # 「0=不限」：max_per_tick<=0 ＝ 本 tick 不截断（到期的全派）
        self._max_per_tick = max(0, int(max_per_tick))
        self._interval = max(60.0, float(interval_sec))
        self._skip_if_no_context = bool(skip_if_no_context)
        self._quiet_start = float(quiet_start_hour)
        self._quiet_end = float(quiet_end_hour)
        self._jitter = send_jitter_sec
        self._staleness = float(staleness_sec)
        self._dry_run = bool(dry_run)
        self._expire_grace_days = float(expire_grace_days)
        # 配置热闸（P0 2026-08-01）：注入后 enabled/dry_run/max_per_tick 每 tick 实时读，
        # 常备循环 + overlay 热重载 = 开关免重启。未注入（单测/旧调用方）保持构造参语义。
        self._cfg_provider = cfg_provider
        # P3：每联系人主动预算闸 + 真发落账回调（均可选，None=旧行为）
        self._budget_gate = budget_gate
        self._sent_hook = sent_hook
        # 对方机器人/自家账号守卫（P1 2026-08-03）：None=旧行为（不拦）
        self._peer_filter = peer_filter
        # 实施84 P0-2/P0-6：拟稿增强（人设/记忆/目标）与客户时钟（安静窗按对方
        # 当地时间）。均可选，None=旧行为。
        self._prompt_extras_provider = prompt_extras_provider
        self._user_clock_provider = user_clock_provider
        # P0 2026-08-30 冲刺推进器：goal:* 行豁免策略（None=旧行为）
        self._goal_row_policy = goal_row_policy
        # M-1 A #218：客户档案 provider（LLM 档注入 + 事实校验）；None=空档案。
        # ``reply_gates``＝LLM 出稿三态闸（档案矛盾拦 / 首次真发·地名·人名强制预览）：
        # None=跟随 provider 是否注入（生产 background_tasks 恒注入 → 恒开；旧调用方 /
        # 未注入档案的单测保持旧行为）；True/False 显式钉死。
        self._profile_provider = profile_provider
        self._reply_gates = (bool(reply_gates) if reply_gates is not None
                             else profile_provider is not None)
        # N-1 A #243：「立即发」同步直投钩子（None=只入队、按排队中回话）
        self._deliver_now = deliver_now
        # M-1 A #214：dry_run 唯一真值 = 实时配置；构造参数只是启动快照。记住上一 tick
        # 的有效值，翻转时打一行 INFO——「启动行 dry_run=True 而 14:00 真发了」这类
        # 状态漂移此前在日志里零痕迹。
        self._last_effective_dry: Optional[bool] = None
        # 实施84 P0-4：dry 语义改「不消费待办」后，同一条待办每 tick 都会再到期
        # ——重拟冷却防止每 5 分钟烧一次 LLM（可经实时配置 dry_resample_hours 调）。
        self._dry_resample_hours = max(0.0, float(dry_resample_hours))
        # 健康自检读数（/api/care/health 消费）：最近一次 tick 的时刻/结果/是否被闸
        self.last_tick_ts: float = 0.0
        self.last_tick_scheduled: int = 0
        self.last_tick_gated: bool = False
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    def _live_cfg(self) -> Optional[dict]:
        """实时配置快照；无 provider 或读取异常 → None（沿用构造参数）。"""
        if self._cfg_provider is None:
            return None
        try:
            cfg = self._cfg_provider()
            return cfg if isinstance(cfg, dict) else None
        except Exception:
            logger.debug("care cfg_provider 读取异常（沿用构造参数）", exc_info=True)
            return None

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def effective_dry_run(self) -> bool:
        """dry_run **唯一真值源**（M-1 A #214）：实时配置有值取实时，否则取构造参数。

        面板 / ``/api/care/health`` / ``/api/care/plan`` / 「立即发」响应 / 派发判定
        全部读这一个方法，不再各自读一份配置快照——12:43「到点了下轮巡检就处理」
        与 14:03 真发出去这种面板与行为不一致，根源就是三处各读各的。
        """
        cfg = self._live_cfg()
        if cfg is None:
            return bool(self._dry_run)
        return bool(cfg.get("dry_run", False))

    def health_snapshot(self) -> dict:
        """链路自检用只读快照（无敏感字段）。"""
        return {
            "running": self.is_running(),
            "interval_sec": self._interval,
            "last_tick_ts": self.last_tick_ts,
            "last_tick_scheduled": self.last_tick_scheduled,
            "last_tick_gated": self.last_tick_gated,
            "dry_run_effective": self.effective_dry_run(),
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="care_dispatcher")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    n = await self.run_once()
                    if n:
                        logger.info("[care_dispatcher] tick: scheduled %d care msgs", n)
                except Exception:
                    logger.exception("care_dispatcher run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("care_dispatcher 退出")

    async def run_once(self, *, now: Optional[float] = None) -> int:
        """一次派发：返回成功 enqueue（或 dry_run 计数）的条数。

        配置热闸：注入 cfg_provider 时每 tick 先读实时配置——enabled=false 直接空转
        （**不碰 store、零副作用**，「默认关」语义与旧的不启动等价）；dry_run/max_per_tick
        同步跟随实时值，overlay 热重载后下一 tick 即生效。
        """
        n = float(now if now is not None else time.time())
        self.last_tick_ts = n
        cfg = self._live_cfg()
        # D1b P0-1：care 关闸时冲刺行（goal_row_policy.live）仍要派——care 的开关
        # 管的是「约定回访」这类 care 自己的业务，不是冲刺推进器的开关。
        goal_only = False
        if cfg is not None:
            if not cfg.get("enabled", False):
                self.last_tick_gated = True
                self.last_tick_scheduled = 0
                if self._goal_row_policy is None:
                    return 0
                goal_only = True
            self._dry_run = bool(cfg.get("dry_run", False))
            if self._last_effective_dry is not None and self._last_effective_dry != self._dry_run:
                logger.info(
                    "[care_dispatcher] dry_run 实时值变化 %s → %s（配置热重载；"
                    "面板/日志/派发同读此值）", self._last_effective_dry, self._dry_run)
            self._last_effective_dry = self._dry_run
            try:
                # 「0=不限」语义：0/负值＝本 tick 不截断（旧 max(1,…) 会把 0 静默钉成 1）。
                self._max_per_tick = max(0, int(cfg.get("max_per_tick", self._max_per_tick)))
            except Exception:
                pass
            try:
                self._dry_resample_hours = max(0.0, float(
                    cfg.get("dry_resample_hours", self._dry_resample_hours)))
            except Exception:
                pass
        if not goal_only:
            self.last_tick_gated = False
        # 每轮先清理逾期太久仍 pending 的待办（错过关怀时机不补发），best-effort
        try:
            expired = self._store.expire_overdue(now=n, grace_days=self._expire_grace_days)
            if expired:
                logger.info("[care_dispatcher] 清理逾期待办 %d 条", expired)
        except Exception:
            logger.debug("care_dispatcher expire_overdue 异常", exc_info=True)
        _cap = int(self._max_per_tick or 0)
        due = self._store.list_due(
            now=n, limit=(_cap * 4) if _cap > 0 else self._UNLIMITED_TICK_FETCH)
        if goal_only:
            # care 关闸：只放冲刺 live 行，其余到期行原样留 pending（care 开闸后
            # 仍按期发出、逾期由 expire_overdue 正常收口——与 dry 语义同款「不消费」）
            due = [it for it in (due or []) if self._goal_live(it)]
        if not due:
            self.last_tick_scheduled = 0
            return 0
        scheduled = 0
        for item in due:
            if _cap > 0 and scheduled >= _cap:
                break
            try:
                if await self._dispatch_one(item, n):
                    scheduled += 1
            except Exception:
                logger.debug("care dispatch_one 异常 id=%s", item.get("id"), exc_info=True)
        self.last_tick_scheduled = scheduled
        return scheduled

    def _goal_policy(self, item: dict) -> dict:
        """goal:* 行的豁免策略（非 goal 行 / 未注入 / 异常 → {}）。"""
        if self._goal_row_policy is None:
            return {}
        if not str(item.get("topic_norm") or "").startswith(GOAL_CARE_NORM_PREFIX):
            return {}
        try:
            gp = self._goal_row_policy(dict(item))
            return gp if isinstance(gp, dict) else {}
        except Exception:
            logger.debug("goal_row_policy 异常（按默认）", exc_info=True)
            return {}

    def _goal_live(self, item: dict) -> bool:
        """该 goal 行是否**绕过 care 灰度门**（enabled/dry_run）真发（D1b P0-1）。"""
        return bool(self._goal_policy(item).get("live"))

    def _mark_skipped(self, sid: int, reason: str) -> None:
        """store.mark_skipped + 记 metrics skip 原因（O·P 联动质量看板用）。"""
        self._store.mark_skipped(sid, note=reason)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_care_skipped(reason)
        except Exception:
            pass

    def prompt_extras(self, item: dict) -> dict:
        """拟稿增强块（人设/记忆/目标背景，实施84 P0-2）。

        公开方法：预览路由复用——「先看后发」与真发同一份增强口径（与
        build_care_prompt 抽为公共函数是同一条设计纪律）。provider 未注入/
        异常 → {}（prompt 与旧版逐字一致）。
        """
        if self._prompt_extras_provider is None:
            return {}
        try:
            out = self._prompt_extras_provider(dict(item))
            return out if isinstance(out, dict) else {}
        except Exception:
            logger.debug("care prompt_extras_provider 异常（忽略）", exc_info=True)
            return {}

    def customer_profile(self, item: dict) -> dict:
        """客户档案（M-1 A #218）。公开方法：预览路由与派发同源。provider 未注入 /
        异常 → 空档案 dict（``care_profile.empty_profile``），绝不抛。"""
        from src.contacts.care_profile import empty_profile
        if self._profile_provider is None:
            return empty_profile()
        try:
            out = self._profile_provider(dict(item))
            if not isinstance(out, dict):
                return empty_profile()
            base = empty_profile()
            base.update(out)
            return base
        except Exception:
            logger.debug("care profile_provider 异常（按空档案）", exc_info=True)
            return empty_profile()

    async def send_now(self, item: dict, *, now: Optional[float] = None) -> Dict[str, Any]:
        """运营「立即发」＝同步直投（N-1 A #243，D-N4 ①）。

        与到期派发**同一条** ``_dispatch_one``（同样的守卫 / 拟稿 / 档案校验 / 入队），
        差别只有三处：① 不吃 ``proactive_care.enabled`` 灰度门（运营亲手点的，不是
        自动派发）；② 零错峰、不做安静时段顺延（D-N4 ③：人工指定的时刻就是时刻）；
        ③ 入队后经 ``deliver_now`` 钩子**当场**投递并把结果带回来，而不是等 drain tick。
        ``dry_run`` 仍尊重：模拟运行中「立即发」＝立即拟稿留样，不真发（decision=dry_sampled）。

        返回 ``{"ok", "decision", "reason", "text", "row_id", "sent_at", "defer_until",
        "dry_run", "held"}``，绝不抛；``ok``＝已发出 / 已排队（暂态） / dry 留样。
        """
        n = float(now if now is not None else time.time())
        res: Dict[str, Any] = {"ok": False, "decision": "", "reason": "", "text": "",
                               "row_id": 0, "sent_at": 0.0, "defer_until": 0.0,
                               "dry_run": self.effective_dry_run(), "held": ""}
        try:
            ok = await self._dispatch_one(dict(item), n, manual=True, result=res)
        except Exception as ex:  # noqa: BLE001
            logger.warning("care send_now 异常 id=%s", item.get("id"), exc_info=True)
            res.update(decision="failed", reason=f"error:{type(ex).__name__}")
            return res
        if not res.get("decision"):
            res["decision"] = "sent" if ok else "failed"
        res["ok"] = res["decision"] in ("sent", "queued", "dry_sampled")
        return res

    async def _dispatch_one(self, item: dict, now: float, *, manual: bool = False,
                            result: Optional[Dict[str, Any]] = None) -> bool:
        def _r(**kw: Any) -> None:
            if result is not None:
                result.update(kw)

        sid = int(item["id"])
        contact_key = str(item.get("contact_key") or "")
        topic = str(item.get("topic") or "").strip()
        chat_key = str(item.get("chat_key") or "")
        platform = str(item.get("platform") or "")
        account_id = str(item.get("account_id") or "default") or "default"

        if manual and str(item.get("status") or "pending") != "pending":
            _r(decision="not_pending", reason="not_pending")
            return False

        if not chat_key or not platform:
            self._mark_skipped(sid, "missing platform/chat_key")
            _r(decision="missing_route", reason="missing platform/chat_key")
            return False

        # M-1 A #218：处于「待运营确认」态的行（LLM 拟稿命中强制预览）不再派发，
        # 等面板上「就这样发 / 改一改再发 / 跳过」或逾期过期。不烧 LLM、不 mark。
        _held = CareScheduleStore.hold_reason(item)
        if _held:
            _r(decision="held", reason="held", held=_held)
            return False

        # 对方机器人/自家账号守卫（P1 2026-08-03）：给 bot 发关怀是纯空转 + 向平台
        # 风控表演自动化，给自家账号发是自嗨。放最前（省最多 token）；**危机关怀
        # 也不豁免**——bot 不该收任何主动消息；异常按放行（fail-open）。
        if self._peer_filter is not None:
            try:
                if self._peer_filter(platform, account_id, chat_key):
                    self._mark_skipped(sid, "peer_bot_or_fleet")
                    _r(decision="skipped", reason="peer_bot_or_fleet")
                    return False
            except Exception:
                logger.debug("care peer_filter 异常（放行）", exc_info=True)

        # Phase ④续¹⁰：危机来源关怀（## 56 主动护栏拦下后转的兜底）走「克制陪伴」专线——
        # 不追问、不引用具体事、不寒暄；且**不因变现配额/无上下文而跳过**：
        # 危机期的一句陪伴不该被计费门控掐断，也不该因「没聊过具体事」而不发（陪伴本身即目的）。
        is_crisis_care = topic == CRISIS_CARE_TOPIC
        # 实施84 P1-2：目标推进型排期行（goal:*）——模板与 no_context 豁免见下
        is_goal_care = str(item.get("topic_norm") or "").startswith(
            GOAL_CARE_NORM_PREFIX)
        # J-8 #182：运营「到点发这句话」行（verbatim:*）——**不经 LLM**，原文进
        # deferred 队列；豁免 no_context / already_discussed（运营已决定要发什么），
        # 仍吃 peer 守卫 / 变现配额 / 联系人预算 / 安静时段 / 队列 gate。
        is_verbatim = is_verbatim_care(item)
        # P0 2026-08-30：冲刺行豁免策略（只对 goal:* 行征询；异常=全默认）
        goal_policy: dict = self._goal_policy(item) if is_goal_care else {}
        # D1b P0-1：本行的 dry 语义——冲刺 live 行不吃 care 的 dry_run（冲刺自己
        # 的灰度是 goals.sprint.dry_run，由 policy 决定 live 与否）。
        # N-1 A：「立即发」不经 run_once，dry 直接读唯一真值（实时配置）。
        _dry_base = self.effective_dry_run() if manual else bool(self._dry_run)
        dry = _dry_base and not goal_policy.get("live")
        _r(dry_run=bool(dry))

        # 实施84 P0-4：dry 语义改「不消费待办」后同一行每 tick 仍到期——重拟
        # 冷却窗内直接静默跳过（不 mark、不烧 LLM；行保持 pending 待真发/过期）。
        # 运营「立即发」不吃冷却（人要看现在这稿）。
        if dry and not manual:
            _last_dry = float(item.get("dry_sampled_at") or 0)
            if (_last_dry > 0
                    and (now - _last_dry) < self._dry_resample_hours * 3600.0):
                return False

        # K2b：变现配额门控——免费用户主动关怀超额 → 跳过（gate 关时回调返 True 不拦）。
        # 放在 LLM 之前，超额时不白耗 token。**危机关怀豁免**（伦理优先于变现）。
        if self._proactive_allowed is not None and not is_crisis_care:
            try:
                if not self._proactive_allowed(contact_key):
                    self._mark_skipped(sid, "paywall_quota")
                    _r(decision="skipped", reason="paywall_quota")
                    return False
            except Exception:
                logger.debug("proactive_allowed 异常（忽略放行）", exc_info=True)

        # O3 改进①：近期已主动聊过该事 → 跳过（防到点打卡）。危机关怀豁免（陪伴该送）。
        if (self._already_discussed is not None and not is_crisis_care
                and not is_verbatim):
            try:
                if self._already_discussed(contact_key, topic):
                    self._mark_skipped(sid, "already_discussed")
                    _r(decision="skipped", reason="already_discussed")
                    return False
            except Exception:
                logger.debug("already_discussed 异常（忽略）", exc_info=True)

        # P3：每联系人主动预算——该联系人今天已被主动摸过太多/太近 → 本条让路。
        # 放 LLM 之前（省 token）；只拦真发（dry_run 样本要流动）；危机关怀豁免；
        # 冲刺行按策略豁免（min_gap 4h/日 2 条的预算刻度是天级关怀的，2-12h
        # 冲刺窗内第二拍必被它吃掉——用户拍板的全力档不吃这个刹车）；
        # gate 自身异常按放行（fail-open：预算是体验优化，不是安全红线）。
        if (self._budget_gate is not None and not is_crisis_care
                and not dry
                and not goal_policy.get("exempt_budget")):
            allowed = True
            try:
                allowed = bool(self._budget_gate(contact_key))
            except Exception:
                logger.debug("budget_gate 异常（放行）", exc_info=True)
                allowed = True
            if not allowed:
                self._mark_skipped(sid, "contact_budget")
                _r(decision="skipped", reason="contact_budget")
                return False

        if is_verbatim:
            # 原文直发：运营手写的整句话就是要发的文本；零 LLM、零增强块、
            # 不过身份泄露/黑名单相似（那是给 AI 拟稿的守卫，人写的话人负责）。
            reply = care_verbatim_text(item)
            if not reply:
                self._mark_skipped(sid, "verbatim_empty")
                _r(decision="skipped", reason="verbatim_empty")
                return False
            logger.info(
                "[care-gen] id=%s contact=%s mode=verbatim dry=%s manual=%s text=%r",
                sid, contact_key, dry, manual, reply[:160])
        else:
            context_block = ""
            if self._context_provider is not None and not is_crisis_care:
                try:
                    context_block = (self._context_provider(contact_key) or "").strip()
                except Exception:
                    logger.debug("context_provider 异常", exc_info=True)
            # 危机关怀不要求上下文（陪伴本身即目的），也**不注入**对话要点（避免回放低谷内容）。
            # 目标推进行同样豁免（实施84 P1-2）：目标节点本身就是开口理由，source_text 自带背景。
            if (self._skip_if_no_context and not context_block
                    and not is_crisis_care and not is_goal_care):
                self._mark_skipped(sid, "no_context")
                _r(decision="skipped", reason="no_context")
                return False

            # B110④：该联系人最近已发关怀 → 防重负样本（危机专线不吃；读失败按空）
            recent_sent: List[str] = []
            if not is_crisis_care:
                try:
                    recent_sent = self._store.recent_sent_texts(contact_key, limit=4)
                except Exception:
                    recent_sent = []

            # 实施84 P0-2：拟稿增强块（人设口吻/记忆要点/目标背景）；危机专线不吃
            extras = {} if is_crisis_care else self.prompt_extras(item)
            # M-1 A #218：客户档案（国籍/居住地/相识时长/语言）必注入 + 出稿事实校验。
            # 危机专线不注入（克制陪伴不引用事实），但矛盾校验仍跑（同样不该把老客当新客）。
            profile = self.customer_profile(item)
            from src.contacts.care_profile import check_care_reply, profile_block
            pblock = "" if is_crisis_care else profile_block(profile)

            prompt = build_care_prompt(
                item, context_block=context_block, recent_sent=recent_sent,
                ai_name=self._ai_name, lang=self._default_lang, now=now,
                persona_line=str(extras.get("persona_line") or ""),
                memory_block=str(extras.get("memory_block") or ""),
                goal_block=str(extras.get("goal_block") or ""),
                profile_block=pblock)
            try:
                reply = (await self._ai.chat(prompt) or "").strip()
            except Exception:
                logger.warning("care LLM 失败 id=%s", sid, exc_info=True)
                _r(decision="failed", reason="llm_error")
                if manual and hasattr(self._store, "mark_send_failed"):
                    self._store.mark_send_failed(sid, "llm_error", now=now)
                return False  # 留 pending，下个 tick 重试
            # M-1 A #218 ④：care 生成落日志——输入原文 / 注入了哪些字段 / 输出 / 走了哪档。
            # 此前 care 生成零上下文日志，事故只能靠客户端截图倒推。
            logger.info(
                "[care-gen] id=%s contact=%s mode=%s dry=%s topic=%r source=%r "
                "profile=%s ctx_chars=%d memory=%s goal=%s persona=%s reply=%r",
                sid, contact_key,
                ("crisis" if is_crisis_care else "goal" if is_goal_care else "event"),
                dry, topic[:60], str(item.get("source_text") or "")[:80],
                {k: profile.get(k) for k in ("country", "residence", "language",
                                             "known_since", "known_days") if profile.get(k)},
                len(context_block), bool(extras.get("memory_block")),
                bool(extras.get("goal_block")), bool(extras.get("persona_line")),
                reply[:200])
            if not reply or len(reply) < 4:
                self._mark_skipped(sid, "llm_empty")
                _r(decision="skipped", reason="llm_empty")
                return False
            low = reply.lower()
            if any(b.lower() in low for b in _IDENTITY_LEAK):
                self._mark_skipped(sid, "identity_leak")
                _r(decision="skipped", reason="identity_leak")
                return False

            # Phase O 质量闭环：与运营 dislike 黑名单话术相似 → 重生成一次，仍相似则跳过
            # （复用 reactivation 同一黑名单：被标记的雷同话术在 care 里同样该避免）
            reply = await self._avoid_disliked(prompt, reply, sid)
            if not reply:
                _r(decision="skipped", reason="disliked_similarity")
                return False

            # M-1 A #218 ②③：事实校验 + 强制预览。与档案矛盾（本地人当外国人 / 老客当
            # 新客）→ 拦下 skip 并留原因；首次对该客户真发 / 含地名 / 含人名 → 不发，
            # 草稿留 pending 待运营确认（dry 档只是看质量，不拦——样本要流动）。
            if self._reply_gates and not dry:
                first_send = True
                try:
                    if hasattr(self._store, "has_real_sent"):
                        first_send = not self._store.has_real_sent(contact_key)
                except Exception:
                    first_send = True
                verdict = check_care_reply(reply, profile, first_real_send=first_send)
                if verdict["verdict"] == "block":
                    logger.warning(
                        "[care-gen] id=%s contact=%s 拟稿与客户档案矛盾 → 拦下不发 reason=%s "
                        "reply=%r profile=%s", sid, contact_key, verdict["reason"],
                        reply[:120], {k: profile.get(k) for k in ("country", "residence",
                                                                  "known_since", "known_days")})
                    self._mark_skipped(sid, verdict["reason"])
                    _r(decision="skipped", reason=verdict["reason"], text=reply)
                    return False
                if verdict["verdict"] == "preview":
                    if hasattr(self._store, "mark_hold_for_preview"):
                        self._store.mark_hold_for_preview(
                            sid, sent_text=reply, reason=verdict["reason"], now=now)
                        logger.info(
                            "[care-gen] id=%s contact=%s 强制预览 → 留待运营确认 reason=%s",
                            sid, contact_key, verdict["reason"])
                        _r(decision="held", reason="held", held=verdict["reason"], text=reply)
                        return False
                    logger.warning(
                        "[care-gen] id=%s store 不支持 hold（旧版），强制预览退化为放行 reason=%s",
                        sid, verdict["reason"])

        # O3 改进②：发送时刻命中 quiet_hours → 顺延到结束（而非跳过）。
        # 实施84 P0-6：能解析出客户时钟时按**对方当地时间**判安静窗（provider
        # 未注入/解析不出 → clock=None＝服务器钟，旧行为）。
        _clock = None
        if self._user_clock_provider is not None:
            try:
                _clock = self._user_clock_provider(dict(item))
            except Exception:
                _clock = None
        # 冲刺行用快抖动（默认 60-1200s 对 2-12h 窗口太慢）；策略给坏值回默认
        _jit = self._jitter
        gp_jit = goal_policy.get("jitter")
        if isinstance(gp_jit, (tuple, list)) and len(gp_jit) == 2:
            try:
                _jit = (float(gp_jit[0]), float(gp_jit[1]))
            except (TypeError, ValueError):
                _jit = self._jitter
        if manual:
            # N-1 A（D-N4 ①③）：运营亲手点「立即发」——零错峰、不做安静时段顺延，
            # 现在就是现在。安全型闸（急停 / 无 sender）由投递层守。
            defer_until = now
        else:
            defer_until = now + random.uniform(_jit[0], _jit[1])
            # 冲刺行可按策略跳过安静时段顺延（顺延到早 8 点＝3 小时目标必死；
            # 用户拍板的全力档自担深夜打扰）；普通行为不变
            if not goal_policy.get("ignore_quiet"):
                defer_until = shift_out_of_quiet_hours(
                    defer_until, start_hour=self._quiet_start,
                    end_hour=self._quiet_end, clock=_clock)
        _r(text=reply, defer_until=defer_until)

        if dry:
            logger.info("[care DRY] id=%s contact=%s topic=%s manual=%s reply=%r",
                        sid, contact_key, topic, manual, reply[:120])
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_care_dry_run(sample={
                    "care_id": sid,
                    "contact_key": contact_key,
                    "topic": topic,
                    "platform": platform,
                    "account_id": account_id,
                    "chat_key": chat_key,
                    "reply_text": reply,
                    "lang": self._default_lang,
                    "would_send_in_min": int(max(0.0, defer_until - now) / 60),
                })
            except Exception:
                logger.debug("record_care_dry_run 异常", exc_info=True)
            # 快照进 care 表（持久口径）但**不消费待办**（实施84 P0-4）：行保持
            # pending——转真发后仍按期发出、逾期由 expire_overdue 正常收口。
            # 旧版 store 无该方法时回落旧语义（混版部署不炸链）。
            if hasattr(self._store, "mark_dry_sampled"):
                self._store.mark_dry_sampled(sid, sent_text=reply, now=now)
            else:
                self._store.mark_sent(sid, note="dry_run", sent_text=reply)
            _r(decision="dry_sampled")
            return True

        try:
            row_id = await self._send(
                platform, account_id, chat_key, reply, defer_until,
                ("care:crisis" if is_crisis_care
                 else "care:verbatim" if is_verbatim
                 else f"care:{topic[:24]}"),
                self._staleness,
                {"care": True, "care_id": sid, "contact_key": contact_key,
                 "topic": topic, "crisis_care": is_crisis_care,
                 "verbatim": is_verbatim, "manual": bool(manual)},
            )
        except Exception:
            logger.warning("care send_callback 失败 id=%s", sid, exc_info=True)
            _r(decision="failed", reason="send_error")
            if manual and hasattr(self._store, "mark_send_failed"):
                self._store.mark_send_failed(sid, "send_error", now=now)
            return False  # 留 pending 重试
        if not row_id:
            # N-1 D：不发而留 pending 必给原因——队列关（multiplatform_deferred.enabled=false）
            # 或 messenger runner 未起，此前只有一句「gated」。
            logger.info("[care-gen] id=%s contact=%s decision=gated reason=queue_unavailable "
                        "platform=%s manual=%s（deferred 队列未接收，留 pending）",
                        sid, contact_key, platform, manual)
            _r(decision="failed", reason="queue_unavailable")
            if manual and hasattr(self._store, "mark_send_failed"):
                self._store.mark_send_failed(sid, "queue_unavailable", now=now)
            return False  # enqueue 失败（如 gate 拦）→ 留 pending
        _r(row_id=int(row_id))
        logger.info(
            "[care-gen] id=%s contact=%s decision=enqueued deferred=%s send_in_min=%d manual=%s",
            sid, contact_key, int(row_id), int(max(0.0, defer_until - now) / 60), manual)

        if manual:
            # N-1 A：入队后当场投这一行。钩子缺席（messenger 浏览器队列 / 旧接线）
            # → 只入队，按「排队中（预计 defer_until）」如实回话。
            dres: Dict[str, Any] = {}
            if self._deliver_now is not None:
                try:
                    dres = dict(await self._deliver_now(int(row_id), platform) or {})
                except Exception:
                    logger.warning("care deliver_now 异常 id=%s row=%s", sid, row_id,
                                   exc_info=True)
                    dres = {"delivered": False, "status": "pending",
                            "reason": "deliver_now_error"}
            else:
                dres = {"delivered": False, "status": "pending", "reason": "no_sync_path"}
            if dres.get("delivered"):
                sent_at = float(dres.get("sent_at") or now)
                _r(decision="sent", sent_at=sent_at)
                logger.info("[care-gen] id=%s contact=%s decision=sent_now deferred=%s sent_at=%.0f",
                            sid, contact_key, int(row_id), sent_at)
            elif str(dres.get("status") or "") == "pending":
                retry_at = float(dres.get("retry_at") or defer_until or now)
                _r(decision="queued", reason=str(dres.get("reason") or "queued"),
                   defer_until=retry_at)
                logger.info("[care-gen] id=%s contact=%s decision=queued reason=%s retry_at=%.0f "
                            "deferred=%s", sid, contact_key, dres.get("reason"), retry_at,
                            int(row_id))
            else:
                # 永久失败（平台拒收 / sender 抛错）：队列行已 failed；care 行留 pending
                # 并记 note=fail:<原因>，卡片显「失败（原因）」且「立即发」可再点。
                why = str(dres.get("reason") or "send_failed")
                _r(decision="failed", reason=why)
                logger.warning("[care-gen] id=%s contact=%s decision=send_failed reason=%s "
                               "deferred=%s", sid, contact_key, why, int(row_id))
                if hasattr(self._store, "mark_send_failed"):
                    self._store.mark_send_failed(sid, why, now=now)
                return False
        # P3：真发成功 → 触达落共享账本（outreach_log），对预算/周报可见。
        # 绝不影响已完成的发送（best-effort）。
        if self._sent_hook is not None:
            try:
                self._sent_hook(dict(item))
            except Exception:
                logger.debug("care sent_hook 异常（忽略）", exc_info=True)
        # 话术快照随行留档（deferred 队列有终态保留期，审计文本以本表为持久口径）
        self._store.mark_sent(sid, note=f"deferred:{int(row_id)}", sent_text=reply)
        return True

    async def deliver_text(self, item: dict, text: str, *,
                           now: Optional[float] = None) -> Dict[str, Any]:
        """J-8 #182「改一改再发」：运营在预览上手改的终稿**直接**送出站队列。

        与派发同一条 ``_send``（deferred 队列 gate / pacing / kill-switch 全享），
        安静时段同样顺延；成功 → 该 care 行 mark_sent（note=``manual:deferred:<id>``，
        话术快照留档）+ sent_hook 落触达账本。不经 LLM、不过拟稿守卫（人写的话
        人负责）。返回 ``{"ok", "reason", "row_id"}``，绝不抛。
        """
        n = float(now if now is not None else time.time())
        sid = int(item.get("id") or 0)
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "reason": "empty_text"}
        chat_key = str(item.get("chat_key") or "")
        platform = str(item.get("platform") or "")
        account_id = str(item.get("account_id") or "default") or "default"
        contact_key = str(item.get("contact_key") or "")
        if not chat_key or not platform:
            return {"ok": False, "reason": "missing_route"}
        _clock = None
        if self._user_clock_provider is not None:
            try:
                _clock = self._user_clock_provider(dict(item))
            except Exception:
                _clock = None
        defer_until = shift_out_of_quiet_hours(
            n + random.uniform(self._jitter[0], min(self._jitter[1], 120.0)),
            start_hour=self._quiet_start, end_hour=self._quiet_end, clock=_clock)
        try:
            row_id = await self._send(
                platform, account_id, chat_key, body, defer_until,
                "care:manual", self._staleness,
                {"care": True, "care_id": sid, "contact_key": contact_key,
                 "topic": str(item.get("topic") or ""), "crisis_care": False,
                 "manual_rewrite": True},
            )
        except Exception:
            logger.warning("care deliver_text 失败 id=%s", sid, exc_info=True)
            return {"ok": False, "reason": "send_failed"}
        if not row_id:
            return {"ok": False, "reason": "gated"}
        logger.info(
            "[care-gen] id=%s contact=%s mode=manual decision=enqueued deferred=%s "
            "held=%r text=%r", sid, contact_key, int(row_id),
            CareScheduleStore.hold_reason(item) or "", body[:160])
        if self._sent_hook is not None:
            try:
                self._sent_hook(dict(item))
            except Exception:
                logger.debug("care sent_hook 异常（忽略）", exc_info=True)
        try:
            if sid:
                self._store.mark_sent(sid, note=f"manual:deferred:{int(row_id)}",
                                      sent_text=body)
        except Exception:
            logger.debug("care deliver_text mark_sent 异常", exc_info=True)
        return {"ok": True, "reason": "", "row_id": int(row_id),
                "defer_until": defer_until}

    def _hits_dislike(self, reply: str):
        """(命中?, 相似样本)。双源黑名单（实施84 P0-6）：

        ① metrics 会话级黑名单——**刻意进程内**（既有设计：主观判断重启重审）；
        ② care 自己的持久 👎 判定（``review`` 列）——本机日均多次重启，纯进程
          黑名单实际存活不了几小时；care 的审核判定本就落库，防重直接读它，
          重启后黑名单不再清零。任一源异常按未命中（fail-open）。
        """
        try:
            from src.monitoring.metrics_store import get_metrics_store
            is_sim, similar_to = get_metrics_store().is_similar_to_disliked(
                reply, threshold=0.7)
            if is_sim:
                return True, similar_to
        except Exception:
            pass
        try:
            persisted = (self._store.disliked_texts(limit=20)
                         if hasattr(self._store, "disliked_texts") else [])
        except Exception:
            persisted = []
        if persisted:
            from difflib import SequenceMatcher
            best, best_t = 0.0, ""
            for d in persisted:
                r = SequenceMatcher(None, reply, d).ratio()
                if r > best:
                    best, best_t = r, d
            if best >= 0.7:
                return True, best_t
        return False, ""

    async def _avoid_disliked(self, prompt: str, reply: str, sid: int) -> str:
        """Phase O 质量闭环：reply 命中 dislike 黑名单 → 重生成一次。

        返回最终可用 reply；若重生成仍相似/失败则 mark_skipped 并返回空串。
        黑名单＝metrics 会话级 + care 持久 👎 双源（``_hits_dislike``）。
        """
        try:
            is_sim, similar_to = self._hits_dislike(reply)
        except Exception:
            return reply
        if not is_sim:
            return reply
        logger.info("[care] reply 与 dislike 黑名单相似 → 重生成 id=%s", sid)
        prompt2 = (
            prompt
            + "\n\n注意：和这条话术风格雷同的版本之前被运营标记为不合适，"
            + "请彻底换一种说法，不要复用以下结构或开头：\n"
            + (similar_to or "")[:200]
        )
        try:
            reply2 = (await self._ai.chat(prompt2) or "").strip()
        except Exception:
            reply2 = ""
        if reply2 and len(reply2) >= 4:
            try:
                is_sim2, _ = self._hits_dislike(reply2)
            except Exception:
                is_sim2 = False
            low2 = reply2.lower()
            if not is_sim2 and not any(b.lower() in low2 for b in _IDENTITY_LEAK):
                logger.info("[care] 重生成成功 id=%s", sid)
                return reply2
        self._mark_skipped(sid, "disliked_similarity")
        return ""


__all__ = ["CareDispatcher", "build_care_prompt", "shift_out_of_quiet_hours",
           "SEND_NOW_DECISIONS"]
