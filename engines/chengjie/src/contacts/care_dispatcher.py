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
from typing import Any, Awaitable, Callable, List, Optional

from src.contacts.care_schedule import CRISIS_CARE_TOPIC, CareScheduleStore

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


def shift_out_of_quiet_hours(ts: float, *, start_hour: float, end_hour: float) -> float:
    """命中安静时段则顺延到其结束时刻；否则原样返回。start==end 表示无安静窗。"""
    if start_hour == end_hour:
        return ts
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
) -> str:
    """由一条 care_schedule 行构造派发 prompt（危机主题自动切「克制陪伴」专线）。

    P2 2026-08-01 抽出为公共函数：派发器与 ``/api/care/schedule/{sid}/preview``
    预览端点共用——「先看后发」看到的就是真发同一句 prompt 口径，预判与行为
    永远一致（与草稿预判徽标同一设计纪律）。

    ``recent_sent``（B110④，实施74 四批）＝该联系人最近真发过的关怀话术
    （store.recent_sent_texts），作防重负样本追加进 prompt；空/None 零追加。
    危机专线刻意不吃本参数（克制陪伴不需要花样，重复的「我在」不是缺陷）。
    """
    n = float(now if now is not None else time.time())
    topic = str(item.get("topic") or "").strip()
    if topic == CRISIS_CARE_TOPIC:
        return _CRISIS_CARE_PROMPT.format(ai_name=ai_name, lang=lang)
    out = _CARE_PROMPT.format(
        ai_name=ai_name,
        topic=topic or "那件事",
        when_desc=_when_desc(float(item.get("event_at") or n), n),
        source_text=(str(item.get("source_text") or "") or "(无)")[:200],
        context_block=context_block or "(无具体要点)",
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
    ) -> None:
        self._store = store
        self._ai = ai_client
        self._send = send_callback
        self._context_provider = context_provider
        self._already_discussed = already_discussed
        self._proactive_allowed = proactive_allowed
        self._ai_name = ai_name or "她"
        self._default_lang = default_lang or "zh"
        self._max_per_tick = max(1, int(max_per_tick))
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

    def health_snapshot(self) -> dict:
        """链路自检用只读快照（无敏感字段）。"""
        cfg = self._live_cfg()
        dry = self._dry_run if cfg is None else bool(cfg.get("dry_run", False))
        return {
            "running": self.is_running(),
            "interval_sec": self._interval,
            "last_tick_ts": self.last_tick_ts,
            "last_tick_scheduled": self.last_tick_scheduled,
            "last_tick_gated": self.last_tick_gated,
            "dry_run_effective": dry,
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
        if cfg is not None:
            if not cfg.get("enabled", False):
                self.last_tick_gated = True
                self.last_tick_scheduled = 0
                return 0
            self._dry_run = bool(cfg.get("dry_run", False))
            try:
                self._max_per_tick = max(1, int(cfg.get("max_per_tick", self._max_per_tick)))
            except Exception:
                pass
        self.last_tick_gated = False
        # 每轮先清理逾期太久仍 pending 的待办（错过关怀时机不补发），best-effort
        try:
            expired = self._store.expire_overdue(now=n, grace_days=self._expire_grace_days)
            if expired:
                logger.info("[care_dispatcher] 清理逾期待办 %d 条", expired)
        except Exception:
            logger.debug("care_dispatcher expire_overdue 异常", exc_info=True)
        due = self._store.list_due(now=n, limit=self._max_per_tick * 4)
        if not due:
            self.last_tick_scheduled = 0
            return 0
        scheduled = 0
        for item in due:
            if scheduled >= self._max_per_tick:
                break
            try:
                if await self._dispatch_one(item, n):
                    scheduled += 1
            except Exception:
                logger.debug("care dispatch_one 异常 id=%s", item.get("id"), exc_info=True)
        self.last_tick_scheduled = scheduled
        return scheduled

    def _mark_skipped(self, sid: int, reason: str) -> None:
        """store.mark_skipped + 记 metrics skip 原因（O·P 联动质量看板用）。"""
        self._store.mark_skipped(sid, note=reason)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_care_skipped(reason)
        except Exception:
            pass

    async def _dispatch_one(self, item: dict, now: float) -> bool:
        sid = int(item["id"])
        contact_key = str(item.get("contact_key") or "")
        topic = str(item.get("topic") or "").strip()
        chat_key = str(item.get("chat_key") or "")
        platform = str(item.get("platform") or "")
        account_id = str(item.get("account_id") or "default") or "default"

        if not chat_key or not platform:
            self._mark_skipped(sid, "missing platform/chat_key")
            return False

        # 对方机器人/自家账号守卫（P1 2026-08-03）：给 bot 发关怀是纯空转 + 向平台
        # 风控表演自动化，给自家账号发是自嗨。放最前（省最多 token）；**危机关怀
        # 也不豁免**——bot 不该收任何主动消息；异常按放行（fail-open）。
        if self._peer_filter is not None:
            try:
                if self._peer_filter(platform, account_id, chat_key):
                    self._mark_skipped(sid, "peer_bot_or_fleet")
                    return False
            except Exception:
                logger.debug("care peer_filter 异常（放行）", exc_info=True)

        # Phase ④续¹⁰：危机来源关怀（## 56 主动护栏拦下后转的兜底）走「克制陪伴」专线——
        # 不追问、不引用具体事、不寒暄；且**不因变现配额/无上下文而跳过**：
        # 危机期的一句陪伴不该被计费门控掐断，也不该因「没聊过具体事」而不发（陪伴本身即目的）。
        is_crisis_care = topic == CRISIS_CARE_TOPIC

        # K2b：变现配额门控——免费用户主动关怀超额 → 跳过（gate 关时回调返 True 不拦）。
        # 放在 LLM 之前，超额时不白耗 token。**危机关怀豁免**（伦理优先于变现）。
        if self._proactive_allowed is not None and not is_crisis_care:
            try:
                if not self._proactive_allowed(contact_key):
                    self._mark_skipped(sid, "paywall_quota")
                    return False
            except Exception:
                logger.debug("proactive_allowed 异常（忽略放行）", exc_info=True)

        # O3 改进①：近期已主动聊过该事 → 跳过（防到点打卡）。危机关怀豁免（陪伴该送）。
        if self._already_discussed is not None and not is_crisis_care:
            try:
                if self._already_discussed(contact_key, topic):
                    self._mark_skipped(sid, "already_discussed")
                    return False
            except Exception:
                logger.debug("already_discussed 异常（忽略）", exc_info=True)

        # P3：每联系人主动预算——该联系人今天已被主动摸过太多/太近 → 本条让路。
        # 放 LLM 之前（省 token）；只拦真发（dry_run 样本要流动）；危机关怀豁免；
        # gate 自身异常按放行（fail-open：预算是体验优化，不是安全红线）。
        if (self._budget_gate is not None and not is_crisis_care
                and not self._dry_run):
            allowed = True
            try:
                allowed = bool(self._budget_gate(contact_key))
            except Exception:
                logger.debug("budget_gate 异常（放行）", exc_info=True)
                allowed = True
            if not allowed:
                self._mark_skipped(sid, "contact_budget")
                return False

        context_block = ""
        if self._context_provider is not None and not is_crisis_care:
            try:
                context_block = (self._context_provider(contact_key) or "").strip()
            except Exception:
                logger.debug("context_provider 异常", exc_info=True)
        # 危机关怀不要求上下文（陪伴本身即目的），也**不注入**对话要点（避免回放低谷内容）。
        if self._skip_if_no_context and not context_block and not is_crisis_care:
            self._mark_skipped(sid, "no_context")
            return False

        # B110④：该联系人最近已发关怀 → 防重负样本（危机专线不吃；读失败按空）
        recent_sent: List[str] = []
        if not is_crisis_care:
            try:
                recent_sent = self._store.recent_sent_texts(contact_key, limit=4)
            except Exception:
                recent_sent = []

        prompt = build_care_prompt(
            item, context_block=context_block, recent_sent=recent_sent,
            ai_name=self._ai_name, lang=self._default_lang, now=now)
        try:
            reply = (await self._ai.chat(prompt) or "").strip()
        except Exception:
            logger.warning("care LLM 失败 id=%s", sid, exc_info=True)
            return False  # 留 pending，下个 tick 重试
        if not reply or len(reply) < 4:
            self._mark_skipped(sid, "llm_empty")
            return False
        low = reply.lower()
        if any(b.lower() in low for b in _IDENTITY_LEAK):
            self._mark_skipped(sid, "identity_leak")
            return False

        # Phase O 质量闭环：与运营 dislike 黑名单话术相似 → 重生成一次，仍相似则跳过
        # （复用 reactivation 同一黑名单：被标记的雷同话术在 care 里同样该避免）
        reply = await self._avoid_disliked(prompt, reply, sid)
        if not reply:
            return False

        # O3 改进②：发送时刻命中 quiet_hours → 顺延到结束（而非跳过）
        defer_until = now + random.uniform(self._jitter[0], self._jitter[1])
        defer_until = shift_out_of_quiet_hours(
            defer_until, start_hour=self._quiet_start, end_hour=self._quiet_end)

        if self._dry_run:
            logger.info("[care DRY] id=%s contact=%s topic=%s reply=%r",
                        sid, contact_key, topic, reply[:120])
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
            # 快照进 care 表：metrics 侧样本是进程内易失的，本列是持久口径
            self._store.mark_sent(sid, note="dry_run", sent_text=reply)
            return True

        try:
            row_id = await self._send(
                platform, account_id, chat_key, reply, defer_until,
                ("care:crisis" if is_crisis_care else f"care:{topic[:24]}"),
                self._staleness,
                {"care": True, "care_id": sid, "contact_key": contact_key,
                 "topic": topic, "crisis_care": is_crisis_care},
            )
        except Exception:
            logger.warning("care send_callback 失败 id=%s", sid, exc_info=True)
            return False  # 留 pending 重试
        if not row_id:
            return False  # enqueue 失败（如 gate 拦）→ 留 pending
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

    async def _avoid_disliked(self, prompt: str, reply: str, sid: int) -> str:
        """Phase O 质量闭环：reply 命中 dislike 黑名单 → 重生成一次。

        返回最终可用 reply；若重生成仍相似/失败则 mark_skipped 并返回空串。
        复用 reactivation 的同一黑名单（被运营标记的雷同话术应全局避免）。
        """
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
        except Exception:
            return reply  # metrics 不可用不阻断派发
        try:
            is_sim, similar_to = ms.is_similar_to_disliked(reply, threshold=0.7)
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
                is_sim2, _ = ms.is_similar_to_disliked(reply2, threshold=0.7)
            except Exception:
                is_sim2 = False
            low2 = reply2.lower()
            if not is_sim2 and not any(b.lower() in low2 for b in _IDENTITY_LEAK):
                logger.info("[care] 重生成成功 id=%s", sid)
                return reply2
        self._mark_skipped(sid, "disliked_similarity")
        return ""


__all__ = ["CareDispatcher", "build_care_prompt", "shift_out_of_quiet_hours"]
