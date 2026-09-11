"""AutosendWorker — L2 草稿自动发送后台任务（Phase A + C3 事件驱动升级）。

设计要点：
  C3 升级（事件驱动）：
  - 新 L2 草稿落库时，InboxStore 通过回调立即唤醒 worker（notify_new_l2）
  - 替代纯定时轮询：延迟从最多 60s 降至毫秒级，同时保留定时兜底
  - asyncio.Event + loop.call_soon_threadsafe 确保线程安全

  Phase A 保留：
  - 自适应间隔：有发送时使用 min_interval，静默时指数扩张到 max_interval
  - 熔断器：连续 circuit_threshold 次失败后进入 open 状态，等待 cooldown_sec 后重试
  - 每草稿隔离：单条发送失败不影响同批次其他草稿
  - 指标：total_sent / total_errors / last_run_ts / last_sent 供 /api/drafts/autosend-status 暴露

配置（config.yaml::inbox.l2_autosend）：
  enabled: true
  min_interval_sec: 60      # 有活动时的最短轮询间隔（也是定时兜底上限的起点）
  max_interval_sec: 600     # 静默时的最长兜底间隔
  circuit_threshold: 5      # 触发熔断的连续错误次数
  cooldown_sec: 300         # 熔断冷却时间
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 投递回调签名：async (platform, account_id, chat_key, text[, original_text]) -> dict
#   original_text 为**可选 kwarg**（经签名探测透传）：翻译前的人设原文。语音分支须用
#   原文判定+合成（人设克隆声念原语言），文本回落才用译文——修「先翻译再判语音 →
#   英文译文超长永远 too_long 静默回落文本」。
SendCallback = Callable[[str, str, str, str], Awaitable[Dict[str, Any]]]
# 出站翻译回调签名：async (to_deliver item dict) -> 应真正发出的文本（译文或回落原文）
TranslateCallback = Callable[[Dict[str, Any]], Awaitable[str]]
# 已读回调签名：async (platform, account_id, chat_key) -> Any
#   投递前给该会话发平台「已读」回执（拟人「先看后回」）；best-effort，失败不阻断投递。
MarkReadCallback = Callable[[str, str, str], Awaitable[Any]]
# 打字状态回调签名：async (platform, account_id, chat_key, action) -> Any
#   投递前拟人打字延迟期间周期挂「正在输入」状态；best-effort，失败不阻断投递。
TypingCallback = Callable[[str, str, str, str], Awaitable[Any]]

# 打字续挂间隔：单一来源在 humanize（chat action ~5s 过期，续挂须短于之）。
from src.inbox.humanize import DEFAULT_TYPING_REFRESH_SEC as _TYPING_REFRESH_SEC

# B41 投递幂等钉（2026-08-22）：同 (会话,草稿) 的同文只许成功出门一次。
from src.inbox.deliver_once import DeliverOnceRegistry

# 防双循环（B41）：同一 draft_service 只允许一个自动循环在跑——deliver 热切换 /
# 假想的重复装配若起第二个 run()，两循环各自 list+resolve+deliver 就是结构性双发。
# 键=id(draft_service)：测试各自造替身互不影响；生产同 svc 二次启动被拒并留痕。
_RUNNING_SVC_KEYS: set = set()

# apply_send_callbacks 的「未传」哨兵（None 是合法值=撤能力，不能当缺省用）
_UNSET = object()

# Q-3（#264 C）：坐席「刚刚」打字 / 发送的窗口——窗口内 worker 视为插话，放弃在途 AI 稿
AGENT_ACTIVITY_WINDOW_SEC = 60.0
# 人工优先复检的放弃原因码（日志 `[autosend] abort=<code>`）
ABORT_REASONS = ("mode_changed", "agent_typing", "agent_sent", "risk_hold", "needs_human")
# Q-18 B（#292）：其中 agent_sent / agent_typing 两码 = **让位（defer）不是放弃**——单一来源
# 在 autosend_policy（诊断 / chip / 路由同口径）。日志 `[autosend] defer=<code> … until=` /
# `[autosend] resume=agent_window_passed` / `[autosend] resume by=mode_select`。
from src.inbox.autosend_policy import (
    AGENT_YIELD_MAX_DEFERRALS as _YIELD_MAX_DEFERRALS,
    YIELD_DEFER_REASONS as _YIELD_DEFER_REASONS,
    agent_yield_state as _agent_yield_state,
)

class UndeliveredError(RuntimeError):
    """投递结果为「数据形态失败」（{delivered:False,...}）时的异常载体。

    实施86 域B-1（#49）：编排器现随失败带回边车结构化字段——``error_kind``
    （send_backoff/account_blocked/…）与 ``retry_after_ms``（限频退避/临时冻结
    的确定性恢复时刻）。旧 RuntimeError(str) 会把它们碾成字符串，改期决策
    （见 _deliver_one 失败分支的 plan_failure_retry）就无从谈起。
    """

    def __init__(self, msg: str, *, error_kind: str = "",
                 retry_after_ms: int = 0) -> None:
        super().__init__(msg)
        self.error_kind = str(error_kind or "")
        self.retry_after_ms = int(retry_after_ms or 0)


def _is_permanent_send_error(err: str) -> bool:
    """判定投递错误是否为「短期内无法恢复」的平台硬错误（→ 会话封禁冷却）。

    2026-07-29 收敛：原本这里维护第三份永久错误词表（sender / proactive / 本处各一份，
    彼此漂移且无人察觉）。现委托 ``dead_peer_registry.classify_send_error`` 单一事实源，
    本处独有的四项（CHAT_ADMIN_REQUIRED / CHANNEL_PRIVATE / USER_BANNED_IN_CHANNEL /
    HAVE RIGHTS TO SEND）已并入该词表，故覆盖面只增不减。

    判据取「classify 命中任意 reason」而非仅 permanent：本处语义是「短期内发不出 →
    会话进**带 TTL 的**封禁冷却」，peer 失效（peer_unresolved）同样该进冷却
    （否则每条新入站都再拟稿→resolve→投递→失败空转，正是本机制要避免的）。
    写永久黑名单时才只认 permanent——见投递失败处的 registry 登记。
    """
    if not err:
        return False
    try:
        from src.ops.dead_peer_registry import classify_send_error
        return classify_send_error(err) is not None
    except Exception:
        return False


def _translate_hold_message(item: Dict[str, Any]) -> str:
    """出站翻译 HOLD 的失败原因文案（M-1 B #234，D-M3）。

    ``outbound_translate`` 把原因挂在 ``item["_xlate_hold"]``：
    - ``lang_unknown`` → 「客户语言未知，转人工确认」（手动/档案/消息/人设全无，D-M3
      要求转半自动、禁止自动投递——绝不落到操作员界面语言 zh）；
    - 其余（``target_lang_mismatch`` / ``engine_refusal`` / ``provider_unavailable`` /
      ``cjk_residue`` …）→ 「翻译失败待确认」，不发原文。
    旧回调（不挂原因）→ 通用文案。前缀 ``translate_hold:<reason>`` 稳定，供审计 /
    失败留痕气泡 / M-2 E 原因字段直接消费。
    """
    hold = item.get("_xlate_hold") if isinstance(item, dict) else None
    reason = str((hold or {}).get("reason") or "").strip() if isinstance(hold, dict) else ""
    if reason == "lang_unknown":
        return ("translate_hold:lang_unknown: 客户语言判不出（无手动设置/档案/消息证据/"
                "人设默认），已转人工确认，不自动投递（D-M3）")
    if reason:
        tgt = str((hold or {}).get("target") or "-")
        return (f"translate_hold:{reason}: 翻译失败待确认（target={tgt}），"
                "已拦截不发原文（无兜底纪律）")
    return "translate_hold: 出站翻译不可用，已拦截（无兜底纪律，不发原文）"


class AutosendWorker:
    """L2 草稿定时自动发后台任务。

    Usage::

        worker = AutosendWorker(draft_service=svc, config=cfg)
        task = asyncio.create_task(worker.run())
        # 关闭时：
        worker.stop()
        await task
    """

    def __init__(
        self,
        *,
        draft_service: Any,
        config: Optional[Dict[str, Any]] = None,
        send_callback: Optional[SendCallback] = None,
        human_send_callback: Optional[SendCallback] = None,
        translate_callback: Optional[TranslateCallback] = None,
        mark_read_callback: Optional[MarkReadCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        persona_resolver: Optional[Callable[[str, str], str]] = None,
        sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
        deliver_only: bool = False,
        dup_guard_cfg: Optional[Dict[str, Any]] = None,
        fresh_guard_cfg: Optional[Dict[str, Any]] = None,
        work_schedule_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        catchup_regenerate_cb: Optional[Callable[..., bool]] = None,
        pilot_guard: Optional[Callable[[str, str], bool]] = None,
        app: Any = None,
    ) -> None:
        cfg = config or {}
        # Q-23（#303）：stage=soft_reply 的人设口吻短生成要 app.state（skill_manager / ai_client /
        # config_manager）。None = 生成不可用 → 软回应一律 gen=skip 不发（不回落固定句）。
        self._app: Any = app
        self.total_soft_reply_delivered: int = 0
        self.total_soft_reply_errors: int = 0
        self.total_soft_reply_aborted: int = 0
        # deliver_only=True：本实例**只**作「人工通过→真投递」的载体，自动轮询循环
        # 压根不会 run()。用于 `l2_autosend.enabled=false` 的人审档部署——那种部署
        # 原本连 worker 都不创建，于是坐席点通过没有任何消费者（只标记不发送）。
        # 放同一个 `app.state.autosend_worker` 键是刻意的：autosend-status /
        # metrics 的 Prometheus gauge / 看门狗 / 报表都从那里读，塞这里可让人工投递
        # 的观测链零改动全通。歧义由本标记 + 快照里的 running/enabled 显式消掉。
        self._deliver_only: bool = bool(deliver_only)
        self._svc = draft_service
        self._enabled: bool = bool(cfg.get("enabled", True))
        # 真实投递回调（None=仅 DB 标记不发，保持旧行为；非 None=L2 草稿 resolve 后真投递）。
        # 由 main.py 在 inbox.l2_autosend.deliver=true 时注入，gating 在注入处。
        self._send_callback: Optional[SendCallback] = send_callback
        # 人工通过专用投递回调（2026-07-29）。为什么与自动链分开：`deliver` 开关管的是
        # **「AI 可否自己发」**，而坐席点「通过」是**人的明示决定**——手动发送端点
        # (/api/unified-inbox/send) 本就不受 deliver 约束，用它闸住人工通过自相矛盾，
        # 结果是「AI 拟稿 + 人审后发」这一**最谨慎、最常被推荐的档位**里发送按钮空转
        # （坐席以为发了、客户什么也没收到）。故 deliver=false 部署也注入真发回调，
        # 只有自动循环仍受 deliver 约束。None → 回落 _send_callback（deliver=true 时同一个）。
        self._human_send_callback: Optional[SendCallback] = human_send_callback
        # 出站翻译回调（None=投递原文，保持旧行为；非 None=投递前把 AI 中文译成客户语言）。
        # 由 main.py 在 inbox.l2_autosend.translate.enabled=true 且 translation_service 可用时注入。
        self._translate_callback: Optional[TranslateCallback] = translate_callback
        # 已读回调（None=不补已读，旧行为；非 None=投递前先给会话发平台「已读」回执，
        # 再进入拟人打字延迟——对端视角：已读 → 停顿数秒 → 收到回复，与真人一致）。
        self._mark_read_callback: Optional[MarkReadCallback] = mark_read_callback
        # 打字状态回调（None=不挂打字状态，旧行为；非 None=打字延迟期间周期挂「正在输入」）。
        self._typing_callback: Optional[TypingCallback] = typing_callback
        # 拟人链运行时开关（P1 2026-08-02）：回调「有没有」是装配期能力（deliver 模式
        # 才建），「用不用」是运营开关——两者此前都冻在构造期，设置页改
        # mark_read_before_reply / typing_indicator 只能等重启。现在开关由 worker
        # 自持（apply_humanize_flags 可热更），bootstrap 以 always=True 装配回调。
        self._mark_read_enabled: bool = bool(cfg.get("mark_read_before_reply", True))
        self._typing_enabled: bool = bool(cfg.get("typing_indicator", True))
        # 平台拟人开关覆写（P1 2026-08-03）：{platform: {mark_read?, typing?}}。
        # 显式 true/false 覆盖全局开关（全局关时也可单平台开）；缺省=跟随全局。
        # 与 deliver_delay 同款「构造期拷贝 + 热更入口」（apply_platform_humanize）。
        self._platform_humanize: Dict[str, Dict[str, Any]] = \
            self._norm_platform_humanize(cfg.get("platform_humanize"))
        # 人设解析器（None=不按人设化节奏，用 block 顶层默认；非 None=按 (platform,account_id)
        # 解析 persona_id → 合并 deliver_delay.persona_overrides + 观测按人设分维）。
        self._persona_resolver: Optional[Callable[[str, str], str]] = persona_resolver
        # send_callback 是否接受 original_text kwarg（首次投递时探测并缓存）。
        self._send_cb_accepts_original: Optional[bool] = None
        self._min_interval: float = float(cfg.get("min_interval_sec", 60))
        self._max_interval: float = float(cfg.get("max_interval_sec", 600))
        # 首次启动延迟（默认=min_interval 保持兼容）。可被新 L2 草稿事件提前唤醒，
        # 因此全自动首条回复实际延迟 ≈ 草稿落库瞬间，而非固定等满此值。
        self._startup_delay: float = float(cfg.get("startup_delay_sec", self._min_interval))
        self._circuit_threshold: int = int(cfg.get("circuit_threshold", 5))
        self._cooldown_sec: float = float(cfg.get("cooldown_sec", 300))

        # Phase 4：投递前拟人延迟（模拟打字，降低秒回露馅/反封号）。
        # config.inbox.l2_autosend.deliver_delay = {min_sec, max_sec, adaptive?}；
        # 默认 0=不延迟（向后兼容）。adaptive=true 时按回复内容长度/情绪自适应
        # （见 humanize.compute_pacing_delay），否则 uniform(min,max)。原始块整体留存，
        # 交由 compute_pacing_delay 统一解析（单一装配点，与原生 A 线回复共用）。
        self._deliver_delay_block: Dict[str, Any] = dict(cfg.get("deliver_delay") or {})
        # P1（2026-08-12）：同会话连发最小间隔地板的进程内账本
        # {conversation_id: 最近一次成功出站 ts}。修「第 2/3 条秒回」：串行/并行队列里
        # 后续草稿的 adaptive 抵扣把排队等待算成已耗时 → 延迟归零 → 同一客户收到
        # 背靠背机关枪。地板只看「距本会话上一条出站多久」（deliver_delay.min_gap_sec，
        # 默认 0=关），与抵扣正交。进程内即可：跨重启的首条本就有全额延迟。
        self._last_conv_sent: Dict[str, float] = {}
        self.total_gap_floored: int = 0
        # 并行投递（2026-08-09，默认关）：拟人节奏上线后单条投递可占 30-54s 延迟
        # + 至多 75s 分条间隔，串行循环下并发会话互相排队。开启后按会话分组并发
        # （同会话保序），见 _deliver_parallel。max_concurrent 夹 [1,8] 防误配。
        _par_cfg = cfg.get("parallel_deliver") or {}
        self._parallel_enabled: bool = bool(_par_cfg.get("enabled", False))
        try:
            self._parallel_max: int = max(1, min(8, int(
                _par_cfg.get("max_concurrent", 3))))
        except (TypeError, ValueError):
            self._parallel_max = 3
        self.parallel_batches: int = 0
        # 可注入 sleep（测试用）；生产用 asyncio.sleep
        self._sleep: Callable[[float], Awaitable[Any]] = sleep or asyncio.sleep

        # 会话级发送封禁：投递遇「永久性」错误（群聊无发言权/被拉黑/会话失效）时，把该
        # 会话列入冷却黑名单——冷却窗口内的 pending 草稿直接取消（不 resolve/投递/刷屏），
        # 冷却到期自动重探（权限恢复即回正常）。0=关闭该机制（旧行为）。
        try:
            self._send_block_cooldown_sec: float = float(
                cfg.get("send_block_cooldown_sec", 21600) or 0)  # 默认 6h
        except (TypeError, ValueError):
            self._send_block_cooldown_sec = 21600.0
        self._blocked_conv_until: Dict[str, float] = {}

        # Sprint2 可恢复投递（默认关 → 保持「宁丢不重发」，零行为变更）。
        # 瞬时投递失败 → 进程内重试队列（指数退避），重发同文本、不 re-resolve（幂等）；
        # 永久失败/重试耗尽 → record_autosend_failure + event 提醒坐席补发。
        _rc = dict(cfg.get("recoverable") or {})
        self._recoverable: bool = bool(_rc.get("enabled", False))
        self._retry_max_attempts: int = int(_rc.get("max_attempts", 5))
        self._retry_backoff_base: float = float(_rc.get("backoff_base_sec", 30))
        self._retry_backoff_max: float = float(_rc.get("backoff_max_sec", 1800))
        self._retry_queue: List[Dict[str, Any]] = []  # [{item, next_ts}]
        # 实施86 域B-1：按边车 retry_after_ms 提示改期的次数（与通用重试分开计，
        # 「改了几次期」与「盲重试几次」是两个信号）
        self.total_deferred: int = 0

        # 运行时状态
        self._running = False
        self._current_interval = self._min_interval

        # C3：事件驱动——asyncio.Event（run() 内初始化，保证在正确的 loop 上）
        self._l2_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 熔断器
        self._consecutive_errors = 0
        self._circuit_open = False
        self._circuit_open_ts: float = 0.0

        # H3：草稿清理配置
        self._cleanup_age_days: int = int(cfg.get("cleanup_age_days", 7))
        self._cleanup_enabled: bool = bool(cfg.get("cleanup_enabled", True))
        self._last_cleanup_ts: float = 0.0
        self._cleanup_interval: float = 86400.0  # 每日执行一次

        # 指标
        self.total_sent: int = 0
        self.total_errors: int = 0
        self.last_run_ts: float = 0.0
        self.last_sent: int = 0
        self.last_error: str = ""
        self.cycles: int = 0
        self.event_triggers: int = 0  # C3：记录事件驱动触发次数
        self.total_cleaned: int = 0   # H3：历史清理草稿总数
        self.total_delivered: int = 0       # 真正投递到平台的条数
        self.total_deliver_errors: int = 0  # 投递失败条数（已 resolve 但平台发送失败）
        self.total_translated: int = 0      # 投递前出站翻译生效（译文≠原文）的条数
        self.total_skipped_blocked: int = 0  # 因会话发送封禁而跳过取消的草稿数
        self.total_skipped_mode: int = 0     # 因会话被显式降级(接管→manual 等)而取消的 L2 数
        self.total_marked_read: int = 0      # 投递前成功补发平台已读回执的条数
        self.total_retry_scheduled: int = 0  # 瞬时投递失败重排重试次数（recoverable）
        self.total_retry_recovered: int = 0  # 重试后成功投递条数
        self.total_retry_exhausted: int = 0  # 重试耗尽最终放弃条数
        self.total_skipped_raced: int = 0    # resolve 撞闸门（已被人工/他方处置）跳过数
        self.total_human_delivered: int = 0      # 人工通过草稿经本 worker 真投递成功数
        self.total_human_deliver_errors: int = 0  # 人工通过草稿投递失败数
        self.total_dup_blocked: int = 0          # 出站近重复守卫拦截数（不算投递错误）
        self.total_dup_rewritten: int = 0        # 拦截后换说法重试成功数（impl85 阶段3）
        # #144（2026-09-02）拆计数：同轮双发拦截 / 跨轮拦截（仅 dup 档原样复读）/
        # 跨轮相近被 since 边界放行（修前会被误拦的那部分）/ 拦下后打「需人工」数
        self.total_dup_blocked_same_round: int = 0
        self.total_dup_blocked_cross_round: int = 0
        self.total_dup_cross_round_released: int = 0
        self.total_dup_blocked_flagged: int = 0
        self.total_superseded: int = 0           # 新入站过期守卫跳过数（fresh_guard，不算 error）
        # 工作时间闸扣留事件数（同一草稿每 tick 重扫会重复计数——这是「扣留中」的
        # 活动信号而非唯一草稿数；复班后自然归零增长）
        self.total_skipped_off_hours: int = 0
        # O-1 D（D-O4）首次接触 / 沉寂 >6h 首回延 1–5 min：留 pending 的唯一草稿数
        # （按 draft_id 去重，与 off_hours 的「活动信号」计数口径不同）
        self.total_first_reply_held: int = 0
        self._first_reply_held_ids: set = set()
        self.total_catchup_regenerated: int = 0  # 复班补觉：作废陈稿并重拟的条数
        self.total_skipped_pilot: int = 0  # 驾驶权在原生面板而取消的 L2 数（surface_fusion）
        # B41（2026-08-22）：投递幂等钉拒绝的重复投递数（同稿在途/已投过）
        self.total_skipped_already_sent: int = 0
        # B41：通道互斥仲裁取消的跟进链稿数（同会话同批常规稿在场，链稿让位）
        self.total_skipped_mutex: int = 0
        # O-1 A（#252 #253）：会话已因停联/自伤冻结而取消的 L2 稿数（告别稿不计）
        self.total_skipped_stop_contact: int = 0
        # M-2（D-M1 / #232 / #233）：账号级通道门禁扣住次数（每 tick 重扫会重复计，
        # 是「扣留中」活动信号）/ 账号因连续失败被降半自动的次数 / 门禁日志节流表
        self.total_skipped_channel_gate: int = 0
        self.total_account_degraded: int = 0
        self.total_retry_gated: int = 0   # #233：改期到点但账号仍在退避/门禁 → 再排不重投
        self._gate_log_ts: Dict[str, float] = {}
        # B41 投递权登记表（实例级，人工/自动两条投递链共用——见 deliver_once 模块
        # docstring 的作用域论证）
        self._deliver_once = DeliverOnceRegistry()

        # 驾驶权互斥锁 guard（surface_fusion P0，2026-08-13，默认不注入=零行为变更）：
        # (platform, account_id) -> bool。True＝该账号自动化持有者是「原生面板」，
        # 工作台自动链让位（取消草稿防双发，人工链不受此闸）。判定 fail-open。
        self._pilot_guard: Optional[Callable[[str, str], bool]] = pilot_guard

        # 出站近重复守卫（2026-08-02）：与最近出站（DB 镜像 + 进程级在途登记表）比对，
        # 命中即静默跳过——防「客户连发多条 → 两次独立生成互不知情 → 同义双发」。
        # 配置由 bootstrap 经 resolve_guard_cfg 注入（本 worker 拿不到全局配置树，
        # 与 _dead_peer_shared 同理）；None/enabled=false = 零行为变更。
        self._dup_guard_cfg: Dict[str, Any] = dict(dup_guard_cfg or {})

        # 新入站过期守卫（fresh_guard，2026-08-03，默认关）：拟稿窗口里客户又说了话
        # → 旧稿答非所问且新稿马上会再发一条。配置优先取 bootstrap 注入（完整树解析，
        # 含 auto_draft.min_text_len 镜像——与 dup_guard_cfg 同一注入范式）；未注入时
        # 从本 cfg 块自解析（enabled/grace 可用）。解析失败按关闭，绝不影响构造。
        try:
            from src.inbox.draft_fresh_guard import parse_fresh_guard_cfg
            self._fresh_guard_cfg: Dict[str, Any] = (
                dict(fresh_guard_cfg) if isinstance(fresh_guard_cfg, dict)
                else parse_fresh_guard_cfg(cfg))
        except Exception:
            self._fresh_guard_cfg = {"enabled": False}

        # 工作时间班表（inbox.work_schedule，2026-08-04，默认关）：账号休息中
        # 的 L2 草稿**留 pending 不处置**（复班自动接续），危机消息穿透照发。
        # provider=每次调用活读 config 根的闭包（班表改动经 overlay 热重载即生效，
        # 与 dup/fresh 的「构造期冻结」刻意不同——作息是运营高频调的东西）；
        # None = 未接线 = 零行为变更。判定全程 fail-open（见 work_hours_gate）。
        self._ws_provider: Optional[Callable[[], Dict[str, Any]]] = \
            work_schedule_provider
        # 复班补觉重拟回调（P1）：(conv_dict, peer_text) -> bool（True=已派发重拟）。
        # 由 bootstrap 在 auto_draft 装配完成后经 set_catchup_regenerate_cb 注入
        # （worker 先于 auto_draft 构造，构造期拿不到）；未注入 = 不作废不重拟，
        # 陈稿按旧行为原样投递——绝不允许「作废了却没人重拟」的静默丢回复。
        self._catchup_regen_cb: Optional[Callable[..., bool]] = \
            catchup_regenerate_cb

        # Q-3（#264 C/D，2026-09-10）人工优先：在途稿登记表（draft_id → 载荷，从进入拟人
        # 等待到真发前）+ 取消信号（draft_id → by）+ 坐席打字 / 发送时刻（conv → ts）。
        # 切档路由 / 坐席打字端点 / 坐席发送 经 cancel_inflight / note_agent_* 写入；
        # _deliver_one 在拟人等待结束、真发之前经 _human_priority_gate 复检一次。
        self._inflight: Dict[str, Dict[str, Any]] = {}
        self._inflight_cancel: Dict[str, str] = {}
        self._agent_typing_ts: Dict[str, float] = {}
        self._agent_sent_ts: Dict[str, float] = {}
        self._hp_lock = threading.Lock()
        self.total_abort_recheck: int = 0          # 真发前二次复检放弃的条数（含在途取消）
        self.total_inflight_cancelled: int = 0     # cancel_inflight 点名取消的在途 / 排队稿数
        self.total_skipped_risk_hold: int = 0      # 捞稿期因会话级风险持有 / 需人工取消的 L2 数
        self.total_risk_hold_regen: int = 0        # 取消后按 L1 重拟派发数
        self._risk_hold_regen_done: Dict[str, float] = {}   # conv → 已重拟过的 hold set_ts
        # Q-18 B（#292）：让位登记表 conv → {by, until, since, draft_id, stage, deferrals}——
        # batch 期稿留 pending（下一 tick 自然复检）、presend 期已 resolve 的载荷进 _retry_queue
        # 按 until 改期（item 带 _yield_defer）；窗过复检放行时 pop 并落 resume 日志。
        self._yield_defer: Dict[str, Dict[str, Any]] = {}
        self._yield_wake_handles: Dict[str, Any] = {}    # conv → loop.call_later 句柄（窗过唤醒）
        self.total_yield_deferred: int = 0         # defer 次数（同稿同窗只计一次）
        self.total_yield_resumed: int = 0          # 窗过 / 切全自动后放行次数
        self.total_yield_exhausted: int = 0        # 连续让位超上限按放弃处理的条数
        # Q-18 D（#293）：班表扣留进拦截台账的去重戳 conv → until_ts
        self._ledger_ws_stamp: Dict[str, float] = {}

    # ── 生命周期 ──────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _dead_peer_shared():
        """共享死 peer 登记表（只 **peek** 不创建）。

        刻意**不自己读 flag**：本 worker 收到的 config 只是 ``inbox.l2_autosend`` 子段，
        拿不到全局 ``ops.dead_peer_registry``；而这也不必要——registry 单例**只在 flag
        开启时**由持 ConfigManager 的 A 线 sender / proactive 建立，故「单例已存在」本身
        就是「flag 已开」的证据。同理不能自建：本 worker 拿不到落盘路径，自建会得到一个
        无路径纯内存实例、共享当场失效。未建立 → None（本地会话冷却照常，零破坏）。
        """
        try:
            from src.ops.dead_peer_registry import peek_dead_peer_registry
            return peek_dead_peer_registry()
        except Exception:
            return None

    @staticmethod
    def _platform_of_conv(conv: str) -> str:
        """从 conversation_id（``inbox:<platform>:<account>:<peer>``）取平台名。

        取不到 → telegram（registry 的默认命名空间，与 A 线口径一致）。
        """
        parts = str(conv or "").split(":")
        return parts[1] if len(parts) >= 3 and parts[1] else "telegram"

    def _dead_peer_blocked(self, conv: str) -> bool:
        """该对端是否已被**任一发送链**确证为永久不可达（跨链共享的读侧）。"""
        reg = self._dead_peer_shared()
        if reg is None or not conv:
            return False
        try:
            # peer 归一化在 registry 内部（conversation_id 取末段），与 A 线裸 chat_id 同 key
            return bool(reg.is_blocked(self._platform_of_conv(conv), conv))
        except Exception:
            return False

    def _dead_peer_clear(self, conv: str) -> None:
        """#88：真实送达=可达铁证 → 清共享表标记 + 本地封禁冷却（no-op 安全）。

        0830 skuio 实锤：Yhang 会话 AI 自动投递 18:38 双勾送达，黄条「曾被
        对方拉黑」仍常驻——#73 的清标钩子只挂了手动路由与 A 线主动外发，
        B 线（自动投递 + 人审通过投递）送达成功从不清标。本地
        ``_blocked_conv_until`` 同清：真送达面前旧封禁冷却已无意义。
        """
        if not conv or conv == "?":
            return
        self._blocked_conv_until.pop(conv, None)
        reg = self._dead_peer_shared()
        if reg is None:
            return
        try:
            if reg.unblock(self._platform_of_conv(conv), conv):
                logger.info(
                    "[AutosendWorker] 送达成功，已解除 dead-peer 标 conv=%s"
                    "（自动回复恢复，黄条随下轮 send-caps 消失）", conv)
        except Exception:
            pass

    def _dead_peer_record(self, conv: str, err: str) -> None:
        """把**永久**不可达登记进共享表（可自愈类由 registry 内部忽略）。"""
        reg = self._dead_peer_shared()
        if reg is None or not conv or conv == "?":
            return
        try:
            from src.ops.dead_peer_registry import classify_send_error
            reason = classify_send_error(err)
            if reason and reg.record(self._platform_of_conv(conv), conv, reason):
                logger.info(
                    "[AutosendWorker] 已登记死 peer conv=%s（%s，跨链共享不再重发）",
                    conv, reason)
        except Exception:
            logger.debug("[AutosendWorker] 死 peer 登记失败（已忽略）", exc_info=True)

    # ── M-2 账号级通道门禁（D-M1 / #232 / #233，2026-09-06）────────────────────
    def _account_gate_hold(self, platform: str, account_id: str) -> str:
        """该账号此刻自动投递是否该扣住：cooldown / degraded / backoff / disconnected / ""。

        冷静期 = 登录/重登后 10 分钟只起草；degraded = 连续 3 次真实失败已降半自动
        （要人确认）；backoff = 边车 send_backoff/account_blocked 水位未过（#233：
        退避期内自动不再改期重投续命）；disconnected = 通道未连接（#232）。
        同账号同原因 60s 只落一条 INFO。判定异常一律放行（门禁不能成为回复链故障点）。
        """
        plat = str(platform or "")
        acct = str(account_id or "default")
        if not plat or not acct:
            return ""
        reason = ""
        try:
            from src.inbox.account_channel_gate import hold_reason
            reason = hold_reason(plat, acct)
            if not reason:
                from src.integrations.platform_session_health import (
                    channel_connection_state,
                )
                if channel_connection_state(plat, acct).get("state") == "disconnected":
                    reason = "disconnected"
        except Exception:
            logger.debug("[AutosendWorker] 账号门禁判定异常（放行）", exc_info=True)
            return ""
        if not reason:
            return ""
        key = f"{plat}:{acct}:{reason}"
        now = time.time()
        if now - self._gate_log_ts.get(key, 0.0) >= 60.0:
            self._gate_log_ts[key] = now
            logger.info(
                "[AutosendWorker] guard=channel_gate 账号 %s:%s 自动投递扣住（%s）——"
                "草稿留 pending 待人过目/通道恢复后接续（D-M1 / #232 / #233）",
                plat, acct, reason)
        return reason

    def _account_gate_wait(self, platform: str, account_id: str) -> float:
        """门禁扣住的重投再排多久：退避/冷静期剩余取大，降级/未连接按 30s 复查（夹 5–900s）。"""
        try:
            from src.inbox.account_channel_gate import (
                backoff_remaining, cooldown_remaining,
            )
            wait = max(backoff_remaining(platform, account_id),
                       cooldown_remaining(platform, account_id), 30.0)
        except Exception:
            wait = 30.0
        return max(5.0, min(900.0, float(wait))) * (0.9 + 0.2 * random.random())

    def _account_gate_note(self, item: Dict[str, Any], *, ok: bool,
                           error_kind: str = "", retry_after_ms: int = 0) -> None:
        """投递结果记进账号门禁：成功清连续失败/退避；失败计连续失败（退避类只记水位）。

        连续 3 次真实失败 → gate 内部降半自动 + 红标（封顶层 channel_degraded 生效，
        本 worker 下一 tick 起按 _account_gate_hold 扣住）。best-effort 绝不抛。
        """
        try:
            from src.inbox.account_channel_gate import note_send_fail, note_send_ok
            plat = str(item.get("platform") or "")
            acct = str(item.get("account_id") or "default")
            if ok:
                note_send_ok(plat, acct, manual=False)
                return
            res = note_send_fail(plat, acct, error_kind=error_kind,
                                 retry_after_ms=int(retry_after_ms or 0))
            if res.get("degraded_now"):
                self.total_account_degraded += 1
        except Exception:
            logger.debug("[AutosendWorker] 账号门禁记账失败（忽略）", exc_info=True)

    def _conv_send_blocked(self, conv: str) -> bool:
        """该会话是否处于发送封禁冷却窗口内（到期自动清理并允许重探）。

        2026-07-29 起叠加**共享死 peer 登记表**（gated，默认关）：A 线 sender /
        proactive 已确证永久不可达的对端，本线也不再投递——此前三条链各持一份黑名单，
        一条链拉黑的死号另两条还在打（实录：已注销号 2h 内被重试 17 次）。
        共享查询独立于 ``_send_block_cooldown_sec``（后者=0 也要拦已确证的死 peer）。
        """
        if self._dead_peer_blocked(conv):
            return True
        if not conv or self._send_block_cooldown_sec <= 0:
            return False
        until = self._blocked_conv_until.get(conv, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            self._blocked_conv_until.pop(conv, None)
            return False
        return True

    def _dup_guard_rows(self, conv: str) -> List[Dict[str, Any]]:
        """近重复比对集：DB 出站镜像 + 进程级在途登记表（防御式，异常回空）。"""
        rows: List[Dict[str, Any]] = []
        if not conv:
            return rows
        store = getattr(self._svc, "_store", None)
        if store is not None:
            try:
                rows = list(store.list_recent_messages(conv, limit=8) or [])
            except Exception:
                rows = []
        try:
            from src.inbox.outbound_dup_guard import outbound_registry
            rows.extend(outbound_registry.recent_rows(conv))
        except Exception:
            logger.debug("[AutosendWorker] 在途登记表读取失败（忽略）", exc_info=True)
        return rows

    # 「本稿所答的最新入站」判定的时钟垫（#144）：入站 ts 来自平台（WA 秒级 /
    # 服务端时钟），草稿 created_ts 是本机 time.time()——允许入站比拟稿「晚」这么
    # 几秒仍算本稿所答的那条，防时钟偏差把 since 边界错退到上一轮入站。
    _DUP_SINCE_GRACE_SEC = 2.0

    def _dup_guard_report(self, item: Dict[str, Any],
                          send_text: str) -> Dict[str, Any]:
        """出站近重复判定全报告：DB 出站镜像 + 进程级在途登记表合并比对。

        返回 ``{hit, cross_round_similar, since_ts, rows}``。``since_ts``＝本稿所答
        的最新入站 ts（#144：similar 档只比对它之后的出站；无入站/无 created_ts
        参照 → 0＝不筑边界，旧行为）。防御式：store 缺失/查询异常按「不命中」
        （守卫绝不阻断正常投递）。
        """
        empty: Dict[str, Any] = {"hit": None, "cross_round_similar": None,
                                 "since_ts": 0.0, "rows": []}
        conv = str(item.get("conversation_id") or "")
        if not conv:
            return empty
        try:
            from src.inbox.outbound_dup_guard import (
                latest_inbound_ts,
                near_duplicate_report,
            )
            rows = self._dup_guard_rows(conv)
            created = float(item.get("created_ts") or 0.0)
            since = latest_inbound_ts(
                rows,
                before_ts=(created + self._DUP_SINCE_GRACE_SEC)
                if created > 0 else None)
            rep = near_duplicate_report(
                send_text, rows,
                window_sec=float(self._dup_guard_cfg.get("window_sec", 180.0)),
                similar_since_ts=since or None,
            )
            rep["since_ts"] = since
            rep["rows"] = rows
            return rep
        except Exception:
            logger.debug("[AutosendWorker] 近重复守卫异常（放行）", exc_info=True)
            return empty

    def _dup_guard_check(self, item: Dict[str, Any],
                         send_text: str) -> Optional[Dict[str, Any]]:
        """兼容薄壳：只回命中（见 ``_dup_guard_report``）。"""
        return self._dup_guard_report(item, send_text).get("hit")

    async def _try_dup_rewrite(
        self, item: Dict[str, Any], send_text: str,
        hit: Optional[Dict[str, Any]], *,
        since_ts: Optional[float] = None,
        rows: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """dup 拦截后「换个说法」重试（impl85 阶段3）。返回通过再核的重写稿或 None。

        rewrite_fn 由装配层放进 dup_guard_cfg（bootstrap / support_kwargs 注入
        ``rewrite_fn`` 闭包，内用 ai_client.rewrite_local）；未注入＝维持旧行为。
        ``since_ts``/``rows`` 与首检同值（#144：再核不得比首检更严）。
        绝不抛（拦截语义只可能维持，不可能因重试异常放行原文）。
        """
        try:
            from src.inbox.outbound_dup_guard import attempt_dup_rewrite
            conv = str(item.get("conversation_id") or "")
            return await attempt_dup_rewrite(
                text=send_text, hit=hit,
                rows=list(rows) if rows is not None else self._dup_guard_rows(conv),
                cfg=self._dup_guard_cfg,
                rewrite_fn=self._dup_guard_cfg.get("rewrite_fn"),
                source="autosend",
                similar_since_ts=since_ts or None,
                conv_id=conv)
        except Exception:
            logger.debug("[AutosendWorker] dup 重写重试异常（维持跳过）",
                         exc_info=True)
            return None

    def _dup_blocked_escalate(
        self, item: Dict[str, Any], hit: Optional[Dict[str, Any]],
        rows: Optional[List[Dict[str, Any]]],
    ) -> bool:
        """dup 拦截且重写没救回 → 绝不静默收尾（#144）。返回是否打了「需人工」。

        客户有新入站在等（会话最新一条是入站）＝拦下即客户零回复：给会话打
        「需人工」标（reason=dup_guard_blocked，chip 悬停可见原因）进待处理清单，
        并发 ops 告警；最新一条已是出站（客户已有回复）→ 只留 WARNING。
        全程 best-effort：打标失败也只记日志，绝不影响投递链。
        """
        conv = str(item.get("conversation_id") or "")
        waiting = False
        try:
            from src.inbox.outbound_dup_guard import peer_awaiting_reply
            waiting = peer_awaiting_reply(rows)
        except Exception:
            waiting = False
        if not waiting:
            logger.warning(
                "[AutosendWorker] guard=near_duplicate 拦截后未救回，客户最新一条"
                "已是出站（已有回复），本轮静默 conv=%s draft=%s",
                conv, item.get("draft_id", ""))
            return False
        tagged = False
        try:
            store = getattr(self._svc, "_store", None)
            if store is not None:
                from src.integrations.protocol_autoreply import tag_needs_human
                tagged = bool(tag_needs_human(store, {
                    "platform": str(item.get("platform") or ""),
                    "account_id": str(item.get("account_id") or "default"),
                    "chat_key": str(item.get("chat_key") or ""),
                }, reason="dup_guard_blocked", source="system"))
        except Exception:
            logger.debug("[AutosendWorker] dup 拦截打「需人工」失败（忽略）",
                         exc_info=True)
        if tagged:
            self.total_dup_blocked_flagged += 1
        logger.warning(
            "[AutosendWorker] guard=near_duplicate 拦截后未救回且客户有新入站在等 "
            "→ %s conv=%s draft=%s level=%s sim=%.2f matched=%r",
            "已打「需人工」(reason=dup_guard_blocked)" if tagged
            else "「需人工」已在/打标不可用，仅留痕",
            conv, item.get("draft_id", ""),
            (hit or {}).get("level", ""),
            float((hit or {}).get("similarity") or 0.0),
            str((hit or {}).get("matched_text", ""))[:60])
        try:
            from src.ops.ops_alert import notify as _ops_notify
            _ops_notify(
                "dup_guard_blocked",
                f"⚠️ 出站近重复拦截后客户无回复 conv={conv}"
                f"（level={(hit or {}).get('level', '')}，已进待处理清单）",
                account_id=conv, reason="dup_guard_blocked")
        except Exception:
            logger.debug("[AutosendWorker] dup 拦截 ops_alert 失败（忽略）",
                         exc_info=True)
        return tagged

    def apply_deliver_delay(self, block: Optional[Dict[str, Any]]) -> None:
        """运行时热更新拟人打字延迟配置（「自动回复设置」页保存后即时生效）。

        ``_deliver_delay_block`` 在构造时从 config 拷贝了一份——写 overlay 后
        config 热重载不会传导到这里，若不提供本入口，设置页改「回复速度」就成了
        「已保存但线上没变」的静默失真（本仓 CWD 相对路径同款病）。传完整块
        （含 persona_overrides 等本页不管的键），替换语义与构造时一致。"""
        self._deliver_delay_block = dict(block or {})

    def apply_humanize_flags(
        self, *, mark_read: Optional[bool] = None, typing: Optional[bool] = None,
    ) -> None:
        """运行时热更新拟人链开关（已读回执 / 打字气泡），None=不动该项。

        与 ``apply_deliver_delay`` 同一模式：开关构造期从 cfg 拷贝、写 overlay
        传导不到，设置页保存后由路由调本入口即时生效。只改「用不用」——回调
        本体（「有没有」，deliver 模式装配）不在此处变更。"""
        if mark_read is not None:
            self._mark_read_enabled = bool(mark_read)
        if typing is not None:
            self._typing_enabled = bool(typing)

    @staticmethod
    def _norm_platform_humanize(raw: Any) -> Dict[str, Dict[str, Any]]:
        """归一平台拟人覆写表：平台键小写、条目须为 dict（其余静默丢弃）。"""
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[str(k).lower()] = dict(v)
        return out

    def apply_platform_humanize(self, block: Optional[Dict[str, Any]]) -> None:
        """运行时热更新平台拟人开关覆写表（「自动回复设置」页保存后即时生效）。

        整表替换语义（与构造时一致）：删掉的平台真被删掉、回到跟随全局。"""
        self._platform_humanize = self._norm_platform_humanize(block)

    def _humanize_flag(self, platform: str, key: str) -> bool:
        """某平台的拟人开关生效值：平台显式覆写 > 全局开关（缺省=跟随全局）。"""
        base = (self._mark_read_enabled if key == "mark_read"
                else self._typing_enabled)
        try:
            v = (self._platform_humanize.get(
                str(platform or "").lower()) or {}).get(key)
        except Exception:
            v = None
        return base if v is None else bool(v)

    def runtime_pacing_snapshot(self) -> Dict[str, Any]:
        """线上实际生效的节奏/拟人参数（设置页「保存后回读验证」用）。

        与 status_snapshot 的区别：这里回的是 worker **内存里正在用**的值——
        「overlay 写成功 + 热更调成功」之后，UI 拿它对账「线上真的变了」，
        闭合「已保存但没生效」的信任缺口。"""
        return {
            "deliver_delay": dict(self._deliver_delay_block),
            "mark_read_enabled": (
                self._mark_read_callback is not None and self._mark_read_enabled),
            "typing_enabled": (
                self._typing_callback is not None and self._typing_enabled),
            "mark_read_capable": self._mark_read_callback is not None,
            "typing_capable": self._typing_callback is not None,
            "platform_humanize": {
                k: dict(v) for k, v in self._platform_humanize.items()},
        }

    def _note_conv_sent(self, conversation_id: str) -> None:
        """登记本会话一次成功出站（min_gap_sec 地板的判据；自动/人工链同账本）。

        账本有界：超 512 条时剔除 1h 前的旧条目（地板只关心几十秒尺度，1h 前
        的记录对判定恒为「间隔已够」，删了语义不变）。
        """
        conv = str(conversation_id or "")
        if not conv:
            return
        now = time.time()
        self._last_conv_sent[conv] = now
        if len(self._last_conv_sent) > 512:
            cutoff = now - 3600.0
            self._last_conv_sent = {
                k: v for k, v in self._last_conv_sent.items() if v >= cutoff}

    def _since_conv_sent(self, conversation_id: str) -> Optional[float]:
        """距本会话上一条出站的秒数；进程内无记录 → None（首条不垫地板）。"""
        ts = self._last_conv_sent.get(str(conversation_id or ""))
        if not ts:
            return None
        return max(0.0, time.time() - ts)

    def _pick_deliver_delay(
        self, text: str = "", elapsed_sec: float = 0.0, persona_id: str = "",
        platform: str = "", conversation_id: str = "", inbound_text: str = "",
    ) -> float:
        return self._pick_deliver_pacing(
            text, elapsed_sec, persona_id=persona_id, platform=platform,
            conversation_id=conversation_id, inbound_text=inbound_text).delay

    def _pick_deliver_pacing(
        self, text: str = "", elapsed_sec: float = 0.0, persona_id: str = "",
        platform: str = "", conversation_id: str = "", inbound_text: str = "",
    ):
        """按 deliver_delay 配置取本次拟人延迟（秒）。统一走 resolve_pacing：
        先合并覆写层（人设 > 平台 > 全局，见 humanize._apply_scoped_overrides）；
        adaptive=false→uniform(min,max)（旧行为）；adaptive=true→按回复内容长度/
        激活度估时并扣除 ``elapsed_sec`` 已耗时。未配置/非法 → 0。
        P1（2026-08-12）：再过同会话连发地板 ``min_gap_sec``（见 apply_min_gap_floor
        ——adaptive 把队列等待当已耗时抵扣对单条是对的，但连续两条出站之间必有
        真人打字间隔；地板与抵扣正交，默认 0=关）。
        顺带按 平台/人设 分维记录节奏观测（best-effort；路径
        ``autosend/{platform|-}/{persona|-}``，旧单段格式的前端解析已兼容两代；
        记录的是**过完地板的最终延迟**——校准参数要看真实等待分布）。"""
        from src.inbox.humanize import (
            apply_min_gap_floor,
            resolve_min_gap_sec,
            resolve_pacing,
        )
        r = resolve_pacing(
            self._deliver_delay_block, text=text, elapsed_sec=elapsed_sec,
            persona_id=persona_id, platform=platform, inbound_text=inbound_text)
        delay = r.delay
        gap = resolve_min_gap_sec(
            self._deliver_delay_block, platform=platform, persona_id=persona_id)
        if gap > 0 and conversation_id:
            delay, floored = apply_min_gap_floor(
                delay,
                since_last_send_sec=self._since_conv_sent(conversation_id),
                min_gap_sec=gap)
            if floored:
                self.total_gap_floored += 1
                r = replace(r, delay=delay, floored=True)
        try:
            from src.integrations.humanize_metrics import record_pacing
            record_pacing(
                f"autosend/{platform or '-'}/{persona_id or '-'}", r)
        except Exception:
            pass
        return r

    def _first_reply_remaining(self, d: Dict[str, Any], conversation_id: str) -> float:
        """O-1 D 首回延迟：本稿还需留 pending 多少秒（0 ＝ 不延 / 已到点）。

        判据（纯函数在 humanize）：``deliver_delay.first_reply`` 开着（缺省＝块带 profile）
        且本轮入站之前无出站（首次接触）或距上一条出站 ≥ silence_hours → hold =
        沉寂后首条入站 ts + 60–300s（按 会话#首条入站 ts 确定性取值）。危机消息不延。
        首次判成 hold 时落一行 ``[pacing] first_reply hold …``（每稿一次）。
        """
        from src.inbox.humanize import (
            first_reply_hold_sec,
            resolve_first_reply_cfg,
            silence_before_inbound,
        )
        _plat = str(d.get("platform") or "")
        _acct = str(d.get("account_id") or "default")
        _pid = ""
        if self._persona_resolver is not None:
            try:
                try:
                    _pid = str(self._persona_resolver(
                        _plat, _acct, str(d.get("chat_key") or "")) or "")
                except TypeError:
                    _pid = str(self._persona_resolver(_plat, _acct) or "")
            except Exception:
                _pid = ""
        cfg = resolve_first_reply_cfg(
            self._deliver_delay_block, platform=_plat, persona_id=_pid)
        if not cfg.get("enabled"):
            return 0.0
        store = getattr(self._svc, "_store", None)
        if store is None or not hasattr(store, "list_recent_messages"):
            return 0.0
        draft_ts = float(d.get("created_ts") or d.get("created_at") or 0)
        if draft_ts <= 0:
            return 0.0
        rows = store.list_recent_messages(conversation_id, limit=12)
        silence, burst_start = silence_before_inbound(rows, draft_ts=draft_ts)
        hold = first_reply_hold_sec(
            cfg, silence_sec=silence, key=f"{conversation_id}#{int(burst_start)}")
        if hold <= 0:
            return 0.0
        peer_text = str(d.get("peer_text") or "")
        if peer_text.strip():
            try:
                from src.utils.wellbeing_guard import detect_crisis
                lvl = str((detect_crisis(peer_text) or {}).get("level") or "none")
                if lvl in ("severe", "elevated"):
                    return 0.0
            except Exception:
                return 0.0
        remain = (burst_start + hold) - time.time()
        if remain <= 0:
            return 0.0
        did = str(d.get("draft_id") or d.get("id") or "")
        if did and did not in self._first_reply_held_ids:
            self._first_reply_held_ids.add(did)
            if len(self._first_reply_held_ids) > 2048:
                self._first_reply_held_ids = set(list(self._first_reply_held_ids)[-512:])
            self.total_first_reply_held += 1
            logger.info(
                "[pacing] first_reply hold conv=%s platform=%s silence=%s hold=%.0fs "
                "remain=%.0fs draft=%s",
                conversation_id, _plat or "-",
                "first_contact" if silence is None else "%.1fh" % (silence / 3600.0),
                hold, remain, did)
        return remain

    # ── Q-3（#264 C/D）人工优先：单一复检函数 + 在途取消 ──────────────────

    def _human_priority_gate(self, conv: str, *, draft_id: str = "",
                             is_farewell: bool = False) -> str:
        """人工优先复检——**一个函数两处调用**：捞稿（resolve 前，原 Sprint1 档位闸位置）
        与真发前（拟人等待结束后）。返回放弃原因码（:data:`ABORT_REASONS`）；空＝放行。

        判定顺序：① 在途取消信号（切档路由 / 坐席打字端点 / 坐席发送点名本稿）→ 该信号的
        by；② 会话显式档位 ∈ manual/review/multi_choice → mode_changed（Sprint1 原闸逐字：
        仅真实字符串生效，None / mock 不干预）；③ 坐席 60s 内发送 → agent_sent；
        ④ 坐席 60s 内打字 → agent_typing；⑤ 会话级风险持有 → risk_hold；⑥ 「需人工」标在场
        → needs_human。告别稿（停联「最多一条」）豁免全部；判定只读，任何异常按放行。

        Q-18 B（#292）：③④ 两码在两处调用方都按 **defer**（让位不丢稿，见 ``_yield_mark_defer``），
        其余原因码仍是放弃——本函数只判不处置，语义不变。
        """
        if not conv or is_farewell:
            return ""
        if draft_id:
            with self._hp_lock:
                by = self._inflight_cancel.get(draft_id, "")
            if by:
                return by
        store = getattr(self._svc, "_store", None)
        if store is not None and hasattr(store, "get_automation_mode_if_set"):
            try:
                _m = store.get_automation_mode_if_set(conv)
            except Exception:
                _m = None
            if isinstance(_m, str) and _m in ("manual", "review", "multi_choice"):
                return "mode_changed"
        now = time.time()
        if now - float(self._agent_sent_ts.get(conv, 0.0)) <= AGENT_ACTIVITY_WINDOW_SEC:
            return "agent_sent"
        if now - float(self._agent_typing_ts.get(conv, 0.0)) <= AGENT_ACTIVITY_WINDOW_SEC:
            return "agent_typing"
        if store is not None:
            # 会话级「无限制」（conv_route，2026-09-12）：⑤ 风险持有 / ⑥ 需人工标 属风控层，
            # 本会话让路（①–④ 坐席在环信号是人工优先，不是拦截，照常）。异常 → 照常判。
            _unr_hold = _unr_tag = False
            try:
                from src.ai.conv_route import skip_for_conv as _cr_skip_w
                _unr_hold = bool(_cr_skip_w(store, conv, "risk_hold"))
                _unr_tag = bool(_cr_skip_w(store, conv, "needs_human_tag"))
            except Exception:
                logger.debug("[autosend_worker] conv_route skip lookup failed; human gate stays on", exc_info=True)
                _unr_hold = _unr_tag = False
            try:
                from src.inbox import risk_hold as _rh
                _hold = "" if _unr_hold else _rh.active(store, conv)
                if _hold:
                    # 泛因 needs_human（打标派生）按 needs_human 报，其余（privacy / commitment /
                    # stop_contact…）按 risk_hold 报——日志能直接看出是哪一类闸
                    return "needs_human" if _hold == _rh.GENERIC_REASON else "risk_hold"
            except Exception:
                logger.debug("[AutosendWorker] risk_hold 判定异常（放行）", exc_info=True)
            if hasattr(store, "get_conv_tags") and not _unr_tag:
                try:
                    from src.integrations.protocol_autoreply import HANDOFF_TAG as _hp_tag
                    if _hp_tag in list(store.get_conv_tags(conv) or []):
                        return "needs_human"
                except Exception:
                    pass
        return ""

    def _inflight_register(self, item: Dict[str, Any]) -> None:
        did = str(item.get("draft_id") or "")
        if not did:
            return
        with self._hp_lock:
            self._inflight[did] = {
                "conversation_id": str(item.get("conversation_id") or ""),
                "platform": str(item.get("platform") or ""),
                "account_id": str(item.get("account_id") or "default"),
                "since": time.time(),
            }

    def _inflight_unregister(self, draft_id: str) -> None:
        with self._hp_lock:
            self._inflight.pop(str(draft_id or ""), None)
            self._inflight_cancel.pop(str(draft_id or ""), None)

    def cancel_inflight(self, *, conversation_id: str = "", platform: str = "",
                        account_id: str = "", by: str = "mode_switch") -> int:
        """取消范围内**排队中 + 拟人等待中**的 L2 稿。返回取消条数（前端 toast「已取消 N 条」）。

        范围：``conversation_id`` → 单会话；否则 ``platform``(+``account_id``) → 账号；都不给
        → 全部在途。排队中的经 ``store.cancel_pending_l2_drafts``（会话范围）作废；在途的写
        取消信号（等待结束即放弃，不真发）并把已 resolve 的行 approved→cancelled
        （``decided_by=abort:<by>``）。``by`` ∈ mode_switch | agent_typing | agent_send。
        日志 ``[inflight] cancel scope=conv|account|all n=… by=…``。绝不抛。
        """
        by = str(by or "mode_switch")
        cid = str(conversation_id or "")
        plat = str(platform or "").lower()
        acct = str(account_id or "")
        scope = "conv" if cid else ("account" if plat else "all")
        store = getattr(self._svc, "_store", None)
        n = 0
        hit_ids: List[str] = []
        with self._hp_lock:
            for did, meta in list(self._inflight.items()):
                if cid and meta.get("conversation_id") != cid:
                    continue
                if not cid and plat and (
                        str(meta.get("platform") or "").lower() != plat
                        or (acct and str(meta.get("account_id") or "") != acct)):
                    continue
                if did in self._inflight_cancel:
                    continue
                self._inflight_cancel[did] = by
                hit_ids.append(did)
        for did in hit_ids:
            n += 1
            try:
                if store is not None and hasattr(store, "update_draft_status"):
                    try:
                        store.update_draft_status(
                            did, status="cancelled", decided_by=f"abort:{by}"[:40],
                            expected_statuses=("approved", "pending", "enriching"))
                    except TypeError:   # 旧签名 / 测试替身无 expected_statuses
                        store.update_draft_status(
                            did, status="cancelled", decided_by=f"abort:{by}"[:40])
            except Exception:
                logger.debug("[AutosendWorker] 在途稿改 cancelled 失败 draft_id=%s", did,
                             exc_info=True)
        if cid and store is not None and hasattr(store, "cancel_pending_l2_drafts"):
            try:
                n += int(store.cancel_pending_l2_drafts(cid, decided_by=f"abort:{by}"[:40]) or 0)
            except Exception:
                logger.debug("[AutosendWorker] 排队 L2 取消失败 conv=%s", cid, exc_info=True)
        # Q-18 B：presend 期让位、正在 _retry_queue 里等窗过的载荷（行已 approved，不在 pending
        # 也不在 _inflight）——坐席此刻真发 / 切档 = 客户那句已被人接，同范围一并取消，
        # 与排队稿同口径（否则窗过后 AI 再答一遍 = 同问双答）。
        _yq: List[Dict[str, Any]] = []
        for r in list(self._retry_queue):
            _it = r.get("item") or {}
            if not _it.get("_yield_defer"):
                continue
            _ic = str(_it.get("conversation_id") or "")
            if cid and _ic != cid:
                continue
            if not cid and plat and (
                    str(_it.get("platform") or "").lower() != plat
                    or (acct and str(_it.get("account_id") or "") != acct)):
                continue
            _yq.append(r)
        for r in _yq:
            try:
                self._retry_queue.remove(r)
            except ValueError:
                continue
            _did = str((r.get("item") or {}).get("draft_id") or "")
            _ic = str((r.get("item") or {}).get("conversation_id") or "")
            self._yield_defer.pop(_ic, None)
            n += 1
            try:
                if store is not None and hasattr(store, "update_draft_status") and _did:
                    try:
                        store.update_draft_status(
                            _did, status="cancelled", decided_by=f"abort:{by}"[:40],
                            expected_statuses=("approved", "pending"))
                    except TypeError:
                        store.update_draft_status(
                            _did, status="cancelled", decided_by=f"abort:{by}"[:40])
            except Exception:
                logger.debug("[AutosendWorker] 让位改期稿改 cancelled 失败 draft_id=%s", _did,
                             exc_info=True)
            logger.info("[autosend] abort=%s stage=deferred draft=%s conv=%s（让位等待中被人接）",
                        by, _did, _ic)
            self._ledger_abort(_ic, by, stage="deferred", draft_id=_did)
        self.total_inflight_cancelled += n
        if n or hit_ids:
            logger.info("[inflight] cancel scope=%s n=%d by=%s conv=%s account=%s inflight=%d",
                        scope, n, by, cid or "-", (f"{plat}:{acct}" if plat else "-"),
                        len(hit_ids))
        return n

    def note_agent_typing(self, conversation_id: str) -> int:
        """坐席在输入框打字（端点 3s 一次节流）：记时刻 + 取消该会话在途 / 排队 AI 稿。"""
        cid = str(conversation_id or "")
        if not cid:
            return 0
        self._agent_typing_ts[cid] = time.time()
        return self.cancel_inflight(conversation_id=cid, by="agent_typing")

    def note_agent_send(self, conversation_id: str) -> int:
        """坐席手动发送成功：记时刻 + 取消该会话在途 / 排队 AI 稿（摘标由路由既有逻辑做）。"""
        cid = str(conversation_id or "")
        if not cid:
            return 0
        self._agent_sent_ts[cid] = time.time()
        return self.cancel_inflight(conversation_id=cid, by="agent_send")

    def _ledger_abort(self, conv: str, code: str, *, stage: str, draft_id: str = "") -> None:
        """Q-18 D（#293）：`[autosend] abort=` 三处日志点同步落拦截台账（app_settings KV 滚动 200，
        回复设置页「今日拦截」卡 / why_no_reply 消费）。risk_hold / needs_human 顺带取持有记录的
        reason / hit。best-effort，绝不抛。"""
        store = getattr(self._svc, "_store", None)
        if store is None or not conv:
            return
        try:
            from src.inbox import abort_ledger as _al
            hit, reason = "", str(code or "")
            if code in ("risk_hold", "needs_human"):
                try:
                    from src.inbox import risk_hold as _rh
                    _rec = _rh.active_record(store, conv) or {}
                    hit = str(_rec.get("hit") or _rec.get("last_hit") or "")
                    reason = str(_rec.get("reason") or code)
                except Exception:
                    hit = ""
            _al.record(store, conversation_id=conv, code=code, stage=stage, hit=hit,
                       source="autosend", draft_id=draft_id, reason=reason)
        except Exception:
            logger.debug("[autosend] 拦截台账写入失败 conv=%s code=%s（忽略）", conv, code,
                         exc_info=True)

    # ── Q-18 B/C（#292）让位 = 延后不是丢弃 ─────────────────────────────

    def agent_yield_state(self, conversation_id: str) -> Dict[str, Any]:
        """会话当前「AI 让位」状态（诊断 finding ``agent_yield`` / 会话头 ``ay-`` chip 同源）：
        :func:`autosend_policy.agent_yield_state` 的结果 + 登记表里的 ``draft_id / stage /
        deferrals``（有稿在等时）。只读、绝不抛。"""
        cid = str(conversation_id or "")
        st = _agent_yield_state(self._agent_sent_ts.get(cid, 0.0),
                                self._agent_typing_ts.get(cid, 0.0))
        ent = self._yield_defer.get(cid)
        if ent:
            st["draft_id"] = str(ent.get("draft_id") or "")
            st["stage"] = str(ent.get("stage") or "")
            st["deferrals"] = int(ent.get("deferrals") or 0)
        return st

    def _yield_schedule_wake(self, conv: str, until: float) -> None:
        """窗过（until）后唤醒主循环复检——否则要等 min_interval 兜底。loop 未起（测试 /
        未 run）→ 跳过，靠下一 tick。"""
        loop = self._loop
        if loop is None or self._l2_event is None:
            return
        delay = max(0.5, float(until) - time.time() + 0.5)

        def _arm() -> None:
            self._yield_cancel_wake(conv)
            try:
                self._yield_wake_handles[conv] = loop.call_later(delay, self._l2_event.set)
            except Exception:
                logger.debug("[autosend] yield wake 定时失败 conv=%s（靠下一 tick）", conv,
                             exc_info=True)
        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            logger.debug("[autosend] yield wake 投递失败（loop 已停）conv=%s", conv)

    def _yield_cancel_wake(self, conv: str) -> None:
        h = self._yield_wake_handles.pop(conv, None)
        if h is None:
            return
        try:
            h.cancel()
        except Exception:
            logger.debug("[autosend] yield wake 取消失败 conv=%s", conv, exc_info=True)

    def _yield_mark_defer(self, conv: str, code: str, draft_id: str, *,
                          stage: str) -> "tuple[float, bool]":
        """登记一次让位。返回 ``(until, exhausted)``：``exhausted=True`` = 同稿连续让位已超
        :data:`AGENT_YIELD_MAX_DEFERRALS`，调用方按放弃处理（老路）。同稿同窗（until 未变）
        只计一次、只落一行 ``[autosend] defer=`` 日志——batch 期 pending 稿每 tick 都会再过闸。"""
        now = time.time()
        st = _agent_yield_state(self._agent_sent_ts.get(conv, 0.0),
                                self._agent_typing_ts.get(conv, 0.0), now=now)
        until = float(st.get("until") or (now + AGENT_ACTIVITY_WINDOW_SEC))
        ent = self._yield_defer.get(conv)
        same_draft = bool(ent) and str(ent.get("draft_id") or "") == str(draft_id)
        new_window = (not same_draft) or abs(float(ent.get("until") or 0.0) - until) > 0.5
        deferrals = (int(ent.get("deferrals") or 0) if same_draft else 0) + (1 if new_window else 0)
        if deferrals > _YIELD_MAX_DEFERRALS:
            self._yield_defer.pop(conv, None)
            self.total_yield_exhausted += 1
            logger.warning("[autosend] yield_exhausted=%s stage=%s draft=%s conv=%s deferrals=%d"
                           "（坐席持续活动，本稿按放弃处理）", code, stage, draft_id, conv,
                           deferrals - 1)
            return until, True
        self._yield_defer[conv] = {
            "by": code, "until": until, "since": float(st.get("since") or now),
            "draft_id": str(draft_id), "stage": stage, "deferrals": deferrals,
            "first_ts": float(ent.get("first_ts") or now) if same_draft else now,
        }
        if new_window:
            self.total_yield_deferred += 1
            logger.info("[autosend] defer=%s stage=%s draft=%s conv=%s until=%.0f "
                        "(in %.0fs, deferral %d/%d)（让位不丢稿，窗过自动复检）",
                        code, stage, draft_id, conv, until, max(0.0, until - now),
                        deferrals, _YIELD_MAX_DEFERRALS)
            self._yield_schedule_wake(conv, until)
        return until, False

    def _yield_resume_if_deferred(self, conv: str, draft_id: str, *, stage: str) -> bool:
        """闸放行且该会话有让位登记 → 摘登记 + ``[autosend] resume=agent_window_passed``。"""
        ent = self._yield_defer.pop(conv, None)
        if not ent:
            return False
        self._yield_cancel_wake(conv)
        self.total_yield_resumed += 1
        logger.info("[autosend] resume=agent_window_passed stage=%s draft=%s conv=%s by=%s "
                    "waited=%.0fs deferrals=%d", stage, draft_id, conv, ent.get("by", ""),
                    max(0.0, time.time() - float(ent.get("first_ts") or time.time())),
                    int(ent.get("deferrals") or 0))
        return True

    def resume_agent_yield(self, conversation_id: str, *, by: str = "mode_select") -> Dict[str, Any]:
        """Q-18 C：切到 / 重选「全自动」= 明示接回——清该会话 ``_agent_sent_ts / _agent_typing_ts``、
        摘让位登记、把 presend 期改期的载荷改为立即到期并唤醒主循环。返回
        ``{had_yield, released, by}``（前端 toast 用）。日志 ``[autosend] resume by=mode_select``。
        绝不抛。"""
        cid = str(conversation_id or "")
        if not cid:
            return {"had_yield": False, "released": 0, "by": by}
        had = bool(cid in self._agent_sent_ts or cid in self._agent_typing_ts
                   or cid in self._yield_defer)
        self._agent_sent_ts.pop(cid, None)
        self._agent_typing_ts.pop(cid, None)
        ent = self._yield_defer.pop(cid, None)
        self._yield_cancel_wake(cid)
        released = 0
        for r in self._retry_queue:
            _it = r.get("item") or {}
            if _it.get("_yield_defer") and str(_it.get("conversation_id") or "") == cid:
                r["next_ts"] = 0.0
                released += 1
        if had or released:
            self.total_yield_resumed += 1
            logger.info("[autosend] resume by=%s conv=%s released=%d draft=%s（清让位窗，立即放行）",
                        by, cid, released, (ent or {}).get("draft_id", "-"))
            self.notify_new_l2()
        return {"had_yield": had, "released": released, "by": by}

    def _risk_hold_regen_once(self, d: Dict[str, Any], conv: str, reason: str) -> bool:
        """捞稿期取消了被风险持有 / 需人工闸住的 L2 稿 → 按 L1 重拟一次（同一次持有只重拟一次，
        防「取消→重拟→又是 L2→再取消」循环）。走 catchup 同一条重拟回调，触发源
        reason=risk_hold_regen（draft_trigger 登记，autodraft 侧封顶 review）。未接线 → False。"""
        if self._catchup_regen_cb is None or not conv:
            return False
        store = getattr(self._svc, "_store", None)
        stamp = 0.0
        try:
            from src.inbox import risk_hold as _rh
            rec = _rh.active_record(store, conv) if store is not None else None
            stamp = float((rec or {}).get("set_ts") or 0.0)
        except Exception:
            stamp = 0.0
        key = f"{conv}|{reason}"
        if self._risk_hold_regen_done.get(key) == (stamp or -1.0):
            return False
        self._risk_hold_regen_done[key] = stamp or -1.0
        if len(self._risk_hold_regen_done) > 2000:
            for k in list(self._risk_hold_regen_done)[:500]:
                self._risk_hold_regen_done.pop(k, None)
        peer_txt = str(d.get("peer_text") or "").strip()
        if not peer_txt:
            return False
        try:
            from src.inbox.draft_trigger import note as _trig_note
            _trig_note(conv, "risk_hold_regen")
        except Exception:
            pass
        try:
            self._catchup_regen_cb({
                "conversation_id": conv,
                "platform": str(d.get("platform") or ""),
                "account_id": str(d.get("account_id") or "default"),
                "chat_key": str(d.get("chat_key") or ""),
            }, peer_txt)
            self.total_risk_hold_regen += 1
            logger.info("[draft] regen conv=%s reason=risk_hold_regen hold=%s（L2 稿已取消，按 L1 重拟）",
                        conv, reason)
            return True
        except Exception:
            logger.warning("[AutosendWorker] risk_hold 重拟派发失败 conv=%s", conv, exc_info=True)
            return False

    async def _run_humanize(
        self, platform: str, account_id: str, chat_key: str,
        *, text: str = "", elapsed_sec: float = 0.0, conversation_id: str = "",
        inbound_text: str = "",
    ) -> None:
        """投递前拟人序列：已读 → 静默思考 → 打字续挂 → 投递（委托 humanize 协作器）。

        已读/打字回调缺省时各自跳过（无回调=旧行为）；延迟取 deliver_delay 配置
        （adaptive 时按 ``text`` 长度自适应、扣 ``elapsed_sec`` 已耗时）。mark_read 成功
        累计 total_marked_read（与旧口径一致：调用不抛即计数）。
        打字气泡两段式（2026-08-04）：延迟前段静默（真人在想/在忙，无输入状态），
        只有临发前 ``typing_lead``（按文本长度×人设手速估）秒挂「正在输入」——
        全程挂打字的旧行为等于宣称「我打了一分钟字只打出一句话」。
        """
        from src.inbox.humanize import (
            count_words,
            resolve_typing_lead,
            run_presend_humanization,
        )

        # 人设解析（best-effort）：用于人设化节奏参数 + 观测分维。失败→空（用顶层默认）。
        # 优先 3 参（含 chat_key → 会话级覆写生效，节奏跟人走）；旧 2 参 resolver
        # （测试替身/历史注入）TypeError 回落，保持兼容。
        _pid = ""
        if self._persona_resolver is not None:
            try:
                try:
                    _pid = str(
                        self._persona_resolver(platform, account_id, chat_key) or "")
                except TypeError:
                    _pid = str(self._persona_resolver(platform, account_id) or "")
            except Exception:
                _pid = ""

        _mr = None
        if (self._mark_read_callback is not None
                and self._humanize_flag(platform, "mark_read")):
            async def _mr():
                await self._mark_read_callback(platform, account_id, chat_key)
        # O-1 D（#253 #254）composing 可观测：回调返回 True＝平台真挂上了；False＝worker 不支持
        # （LINE / Messenger 协议硬限制）或失败；无回调 / 开关关 → 如实写 nocb / off。
        # 240 分钟零 typing 日志的根因就是这里此前只记指标不落日志。
        _tp = None
        _typing_state = {"calls": 0, "ok": 0}
        if self._typing_callback is None:
            _composing = "nocb"
        elif not self._humanize_flag(platform, "typing"):
            _composing = "off"
        else:
            _composing = "pending"

            async def _tp(action):
                _typing_state["calls"] += 1
                _res = await self._typing_callback(platform, account_id, chat_key, action)
                if _res is None or bool(_res):
                    _typing_state["ok"] += 1

        def _inc_marked():
            self.total_marked_read += 1

        _pr = self._pick_deliver_pacing(
            text, elapsed_sec, persona_id=_pid, platform=platform,
            conversation_id=conversation_id, inbound_text=inbound_text)
        _lead = (_pr.typing_lead if getattr(_pr, "typing_lead", None) is not None
                 else resolve_typing_lead(self._deliver_delay_block, text=text,
                                          persona_id=_pid, platform=platform))
        await run_presend_humanization(
            delay=_pr.delay,
            action="typing",
            mark_read=_mr,
            typing=_tp,
            sleep=self._sleep,
            refresh_sec=_TYPING_REFRESH_SEC,
            typing_lead_sec=_lead,
            on_marked=_inc_marked,
            stop_before_send_sec=float(getattr(_pr, "stop_sec", 0.0) or 0.0),
        )
        if _composing == "pending":
            if _typing_state["calls"] == 0:
                _composing = "skipped"          # 延迟太短 / 打字段低于阈值，没到挂气泡那一步
            elif _typing_state["ok"] > 0:
                _composing = "sent"
            else:
                _composing = "unsupported"
        # 每条出站一行节奏日志（D-O4 验收：发出间隔与字数正相关、无两条到秒相同）
        logger.info(
            "[pacing] conv=%s platform=%s profile=%s read=%.1f think=%.1f type=%.1f stop=%.1f "
            "elapsed=%.1f total=%.1f typing_lead=%.1f composing=%s typing_calls=%d words=%d",
            conversation_id or "-", platform or "-", getattr(_pr, "profile", "") or "legacy",
            float(getattr(_pr, "read_sec", 0.0) or 0.0), float(getattr(_pr, "think_sec", 0.0) or 0.0),
            float(getattr(_pr, "type_sec", 0.0) or 0.0), float(getattr(_pr, "stop_sec", 0.0) or 0.0),
            float(_pr.elapsed or 0.0), float(_pr.delay or 0.0), float(_lead or 0.0),
            _composing, int(_typing_state["calls"]), count_words(text))

    def notify_new_l2(self) -> None:
        """从任意线程安全地通知 worker：有新 L2 草稿已落库，立即唤醒（C3 事件驱动）。

        由 InboxStore.register_l2_callback 注册调用。
        使用 loop.call_soon_threadsafe 避免跨线程 asyncio 竞态。
        """
        if self._loop is not None and self._l2_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._l2_event.set)
            except RuntimeError:
                pass  # event loop 已停止

    def _send_cb_kwargs(self, original_text: str, cb: Any = None) -> Dict[str, Any]:
        """original_text 透传 kwargs（按回调签名探测，旧 4 参回调不受影响）。

        ``cb`` 显式给出实际要调用的回调——人工通过链用的是 ``_human_send_callback``，
        与自动链可能是不同对象。2026-07-29 修：此前硬探 ``self._send_callback``，
        在 deliver=false（自动回调为 None）时 ``inspect.signature(None)`` 抛 TypeError
        → 缓存成 False → 人工链**永久丢掉 original_text**（语音分支据原文合成，
        丢了就用译文发声=念错语言）。缓存按回调对象分别记，互不串味。
        """
        target = cb if cb is not None else self._send_callback
        if target is None:
            return {}
        cached = self._send_cb_accepts_original
        if not isinstance(cached, dict):
            cached = {} if cached is None else {id(self._send_callback): bool(cached)}
            self._send_cb_accepts_original = cached
        key = id(target)
        if key not in cached:
            try:
                import inspect
                cached[key] = (
                    "original_text" in inspect.signature(target).parameters)
            except (ValueError, TypeError):
                cached[key] = False
        return {"original_text": original_text} if cached[key] else {}

    def _apply_compliance_disclosure(self, conversation_id: str, text: str) -> str:
        """WP-4 系统级披露（compliance.disclosure.notice，基线关）：每会话首条
        AI 出站前置披露语（持久防重，键=conversation_id）。

        自动链与人工通过链共用本方法（两路措辞/时机绝不漂移）。放在出站翻译
        **之后**调用——披露语按会话语言取内置模板，再过翻译层反而混语。语音
        分支用翻译前原文合成（_send_cb_kwargs 透传 original_text），披露只落
        文本面、克隆声绝不念出。开关关/provider 未注册/任何异常 → 原样返回。
        """
        try:
            from src.compliance.disclosure import apply_disclosure
            from src.compliance.runtime import runtime_config

            lang_hint = ""
            try:
                from src.inbox.outbound_translate import peer_language_hint
                _st = getattr(self._svc, "_store", None)
                if _st is not None and conversation_id:
                    lang_hint = peer_language_hint(_st, conversation_id) or ""
            except Exception:
                lang_hint = ""
            out, _applied = apply_disclosure(
                runtime_config(), conversation_id, text, lang_hint=lang_hint)
            return out
        except Exception:
            return text

    async def deliver_human_approved(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """把**人工通过**的 inbox 草稿真投递到平台（2026-07-29 修「通过≠发送」断链）。

        由 DraftService._schedule_inbox_delivery 经事件循环任务调用。与 L2 自动链共用
        send/translate 回调（出站翻译、发图指令、桌面受控出站路由全部一致），差异：
          - 不走拟人已读/打字/延迟（坐席刚刚人为决策，立即发送才符合预期）；
          - 不进 recoverable 重试队列（失败即审计 + autosend_deliver_failed 事件提醒
            坐席补发——人在场，显式失败优于静默重试）。
        自吞一切异常（后台任务，无人 await）。
        """
        item = {
            "draft_id": str(draft.get("draft_id") or ""),
            "conversation_id": str(draft.get("conversation_id") or ""),
            "platform": str(draft.get("platform") or ""),
            "account_id": str(draft.get("account_id") or "default"),
            "chat_key": str(draft.get("chat_key") or ""),
            "text": str(draft.get("final_text") or draft.get("draft_text") or "").strip(),
            # O-1 B（D-O2）：人审后的终稿是人的文字——出站去 AI 标点后处理见此标记即绕过
            "origin": "manual",
        }
        return await self._deliver_now(item, stage="human")

    # ── Q-23（#303）stage=soft_reply：守卫触发的自动软回应单一出口 ─────────────

    def _soft_reply_own_hold(self, conv: str) -> bool:
        """当前会话级持有是否正是软回应要回应的那个（``SOFT_REPLY_OWN_HOLDS``）。"""
        store = getattr(self._svc, "_store", None)
        if store is None or not conv:
            return False
        try:
            from src.inbox import risk_hold as _rh
            from src.inbox.autosend_policy import SOFT_REPLY_OWN_HOLDS
            return str(_rh.active(store, conv) or "") in SOFT_REPLY_OWN_HOLDS
        except Exception:
            return False

    def _soft_reply_abort(self, row: Dict[str, Any], code: str, *, detail: str = "") -> Dict[str, Any]:
        cid = str(row.get("conversation_id") or "")
        did = str(row.get("draft_id") or "")
        self.total_soft_reply_aborted += 1
        logger.info("[autosend] abort=%s stage=soft_reply draft=%s conv=%s level=%s policy=%s mode=%s%s",
                    code, did or "-", cid or "-", row.get("level") or "-", row.get("policy") or "-",
                    row.get("mode") or "-", (f" detail={detail}" if detail else ""))
        self._ledger_abort(cid, code, stage="soft_reply", draft_id=did)
        return {"ok": False, "status": code, "error": code}

    async def _soft_reply_generate(self, row: Dict[str, Any], ctx: Any) -> Dict[str, str]:
        """人设口吻一句话短生成。返回 ``{text, lang, by}``；``text`` 空＝生成失败（**不发**，不回落固定句）。"""
        cid = str(row.get("conversation_id") or "")
        store = getattr(self._svc, "_store", None)
        cfg = getattr(self._svc, "_cfg", None) or {}
        lang, by = "", ""
        try:
            from src.inbox.outbound_translate import resolve_outbound_lang
            lang, by = resolve_outbound_lang(
                cid, store=store, cfg_root=cfg, platform=str(row.get("platform") or ""),
                account_id=str(row.get("account_id") or ""), chat_key=str(row.get("chat_key") or ""))
        except Exception:
            logger.debug("[adult] soft_reply resolve_outbound_lang 异常", exc_info=True)
        if not lang:
            lang = str(row.get("lang") or getattr(ctx, "lang", "") or "").strip()
            by = "peer_text" if lang else ""
        if not lang:
            return {"text": "", "lang": "", "by": "lang_unknown"}
        if self._app is None:
            return {"text": "", "lang": lang, "by": by, "gen": "no_app"}
        try:
            from src.inbox.persona_reply import generate_soft_deflect
            res = await generate_soft_deflect(
                app=self._app, platform=str(row.get("platform") or ""),
                chat_key=str(row.get("chat_key") or ""), account_id=str(row.get("account_id") or ""),
                conversation_id=cid, target_lang=lang, peer_text=str(row.get("peer_text") or ""),
                persona_id=str(row.get("persona_id") or ""), level=str(row.get("level") or ""))
        except Exception:
            logger.debug("[adult] soft_reply 生成异常（不发）", exc_info=True)
            res = {}
        text = str((res or {}).get("reply") or "").strip() if (res or {}).get("ok") else ""
        return {"text": text, "lang": lang, "by": by}

    async def deliver_soft_reply(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """守卫触发的自动软回应（成人 explicit/pressure × soft_reply、human 3 分钟补发）**唯一**出口。

        与人工通过链的差别：人工通过是人的决定、什么闸都不过；软回应是**自动出站**，所以：
          ① ``autosend_policy.decide(kind="soft_reply")``：场景闸（群 / 非客户 → L0）、冻结、档位
             （非 auto_ai → 候选进审核稿，不发）、他因持有；
          ② ``_human_priority_gate``：在途取消 / mode_changed / 坐席 60s 内发送或打字 → **放弃**
             （软回应不延后——坐席在场就交给人）；risk_hold / needs_human 只放行「自己刚设的 adult 持有」；
          ③ 文本由 ``persona_reply.generate_soft_deflect`` 人设口吻短生成；生成失败 → 不发（无固定句）；
          ④ 生成耗时数秒，真发前**再过一次** ②；
          ⑤ 投递与人工通过共用 ``_deliver_now``（翻译 / 披露 / deliver_once 幂等 / 失败审计同口径）。
        跳过节奏排队（软回应要即时），不进 recoverable 重试。自吞一切异常（后台任务无人 await）。
        日志 ``[adult] soft_reply conv= level= policy= mode= lang= gen=persona|skip status=``。
        """
        row = dict(row or {})
        cid = str(row.get("conversation_id") or "")
        did = str(row.get("draft_id") or f"soft_reply:{cid}:{int(time.time())}")
        row["draft_id"] = did
        store = getattr(self._svc, "_store", None)
        cfg = getattr(self._svc, "_cfg", None) or {}
        if not cid or not str(row.get("chat_key") or ""):
            return {"ok": False, "status": "empty", "error": "empty"}
        try:
            from src.inbox.guard_context import from_row as _ctx_from_row
            ctx = _ctx_from_row(row, cfg=cfg, store=store)
        except Exception:
            ctx = None
        frozen = ""
        try:
            from src.inbox.stop_contact import frozen_reason as _frz
            frozen = str(_frz(store, cid) or "") if store is not None else ""
        except Exception:
            frozen = ""
        try:
            from src.inbox.autosend_policy import decide as _decide
            dec = _decide(peer_risk="low", automation_mode=str(row.get("automation_mode") or ""),
                          platform=str(row.get("platform") or ""), conversation_frozen=bool(frozen),
                          conversation_id=cid, store=store, kind="soft_reply", ctx=ctx)
        except Exception:
            logger.debug("[adult] soft_reply decide 异常（按不发）", exc_info=True)
            return self._soft_reply_abort(row, "policy_error")
        if not dec.autosend_allowed:
            code = str(dec.hold_reason or "policy")
            if code.startswith("mode:"):
                code = "mode_changed"
            return self._soft_reply_abort(row, code, detail=str(dec.hold_reason or ""))
        gate = self._human_priority_gate(cid, draft_id=did)
        if gate in ("risk_hold", "needs_human") and self._soft_reply_own_hold(cid):
            gate = ""
        if gate:
            return self._soft_reply_abort(row, gate)
        self._inflight_register({"draft_id": did, "conversation_id": cid,
                                 "platform": row.get("platform"), "account_id": row.get("account_id")})
        try:
            text = str(row.get("final_text") or row.get("draft_text") or "").strip()
            gen = "given" if text else "persona"
            lang = str(row.get("lang") or "")
            if not text:
                g = await self._soft_reply_generate(row, ctx)
                text, lang = str(g.get("text") or ""), str(g.get("lang") or "")
                if not text:
                    gen = "skip"
                    logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=%s lang=%s gen=skip status=%s",
                                cid, row.get("level") or "-", row.get("policy") or "-", row.get("mode") or "-",
                                lang or "-", g.get("gen") or g.get("by") or "gen_failed")
                    self.total_soft_reply_errors += 1
                    return {"ok": False, "status": "gen_skip", "error": str(g.get("gen") or g.get("by") or "gen_failed")}
            gate = self._human_priority_gate(cid, draft_id=did)
            if gate in ("risk_hold", "needs_human") and self._soft_reply_own_hold(cid):
                gate = ""
            if gate:
                return self._soft_reply_abort(row, gate, detail="after_gen")
            item = {
                "draft_id": did, "conversation_id": cid,
                "platform": str(row.get("platform") or ""),
                "account_id": str(row.get("account_id") or "default"),
                "chat_key": str(row.get("chat_key") or ""),
                "text": text, "origin": "soft_reply",
            }
            res = await self._deliver_now(item, stage="soft_reply")
            logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=%s lang=%s gen=%s status=%s text=%s",
                        cid, row.get("level") or "-", row.get("policy") or "-", row.get("mode") or "-",
                        lang or "-", gen, "sent" if res.get("ok") else f"failed:{res.get('error')}", text[:60])
            if res.get("ok"):
                try:
                    from src.inbox.adult_grader import record_sent as _rec
                    _rec(store, row, text)
                except Exception:
                    logger.debug("[adult] soft_reply 账本写入失败（忽略）", exc_info=True)
            return res
        except Exception as exc:  # noqa: BLE001
            self.total_soft_reply_errors += 1
            logger.warning("[adult] soft_reply conv=%s 投递链异常: %s", cid, exc)
            return {"ok": False, "status": "error", "error": str(exc)}
        finally:
            self._inflight_unregister(did)

    async def _deliver_now(self, item: Dict[str, Any], *, stage: str = "human") -> Dict[str, Any]:
        """人工通过 / 软回应共用的**即时**投递核心（不走拟人节奏、不进重试队列）。

        ``stage="human"`` 逐字保持 2026-07-29 以来的人工通过行为（计数 / 影子计量 / 日志口径）；
        ``stage="soft_reply"``（Q-23）只换计数与日志前缀。
        """
        send_cb = self._human_send_callback or self._send_callback
        if send_cb is None or not item["text"] or not item["chat_key"]:
            return {"ok": False, "error": "no_send_path_or_empty"}
        _human = stage == "human"
        _label = "人工通过草稿" if _human else "软回应"
        # B41 投递幂等钉：人工通过与自动链共用同一登记表——同稿在途/已投过
        # 一律拒（resolve 的状态 CAS 只保「处置一次」，这里保「出门一次」）。
        _claim_err = self._deliver_once.claim(
            item["conversation_id"], item["draft_id"], item["text"])
        if _claim_err:
            self.total_skipped_already_sent += 1
            logger.warning(
                "[AutosendWorker] guard=deliver_once %s投递被拒 draft=%s "
                "conv=%s reason=%s（同稿已投/在途，防双发）",
                "人工通过" if _human else "软回应",
                item["draft_id"], item["conversation_id"], _claim_err)
            return {"ok": False, "error": f"deliver_once:{_claim_err}"}
        _delivered_ok = False
        try:
            send_text = item["text"]
            if self._translate_callback is not None:
                _tx = send_text
                try:
                    _tx = await self._translate_callback(item)
                except Exception:
                    logger.warning(
                        "[AutosendWorker] %s出站翻译回调异常 → HOLD 不发 conv=%s",
                        "人工通过" if _human else "软回应",
                        item["conversation_id"], exc_info=True)
                    _tx = None
                # None = HOLD（无兜底纪律 2026-08-17：翻译失败一律不发原文，
                # 回调内部已收口全部失败面；回调自身异常同按 HOLD）。走投递
                # 失败链（审计+坐席铃铛），翻译链恢复后人工/重试补投。
                # M-1 B #234（D-M3）：原因随失败链透出——lang_unknown（客户语言判不出，
                # 转人工确认）/ 校验失败（含 target_lang_mismatch，翻译失败待确认）。
                if _tx is None:
                    raise RuntimeError(_translate_hold_message(item))
                if _tx:
                    if _tx != send_text:
                        self.total_translated += 1
                    send_text = _tx
            send_text = self._apply_compliance_disclosure(
                item["conversation_id"], send_text)
            res = await send_cb(
                item["platform"], item["account_id"], item["chat_key"],
                send_text, **self._send_cb_kwargs(item["text"], send_cb),
            )
            if isinstance(res, dict) and (
                res.get("ok") is False
                or res.get("delivered") is False
                or res.get("blocked")
            ):
                raise RuntimeError(str(
                    res.get("error") or res.get("blocked") or "send not ok"))
            _delivered_ok = True
            if _human:
                self.total_human_delivered += 1
            else:
                self.total_soft_reply_delivered += 1
            # #88：人审通过投递送达成功 → 清 dead-peer 标（与自动链同口径）
            self._dead_peer_clear(item["conversation_id"])
            # B41：成功投递补写 DB sent_at（best-effort，与自动链同口径；软回应无草稿行，跳过）
            if _human:
                try:
                    _st_mark = getattr(self._svc, "_store", None)
                    if _st_mark is not None and hasattr(_st_mark, "mark_draft_sent"):
                        _st_mark.mark_draft_sent(item["draft_id"])
                except Exception:
                    logger.debug("[AutosendWorker] mark_draft_sent 失败（忽略）",
                                 exc_info=True)
            # 2026-08-19 Token P5b 影子计数（观测非计费）：投递点口径——与出稿点
            # （ai_client generate_reply 记 ai_reply）对读几周，拿真实「出稿/投递」
            # 比值再决定计费点迁移。fail-silent，总闸关=零行为。
            _shadow_key = "ai_reply_delivered_human" if _human else "ai_reply_delivered_soft_reply"
            try:
                from src.licensing.token_ledger import record_shadow

                record_shadow(_shadow_key)
            except Exception:
                # 影子计量丢一次＝「出稿 vs 投递」对读偏小。该读数是 enforce 切换的
                # 判据之一（tools/wallet_shadow_report.py），静默偏差会让决策失真。
                logger.warning("[AutosendWorker] 影子计量 %s 记账失败", _shadow_key,
                               exc_info=True)
            # 人工通过也进同一账本：坐席刚发过 → 紧随的自动稿同样要垫连发地板
            # （对客户视角「谁按的发送」不重要，背靠背两条出站一样露馅）。
            self._note_conv_sent(item["conversation_id"])
            logger.info(
                "[AutosendWorker] %s已投递 draft=%s conv=%s",
                _label, item["draft_id"], item["conversation_id"])
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            if _human:
                self.total_human_deliver_errors += 1
                self.last_error = f"human_deliver: {exc}"
            else:
                self.total_soft_reply_errors += 1
                self.last_error = f"soft_reply_deliver: {exc}"
            logger.warning(
                "[AutosendWorker] %s投递失败 draft=%s conv=%s: %s",
                _label, item["draft_id"], item["conversation_id"], exc)
            try:
                rec = getattr(self._svc, "record_autosend_failure", None)
                if rec is not None and _human:
                    rec(
                        item["draft_id"],
                        conversation_id=item["conversation_id"],
                        reason=f"人工通过投递失败: {exc}",
                    )
            except Exception:
                logger.debug("human_deliver 失败审计写入失败", exc_info=True)
            # 复用坐席铃铛/webhook 的投递失败提醒（工作台已订阅该事件）；软回应是自动出站，
            # 失败不要坐席补发（人工处置由「需人工」标本身承载），只留日志。
            if _human:
                self._publish_deliver_failed(
                    item, str(exc), permanent=_is_permanent_send_error(str(exc)))
            return {"ok": False, "error": str(exc)}
        finally:
            self._deliver_once.release(
                item["conversation_id"], item["draft_id"],
                delivered=_delivered_ok, text=item["text"])

    async def run(self) -> None:
        if not self._enabled:
            logger.info("[AutosendWorker] L2 自动发送已禁用（config.enabled=false）")
            return
        # B41 防双循环：同一 draft_service 的第二个自动循环＝每条 L2 都可能被
        # 两边各投一次（resolve CAS 只保一方 resolve，但两实例都持有 send 能力时
        # 竞态窗口仍在）。拒绝启动 + ERROR 留痕，绝不静默并跑。
        _svc_key = id(self._svc)
        if _svc_key in _RUNNING_SVC_KEYS:
            logger.error(
                "[AutosendWorker] 同一 draft_service 已有自动循环在跑，"
                "拒绝二次启动（B41 防双投）")
            return
        _RUNNING_SVC_KEYS.add(_svc_key)
        try:
            # C3：在 run() 内初始化，确保绑定到正确的 event loop
            self._loop = asyncio.get_running_loop()
            self._l2_event = asyncio.Event()
            self._running = True
            logger.info(
                "[AutosendWorker] 启动（事件驱动+定时兜底）— min_interval=%.0fs max_interval=%.0fs "
                "circuit_threshold=%d cooldown=%.0fs",
                self._min_interval, self._max_interval,
                self._circuit_threshold, self._cooldown_sec,
            )
            # 首次启动延迟，避免与服务启动争资源；但用可中断等待——
            # 若启动延迟期间有新 L2 草稿落库（事件触发），立即提前唤醒，不再傻等满 startup_delay。
            if self._startup_delay > 0:
                try:
                    await asyncio.wait_for(self._l2_event.wait(), timeout=self._startup_delay)
                    self._l2_event.clear()
                    self.event_triggers += 1
                except asyncio.TimeoutError:
                    pass
            while self._running:
                await self._tick()
                jitter = random.uniform(-0.1, 0.1) * self._current_interval
                wait_sec = max(5.0, self._current_interval + jitter)
                # C3：等待事件或定时器兜底
                try:
                    await asyncio.wait_for(self._l2_event.wait(), timeout=wait_sec)
                    self._l2_event.clear()
                    self.event_triggers += 1
                    logger.debug("[AutosendWorker] L2 事件触发，提前唤醒")
                except asyncio.TimeoutError:
                    pass  # 定时兜底触发，正常
        finally:
            _RUNNING_SVC_KEYS.discard(_svc_key)
            self._running = False

    # ── 单轮逻辑 ─────────────────────────────────────────────

    async def _tick(self) -> None:
        self.cycles += 1
        self.last_run_ts = time.time()

        # 检查熔断器
        if self._circuit_open:
            elapsed = time.time() - self._circuit_open_ts
            if elapsed < self._cooldown_sec:
                logger.debug(
                    "[AutosendWorker] 熔断中，剩余冷却 %.0fs", self._cooldown_sec - elapsed
                )
                return
            # 半开：尝试恢复
            self._circuit_open = False
            self._consecutive_errors = 0
            logger.info("[AutosendWorker] 熔断冷却完毕，进入半开状态尝试恢复")

        to_deliver: List[Dict[str, Any]] = []
        try:
            sent, errors, to_deliver = await asyncio.get_event_loop().run_in_executor(
                None, self._process_batch
            )
        except Exception as exc:
            errors = 1
            sent = 0
            self.last_error = str(exc)
            logger.error("[AutosendWorker] 批次执行异常: %s", exc, exc_info=True)

        # 真实投递：草稿已 resolve（DB 标记 approved + autosend 审计），现把文本发到平台。
        # resolve-先于-deliver：默认「投递失败宁可丢一条也不重发」；开启 recoverable 后瞬时失败
        # 进重试队列（见下）。失败计入 deliver_errors 但不触发熔断（熔断只看 resolve 错误）。
        # Sprint2：把到期重试项并入本轮投递（重发同文本，不 re-resolve）。
        # 实施86 域B-1：排空不再闸 recoverable——限频/冻结改期项（_deferrals）
        # 独立于通用重试入队，recoverable 关时也必须到期重投；两来源共用一队，
        # 无任一来源时队列恒空 → deliver_now == to_deliver，与旧行为一致。
        deliver_now = list(to_deliver)
        if self._retry_queue:
            _rt_now = time.time()
            _due = [r for r in self._retry_queue if r.get("next_ts", 0) <= _rt_now]
            for r in _due:
                try:
                    self._retry_queue.remove(r)
                except ValueError:
                    continue
                # M-2 C（#233，K9CY6R）：改期到点的重投先过账号门禁——边车退避水位未过
                # （或冷静期 / 降级 / 未连接）→ 不投，按剩余水位再排。此前 13s/23s 改期
                # 一到点就重投 → 撞上仍在退避的边车或再次 500 → 边车 streak 续命 → 手动
                # 也 429；退避只由「真实成功 / 登录成功 / 健康探测通过」解锁，不由重试撞。
                _rit = r.get("item") or {}
                if self._send_callback is not None:
                    _hold = self._account_gate_hold(
                        str(_rit.get("platform") or ""),
                        str(_rit.get("account_id") or "default"))
                    if _hold:
                        r["next_ts"] = _rt_now + self._account_gate_wait(
                            str(_rit.get("platform") or ""),
                            str(_rit.get("account_id") or "default"))
                        self._retry_queue.append(r)
                        self.total_retry_gated += 1
                        continue
                deliver_now.append(_rit)
        if self._send_callback is not None and deliver_now:
            # 并行投递（2026-08-09，默认关）：拟人节奏上线后单条投递可占
            # 30-54s（deliver_delay）+ 至多 75s（分条间隔）——串行循环下并发
            # 会话互相排队（§95 已知边界升级为实际瓶颈）。开启后按会话分组
            # 并发：同会话保序串行（顺序/防双发不变量），跨会话受
            # max_concurrent 信号量封顶。关闭=逐条串行（旧行为）。
            if self._parallel_enabled and len(deliver_now) > 1:
                await self._deliver_parallel(deliver_now)
            else:
                for item in deliver_now:
                    await self._deliver_one(item)

        self.last_sent = sent
        self.total_sent += sent
        self.total_errors += errors

        if errors > 0:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._circuit_threshold:
                self._circuit_open = True
                self._circuit_open_ts = time.time()
                logger.warning(
                    "[AutosendWorker] 连续 %d 次错误，熔断器开启，冷却 %.0fs",
                    self._consecutive_errors, self._cooldown_sec,
                )
        else:
            self._consecutive_errors = 0

        # 自适应间隔
        self._adapt_interval(sent)

        if sent > 0:
            logger.info("[AutosendWorker] 本轮发送 %d 条，错误 %d 条", sent, errors)
        else:
            logger.debug("[AutosendWorker] 本轮无 L2 待发草稿")

        # #160 影子台账「放行后的去向」：把已放行但未终局的稿对照草稿行终态写 outcome
        # （sent / cancelled:<decided_by> / rejected / approved_unsent）。单一收口点——本文件
        # 6 处取消路径与投递成败都体现在 reply_drafts 行上，这里按结果读，不逐处埋钩子。
        # 主键查询 ≤200 次/轮、放线程池、任何异常吞掉：绝不影响发送主流程。
        _recon = getattr(self._svc, "reconcile_shadow_outcomes", None)
        if callable(_recon):
            try:
                await asyncio.get_event_loop().run_in_executor(None, _recon)
            except Exception:
                logger.debug("[AutosendWorker] shadow outcome reconcile 失败（忽略）",
                             exc_info=True)

        # H3：每日清理超龄已处理草稿（best-effort，不影响发送主流程）
        if self._cleanup_enabled and (time.time() - self._last_cleanup_ts) > self._cleanup_interval:
            try:
                store = getattr(self._svc, "_store", None)
                if store is not None and hasattr(store, "cleanup_old_drafts"):
                    n = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: store.cleanup_old_drafts(max_age_days=self._cleanup_age_days),
                    )
                    self.total_cleaned += n
                    # P3：顺带清理超龄出向译文旁路记录（best-effort，独立 try）
                    if hasattr(store, "cleanup_outbound_translations"):
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None, store.cleanup_outbound_translations,
                            )
                        except Exception:
                            logger.debug(
                                "[AutosendWorker] cleanup_outbound_translations 失败",
                                exc_info=True)
                    self._last_cleanup_ts = time.time()
            except Exception:
                logger.debug("[AutosendWorker] cleanup_old_drafts 失败", exc_info=True)

    async def _deliver_one(self, item: Dict[str, Any]) -> None:
        """投递单条已 resolve 草稿（翻译→近重复守卫→拟人延迟→过期复查→发送→失败处置）。

        2026-08-09 从 _tick 的串行 for 循环整体抽出（并行化前置）。        并发语义边界：
        **同会话必须串行**（调度层 _deliver_parallel 按会话分组保证——顺序与
        防双发不变量都建立在会话内有序上）；跨会话可并发——本方法更新的共享
        状态（计数器/重试队列/封禁表/dup 登记）全部在事件循环单线程语义下写入，
        无跨线程共享。原 for 循环体的 continue 在此为 return（单条早退）。
        """
        # B41 投递幂等钉：同 (会话,草稿) 同文只许成功出门一次。拿不到投递权＝
        # 另一条链在途 / 同稿已投过（复活行 revive 双触发）→ 静默跳过，不算错误。
        # 重试项 (_attempt>0) 豁免——与 dup_guard/fresh_guard 同口径：重试项只在
        # 上一轮**失败**（未登记指纹）后存在，重发同文本是 recoverable 既定语义。
        _do_conv = str(item.get("conversation_id") or "")
        _do_did = str(item.get("draft_id") or "")
        _do_text = str(item.get("text", ""))
        _do_retry = int(item.get("_attempt", 0)) > 0
        if not _do_retry:
            _claim_err = self._deliver_once.claim(_do_conv, _do_did, _do_text)
            if _claim_err:
                self.total_skipped_already_sent += 1
                logger.warning(
                    "[AutosendWorker] guard=deliver_once 拒绝重复投递 draft=%s "
                    "conv=%s reason=%s（B41 同稿双投防线）",
                    _do_did, _do_conv, _claim_err)
                return
        _delivered_ok = False
        _dup_token = 0  # 出站登记 token（失败撤销用），须在 try 外初始化
        try:
            # 出站翻译：投递前把 AI 中文回复译成客户语言（补「全自动聊天翻译」闭环）。
            # 无兜底纪律（2026-08-17）：需要翻译而翻译失败＝HOLD 不发（回调内部
            # 已把全部失败面收成 None；此处回调**自身抛异常**同样按 HOLD 处理，
            # 旧「异常发原文」拆除）→ 按投递失败进重试队列，翻译链恢复后自动补投。
            send_text = str(item.get("text", ""))
            if self._translate_callback is not None:
                _tx = send_text
                try:
                    _tx = await self._translate_callback(item)
                except Exception:
                    logger.warning(
                        "[AutosendWorker] 出站翻译回调异常 → HOLD 不发 conv=%s",
                        item.get("conversation_id", "?"), exc_info=True)
                    _tx = None
                # M-1 B #234（D-M3）：HOLD 原因随失败链透出（lang_unknown 转人工 /
                # target_lang_mismatch 等校验失败标「翻译失败待确认」），任何情况不发原文。
                if _tx is None:
                    raise RuntimeError(_translate_hold_message(item))
                if _tx:
                    if _tx != send_text:
                        self.total_translated += 1
                    send_text = _tx
            # 出站近重复守卫（2026-08-02）：客户短时间连发多条 → 两次独立
            # LLM 生成互不知情 → 同义双发。投递前与最近出站（DB + 在途登记）
            # 最后核对一次，命中即静默跳过——不算投递错误、不喂熔断（重复
            # 是「多余」不是「故障」）。重试项 (_attempt>0) 免检：重发同文本
            # 是 recoverable 的既定语义。放行后**乐观登记**待发文本（先于
            # humanize 延迟窗），让并行在途的下一条立即可见；失败即撤销。
            _conv_id_g = str(item.get("conversation_id") or "")
            if (self._dup_guard_cfg.get("enabled")
                    and int(item.get("_attempt", 0)) == 0):
                from src.inbox.outbound_dup_guard import (
                    outbound_registry as _dup_reg,
                    record_dup_check as _dup_rec,
                )
                # #144：similar 档只比对「本稿所答的最新入站」之后的出站——上一轮
                # 的回复与本稿相近是两轮各答一次，不是双发（英文/西语两会话实锤：
                # 客户新消息的回复被 119s/88s 前上一轮回复拦下 → 零回复）。
                _rep = self._dup_guard_report(item, send_text)
                _hit = _rep.get("hit")
                _cross = _rep.get("cross_round_similar")
                _since = float(_rep.get("since_ts") or 0.0)
                _rows = _rep.get("rows") or []
                _lvl = (_hit or {}).get("level", "")
                _block = bool(_hit) and (
                    _lvl == "dup"
                    or bool(self._dup_guard_cfg.get("block_similar", True)))
                if _cross and not _block:
                    self.total_dup_cross_round_released += 1
                    logger.info(
                        "[AutosendWorker] guard=near_duplicate 跨轮相近放行 conv=%s "
                        "sim=%.2f age=%.0fs matched=%r（匹配出站早于本轮入站 %.0fs，"
                        "两轮各答一次非双发 #144）",
                        _conv_id_g, _cross.get("similarity", 0.0),
                        _cross.get("age_sec", 0.0),
                        str(_cross.get("matched_text", ""))[:60],
                        max(0.0, _since - float(_cross.get("matched_ts") or 0.0)))
                try:
                    _dup_rec(_lvl, source="autosend", blocked=_block,
                             released_cross_round=bool(_cross and not _block))
                except Exception:
                    pass
                if _block:
                    _same_round = bool((_hit or {}).get("same_round", True))
                    logger.warning(
                        "[AutosendWorker] guard=near_duplicate 出站近重复"
                        "拦截 conv=%s level=%s round=%s sim=%.2f age=%.0fs matched=%r",
                        _conv_id_g, _lvl, "same" if _same_round else "cross",
                        _hit.get("similarity", 0.0),
                        _hit.get("age_sec", 0.0),
                        str(_hit.get("matched_text", ""))[:60])
                    # impl85 阶段3：拦下后换说法重试一次（重写稿再过守卫），
                    # 通过才继续投递；仍雷同/无重写链 → 跳过本条，但绝不静默：
                    # 结局进 WARNING 日志，客户在等的会话打「需人工」（#144）。
                    _rw = await self._try_dup_rewrite(
                        item, send_text, _hit, since_ts=_since, rows=_rows)
                    if not _rw:
                        self.total_dup_blocked += 1
                        if _same_round:
                            self.total_dup_blocked_same_round += 1
                        else:
                            self.total_dup_blocked_cross_round += 1
                        self._dup_blocked_escalate(item, _hit, _rows)
                        return
                    self.total_dup_rewritten += 1
                    logger.info(
                        "[AutosendWorker] dup 拦截后换说法重试成功 conv=%s "
                        "len %d→%d（重写稿已再过守卫）",
                        _conv_id_g, len(send_text), len(_rw))
                    send_text = _rw
                _dup_token = _dup_reg.register(_conv_id_g, send_text)
            # 拟人序列（已读 → 打字续挂 → 延迟）：统一走 humanize 协作器，
            # 与 L3 缓冲话术共用同一节奏。对端视角：已读 → 正在输入 → 收到回复。
            # best-effort：mark_read/typing 失败不阻断投递（协作器内部吞异常）。
            _plat = item.get("platform", "")
            _acc = item.get("account_id", "default")
            _ck = item.get("chat_key", "")
            # 自适应延迟按实际要发的文本长度估时，并扣除草稿创建至今的已耗时
            # （adaptive=true 时生效；总响应时长目标而非叠加）。
            _created = float(item.get("created_ts") or 0)
            _elapsed = max(0.0, time.time() - _created) if _created > 0 else 0.0
            # Q-3（#264 C/D）：进入拟人等待即登记在途——切档路由 / 坐席打字 / 坐席发送
            # 可在等待期间点名取消（cancel_inflight），等待结束下方复检读到信号即放弃。
            self._inflight_register(item)
            try:
                await self._run_humanize(
                    _plat, _acc, _ck, text=send_text, elapsed_sec=_elapsed,
                    conversation_id=_conv_id_g,
                    inbound_text=str(item.get("peer_text") or ""))
                # Q-3（#264 C）真发前二次复检**第二处**（与捞稿期同一函数）：拟人等待可达
                # 30–60s，此间坐席切手动 / 打字 / 发送、会话被风险持有或打「需人工」——
                # 旧链只在等待**之前**过闸、等待后只认**客户**插话（下方 fresh_guard），
                # 于是 XBGPBN 23:56:20 切了手动、人在打字，AI 稿照发。任一不满足即放弃：
                # 撤出站登记、行 approved→cancelled（decided_by=abort:<code>）、不喂熔断。
                _hp_abort = self._human_priority_gate(
                    _conv_id_g, draft_id=_do_did, is_farewell=bool(item.get("_farewell")))
                # Q-18 B：点名取消信号（cancel_inflight 已把行改 cancelled）来的原因码不能 defer——
                # 只有「状态判定」出的 agent_sent / agent_typing 才让位
                with self._hp_lock:
                    _hp_signalled = _do_did in self._inflight_cancel
            finally:
                self._inflight_unregister(_do_did)
            # Q-18 B（#292）：拟人等待期间坐席发了 / 在打字 → **让位不丢稿**：撤出站登记、
            # 行保持 approved，载荷带 _yield_defer 进 _retry_queue 按 until 改期；到点重走本函数
            # （再过闸 + fresh_guard：坐席又活动 → 再让位；期间客户又说了 → 新稿覆盖；
            # 切手动 → mode_changed 老路取消）。连续让位超上限 → 按放弃走下方老路。
            if _hp_abort in _YIELD_DEFER_REASONS and not _hp_signalled:
                _y_until, _y_exhausted = self._yield_mark_defer(
                    _conv_id_g, _hp_abort, _do_did, stage="presend")
                if not _y_exhausted:
                    if _dup_token:
                        try:
                            from src.inbox.outbound_dup_guard import (
                                outbound_registry as _dup_reg_y,
                            )
                            _dup_reg_y.unregister(_conv_id_g, _dup_token)
                        except Exception:
                            logger.warning(
                                "[AutosendWorker] 出站去重撤登记失败 conv=%s（≤600s 内可能误判重复）",
                                _conv_id_g, exc_info=True)
                    _yitem = dict(item)
                    _yitem["_yield_defer"] = True
                    _yitem["_yield_by"] = _hp_abort
                    _yitem["_yield_deferrals"] = int(item.get("_yield_deferrals", 0)) + 1
                    self._retry_queue.append({"item": _yitem, "next_ts": _y_until + 0.5})
                    return
            elif not _hp_abort and _conv_id_g in self._yield_defer:
                self._yield_resume_if_deferred(_conv_id_g, _do_did, stage="presend")
            if _hp_abort:
                self.total_abort_recheck += 1
                if _dup_token:
                    try:
                        from src.inbox.outbound_dup_guard import (
                            outbound_registry as _dup_reg_hp,
                        )
                        _dup_reg_hp.unregister(_conv_id_g, _dup_token)
                    except Exception:
                        logger.warning(
                            "[AutosendWorker] 出站去重撤登记失败 conv=%s（≤600s 内可能误判重复）",
                            _conv_id_g, exc_info=True)
                try:
                    _hp_store = getattr(self._svc, "_store", None)
                    if _hp_store is not None and hasattr(_hp_store, "update_draft_status"):
                        try:
                            _hp_store.update_draft_status(
                                _do_did, status="cancelled",
                                decided_by=f"abort:{_hp_abort}"[:40],
                                expected_statuses=("approved", "pending"))
                        except TypeError:   # 旧签名 / 测试替身无 expected_statuses
                            _hp_store.update_draft_status(
                                _do_did, status="cancelled",
                                decided_by=f"abort:{_hp_abort}"[:40])
                except Exception:
                    logger.debug("[AutosendWorker] 复检放弃后改 cancelled 失败 draft_id=%s",
                                 _do_did, exc_info=True)
                logger.info("[autosend] abort=%s stage=presend draft=%s conv=%s（拟人等待后复检，未发）",
                            _hp_abort, _do_did, _conv_id_g)
                self._ledger_abort(_conv_id_g, _hp_abort, stage="presend", draft_id=_do_did)
                return
            # 延迟后二次过期复查（fresh_guard 同闸门，2026-08-05）：拟人延迟
            # 现可配 30-60s，而 _process_batch 的过期检查跑在延迟**之前**——
            # 整个延迟窗口原本不设防：客户此间插话，旧稿照发＝答非所问，
            # 且插话催生的新稿接踵而至＝两连发。判据与创建侧同一纯函数
            # find_superseding_inbound（同文本/过短不拦——保证「拦下后一定
            # 有新稿覆盖两问」）；重试项免检（recoverable 既定语义）；
            # 守卫自身异常一律放行（宁发旧稿不可断链）。草稿此时已 resolve，
            # 只跳过投递不回滚状态——与「resolve-先于-deliver」既定语义一致，
            # 计 total_superseded 不计 error（竞态非故障，不喂熔断器）。
            if (self._fresh_guard_cfg.get("enabled") and _conv_id_g
                    and int(item.get("_attempt", 0)) == 0):
                try:
                    from src.inbox.draft_fresh_guard import (
                        find_superseding_inbound as _fsi_ph,
                    )
                    _ph_store = getattr(self._svc, "_store", None)
                    _ph_ts = float(item.get("created_ts") or 0)
                    _ph_hit = None
                    if (_ph_store is not None and _ph_ts > 0
                            and hasattr(_ph_store, "list_recent_messages")):
                        _ph_hit = _fsi_ph(
                            _ph_store.list_recent_messages(
                                _conv_id_g, limit=8),
                            draft_ts=_ph_ts,
                            peer_text=str(item.get("peer_text") or ""),
                            grace_sec=float(
                                self._fresh_guard_cfg.get("grace_sec", 3.0)),
                            min_text_len=int(
                                self._fresh_guard_cfg.get("min_text_len", 0)),
                        )
                    if _ph_hit is not None:
                        self.total_superseded += 1
                        if _dup_token:
                            try:
                                from src.inbox.outbound_dup_guard import (
                                    outbound_registry as _dup_reg_ph,
                                )
                                _dup_reg_ph.unregister(
                                    _conv_id_g, _dup_token)
                            except Exception:
                                logger.warning(
                                    "[AutosendWorker] 出站去重撤登记失败 conv=%s（≤600s 内可能误判重复）",
                                    _conv_id_g, exc_info=True)
                        logger.info(
                            "[AutosendWorker] guard=fresh post_humanize "
                            "延迟窗内客户插话，放弃本条投递 draft=%s conv=%s "
                            "（等新稿覆盖两问）",
                            item.get("draft_id", ""), _conv_id_g)
                        return
                except Exception:
                    logger.debug(
                        "[AutosendWorker] guard=fresh 延迟后复查异常（放行）",
                        exc_info=True)
            # original_text 透传（签名探测一次并缓存）：语音分支须用翻译前原文
            # 判定+合成；旧 4 参回调（含测试桩）不受影响。
            send_text = self._apply_compliance_disclosure(_conv_id_g, send_text)
            _send_kw: Dict[str, Any] = self._send_cb_kwargs(
                str(item.get("text", "")))
            res = await self._send_callback(
                item.get("platform", ""), item.get("account_id", "default"),
                item.get("chat_key", ""), send_text, **_send_kw,
            )
            # 投递失败判定：除显式 ok=False 外，编排器/出站闸门返回
            # {delivered: False} 或 {blocked: ...}（如 kill-switch/send-gate 拦截、
            # 桌面出站被闸门拒）也算未送达——否则会把「被拦截」误计为已送达刷指标。
            # 实施86 域B-1：结构化字段（error_kind/retry_after_ms）随异常携带，
            # 失败分支据此决定「按边车提示改期」还是终局留痕。
            if isinstance(res, dict) and (
                res.get("ok") is False
                or res.get("delivered") is False
                or res.get("blocked")
            ):
                raise UndeliveredError(
                    str(res.get("error") or res.get("blocked") or "send not ok"),
                    error_kind=str(res.get("error_kind") or ""),
                    retry_after_ms=int(res.get("retry_after_ms") or 0))
            _delivered_ok = True
            self.total_delivered += 1
            # M-2：真实送达 → 账号门禁清连续失败 / 退避（通道显然通了）
            self._account_gate_note(item, ok=True)
            # #88：自动投递送达成功 → 清 dead-peer 标（黄条解除 + 自动回复恢复）
            self._dead_peer_clear(_conv_id_g)
            # #207：AI 回上了 → 摘「AI 没能回」类「需人工」标（dup_guard_blocked 等；
            # crisis / high_risk / 人工标由 auto_clear 内部判定保留）。best-effort。
            try:
                from src.integrations.protocol_autoreply import auto_clear_needs_human
                auto_clear_needs_human(getattr(self._svc, "_store", None),
                                       _conv_id_g, trigger="autosend_delivered")
            except Exception:
                logger.debug("[AutosendWorker] 自动摘「需人工」失败（忽略）", exc_info=True)
            # B41：成功投递补写 DB sent_at（best-effort）——跨重启的「已投过」
            # 证据 + 价值周报的 inbox 投递计数从此有真值。
            try:
                _st_mark = getattr(self._svc, "_store", None)
                if _st_mark is not None and hasattr(_st_mark, "mark_draft_sent"):
                    _st_mark.mark_draft_sent(_do_did)
            except Exception:
                logger.debug("[AutosendWorker] mark_draft_sent 失败（忽略）",
                             exc_info=True)
            # 2026-08-19 Token P5b 影子计数（观测非计费）：自动链投递点口径，
            # 与出稿点对读校准「出稿/投递」比值。fail-silent，总闸关=零行为。
            try:
                from src.licensing.token_ledger import record_shadow

                record_shadow("ai_reply_delivered_auto")
            except Exception:
                logger.warning("[AutosendWorker] 影子计量 ai_reply_delivered_auto 记账失败",
                               exc_info=True)
            self._note_conv_sent(_conv_id_g)
            if int(item.get("_attempt", 0)) > 0:
                self.total_retry_recovered += 1
            try:   # P4 埋点：AI 承接（该会话首条自动回复投递成功，进程内每会话一次）
                from src.utils.telemetry import track_once
                _tele_cid = str(item.get("conversation_id") or "")
                track_once(f"ai_engaged:{_tele_cid}", "session.ai_engaged", {
                    "session_id": _tele_cid,
                    "first_response_ms": int(_elapsed * 1000)})
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self.total_deliver_errors += 1
            self.last_error = f"deliver: {exc}"
            _conv = str(item.get("conversation_id", "") or "?")
            # 投递失败 → 撤销出站登记（防登记幽灵把后续重试当重复拦住）
            if _dup_token:
                try:
                    from src.inbox.outbound_dup_guard import (
                        outbound_registry as _dup_reg2,
                    )
                    _dup_reg2.unregister(_conv, _dup_token)
                except Exception:
                    # 撤登记失败不会永久卡死（registry 有 600s TTL 剪枝），但该会话在
                    # 这段窗口内可能被误判为「已有在途出站」而拦掉下一条 —— 对外表现
                    # 是「客户没收到回复」，值得留痕以便与投诉对时间。
                    logger.warning("[AutosendWorker] 出站去重撤登记失败 conv=%s（≤600s 内可能误判重复）",
                                   _conv, exc_info=True)
            _plat = item.get("platform", "?")
            _permanent = _is_permanent_send_error(str(exc))
            # M-2（D-M1 ⑦ / #233）：账号门禁记账——真实失败计连续失败（3 次 → 降半自动
            # + 红标）；边车退避类（send_backoff / account_blocked）只记账号级退避水位、
            # 不计失败（通道自保不是新故障）。translate_hold 是我方翻译链扣留，不算通道失败。
            if not str(exc).startswith("translate_hold"):
                self._account_gate_note(
                    item, ok=False,
                    error_kind=str(getattr(exc, "error_kind", "") or ""),
                    retry_after_ms=int(getattr(exc, "retry_after_ms", 0) or 0))
            # 跨链共享登记（gated）：注销/被拉黑/无权限这类**永久**不可达写进
            # 共享表，A 线 sender 与 proactive 立即同步受益；peer 失效等可自愈类
            # 由 registry 按「非永久」忽略，仍只走下面的本地会话冷却。
            if _permanent:
                self._dead_peer_record(_conv, str(exc))
            # 永久性错误（无发言权/被拉黑/会话失效）→ 会话进封禁冷却；仅「首次进入
            # 封禁」打一条 WARNING，冷却窗口内的后续同类失败降级 debug（防刷屏）。
            if (self._send_block_cooldown_sec > 0
                    and _conv != "?"
                    and _permanent):
                _already = self._conv_send_blocked(_conv)
                self._blocked_conv_until[_conv] = (
                    time.time() + self._send_block_cooldown_sec)
                if _already:
                    logger.debug(
                        "[AutosendWorker] 会话仍处发送封禁 conv=%s: %s", _conv, exc)
                else:
                    logger.warning(
                        "[AutosendWorker] 投递永久失败，暂停会话自动发 %.0fs "
                        "conv=%s platform=%s: %s",
                        self._send_block_cooldown_sec, _conv, _plat, exc)
            else:
                logger.warning(
                    "[AutosendWorker] 投递失败 conv=%s platform=%s: %s",
                    _conv, _plat, exc,
                )
            # 实施86 域B-1（#49）：边车给了 retry_after_ms（限频退避/临时冻结的
            # 确定性恢复时刻）→ 按提示改期重投，而不是当场终局失败白丢草稿。
            # 与 recoverable 通用重试正交（那是盲重试，默认关；这是按平台说的
            # 时间等，不受 recoverable 开关约束）。超长冻结（>15min，如风控 2h）
            # 不改期——直接走下方终局留痕让坐席看见，别让草稿滞留数小时。
            from src.inbox.send_failure_class import plan_failure_retry
            _defer_plan, _defer_delay = plan_failure_retry(
                hint_ms=int(getattr(exc, "retry_after_ms", 0) or 0),
                deferrals_used=int(item.get("_deferrals", 0)),
                permanent=_permanent)
            if _defer_plan == "defer":
                _ditem = dict(item)
                _ditem["_deferrals"] = int(item.get("_deferrals", 0)) + 1
                self._retry_queue.append(
                    {"item": _ditem, "next_ts": time.time() + _defer_delay})
                self.total_deferred += 1
                logger.info(
                    "[AutosendWorker] 投递被限频/冻结（%s），按平台提示 %.0fs 后"
                    "改期重投（第%d次改期）conv=%s",
                    getattr(exc, "error_kind", "") or "retry_after",
                    _defer_delay, _ditem["_deferrals"], _conv)
                return  # 改期中：本条暂不记 autosend_failed / 不留痕
            # Sprint2 可恢复：瞬时失败且未耗尽 → 排入重试队列（指数退避），不记 failed、
            # 不 re-resolve（幂等重发同文本）。永久错误不重试（会话已进封禁冷却）。
            _attempt = int(item.get("_attempt", 0))
            if (self._recoverable and not _permanent
                    and _attempt + 1 < self._retry_max_attempts):
                _delay = min(
                    self._retry_backoff_base * (2 ** _attempt),
                    self._retry_backoff_max)
                _ritem = dict(item)
                _ritem["_attempt"] = _attempt + 1
                self._retry_queue.append(
                    {"item": _ritem, "next_ts": time.time() + _delay})
                self.total_retry_scheduled += 1
                logger.info(
                    "[AutosendWorker] 投递瞬时失败，%.0fs 后第%d次重试 conv=%s",
                    _delay, _attempt + 1, _conv)
                return  # 重试中：本条暂不记 autosend_failed
            if self._recoverable and not _permanent:
                self.total_retry_exhausted += 1
            # 写 autosend_failed 审计，让安全条/记录弹窗看见「自动发了但没送达」
            try:
                rec = getattr(self._svc, "record_autosend_failure", None)
                if rec is not None:
                    rec(
                        item.get("draft_id", ""),
                        conversation_id=item.get("conversation_id", ""),
                        reason=f"平台投递失败: {exc}",
                    )
            except Exception:
                logger.debug("[AutosendWorker] autosend_failed 审计写入失败", exc_info=True)
            # B63③（实施64 P1-4，实录 `_320`）：终局失败在会话消息流留痕
            # （direction=out status=failed）——坐席在聊天里看得见「这条没发出去」
            # 并可一键重发；此前失败内容零留痕直接消失，用户只能翻全自动记录。
            try:
                mir = getattr(self._svc, "record_failed_outbound_mirror", None)
                if mir is not None:
                    # 实施72 P3：原因码随留痕落库（气泡自解释「为什么失败」——
                    # send_gate:daily_cap / kill_switch / needs_login…）。
                    # 旧 svc 无 reason 形参 → TypeError 回落旧调用。
                    try:
                        mir(str(item.get("conversation_id") or ""),
                            str(item.get("text") or ""),
                            reason=str(exc)[:200])
                    except TypeError:
                        mir(str(item.get("conversation_id") or ""),
                            str(item.get("text") or ""))
            except Exception:
                logger.debug("[AutosendWorker] 投递失败留痕写入失败（忽略）", exc_info=True)
            # Sprint2：开启 recoverable 时，永久/耗尽失败发实时提醒事件，坐席可补发。
            if self._recoverable:
                self._publish_deliver_failed(item, str(exc), permanent=_permanent)
        finally:
            # B41：释放在途位；成功才登记已投指纹（transient 失败释放后重试
            # 可再 claim——重发同文本是 recoverable 的既定语义）。重试项未
            # claim 过，但 release 幂等（pop 不存在的键无害），成功仍登记指纹。
            self._deliver_once.release(
                _do_conv, _do_did, delivered=_delivered_ok, text=_do_text)


    async def _deliver_parallel(self, deliver_now: List[Dict[str, Any]]) -> None:
        """按会话分组并发投递（inbox.l2_autosend.parallel_deliver，默认关）。

        分组键=conversation_id（缺失则独占一组）：同会话内条目保持批内顺序串行；
        组间经全局信号量（max_concurrent，默认 3）并发——30-54s 拟人延迟与
        分条间隔不再让并发会话互相排队。gather(return_exceptions=True) 兜底：
        _deliver_one 自吞业务异常，这里只记录意外崩溃，绝不让单组炸掉整轮 tick。
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for idx, it in enumerate(deliver_now):
            key = str(it.get("conversation_id") or "") or f"__solo_{idx}"
            groups.setdefault(key, []).append(it)
        sem = asyncio.Semaphore(self._parallel_max)

        async def _run_group(rows: List[Dict[str, Any]]) -> None:
            async with sem:
                for row in rows:
                    await self._deliver_one(row)

        self.parallel_batches += 1
        results = await asyncio.gather(
            *(_run_group(rows) for rows in groups.values()),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, BaseException):
                logger.error("[AutosendWorker] 并行投递组异常: %s", res,
                             exc_info=res)

    def _publish_deliver_failed(self, item: Dict[str, Any], reason: str,
                                *, permanent: bool) -> None:
        """Sprint2：发「autosend 投递失败」实时事件，供工作台铃铛/webhook 提醒坐席补发。

        best-effort，绝不抛。仿 sla_watcher 的 event_bus 提醒范式（recon 建议通道）。
        """
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("autosend_deliver_failed", {
                "draft_id": str(item.get("draft_id") or ""),
                "conversation_id": str(item.get("conversation_id") or ""),
                "platform": str(item.get("platform") or ""),
                "permanent": bool(permanent),
                "reason": str(reason)[:200],
                "text_preview": str(item.get("text") or "")[:80],
                "ts": time.time(),
            })
        except Exception:
            logger.debug("[AutosendWorker] 投递失败事件发布失败（忽略）", exc_info=True)

    def _process_batch(self) -> "tuple[int, int, List[Dict[str, Any]]]":
        """同步：列出 L2 pending 草稿并逐条 resolve（DB 标记 + 审计）。每条隔离 try/except。

        返回 (sent, errors, to_deliver)：to_deliver 是已成功 resolve、需要真正投递到平台的
        草稿载荷列表（platform/account_id/chat_key/text）。投递本身在 async 的 _tick 里做。
        """
        drafts = self._svc.list_drafts(status="pending", limit=200)
        # 捞稿只认草稿行上由 src/inbox/autosend_policy.decide 写下的档位（#160 v2 单一
        # 入口不变量）：这里**不**按 risk_level/关键词再算一遍——两处各算一套就会出现
        # 「台账说放行了、实际还是被拦」的不可归因状态。
        l2 = [d for d in drafts if d.get("autopilot_level") == "L2"]
        sent, errors = 0, 0
        to_deliver: List[Dict[str, Any]] = []
        # B41 通道互斥仲裁（2026-08-22）：同一会话同批**既有常规回复稿又有跟进链
        # 自动稿（source_id 前缀 wf:）**＝两条出稿通道对同一客户各写了一份回复，
        # 背靠背发出就是「同问双答」事故（impl49 B41 _241 实录）。仲裁规则＝
        # 常规稿优先（它回应真实入站），链稿让位取消——链的节奏拍由 runner 的
        # defer 机制平移，不在这里补偿。仅投递模式启用（标记模式双标无害）。
        _mutex_cancel: set = set()
        if self._send_callback is not None and len(l2) > 1:
            _by_conv: Dict[str, List[Dict[str, Any]]] = {}
            for _d in l2:
                _c = str(_d.get("conversation_id") or "")
                if _c:
                    _by_conv.setdefault(_c, []).append(_d)
            for _c, _rows in _by_conv.items():
                if len(_rows) < 2:
                    continue
                _wf_rows = [r for r in _rows if str(
                    r.get("source_id") or "").startswith("wf:")]
                if _wf_rows and len(_wf_rows) < len(_rows):
                    _mutex_cancel.update(
                        str(r.get("draft_id") or "") for r in _wf_rows)
        # 复班补觉每批重拟预算：防复班瞬间对整夜积压一次性打满 LLM；超预算的
        # 留 pending，下一 tick 继续（min_interval 节拍天然把补觉摊开）。
        catchup_budget = 5
        for d in l2:
            draft_id = d.get("draft_id", "")
            _conv = str(d.get("conversation_id") or "")
            # B41 通道互斥：链稿让位（常规稿本轮在场）→ 取消防双答
            if draft_id and draft_id in _mutex_cancel:
                try:
                    _store_mx = getattr(self._svc, "_store", None)
                    if _store_mx is not None and hasattr(
                            _store_mx, "update_draft_status"):
                        _store_mx.update_draft_status(
                            draft_id, status="cancelled",
                            decided_by="channel_mutex")
                except Exception:
                    logger.debug(
                        "[AutosendWorker] channel_mutex 取消链稿失败 draft_id=%s",
                        draft_id, exc_info=True)
                self.total_skipped_mutex += 1
                logger.info(
                    "[AutosendWorker] guard=channel_mutex 跟进链稿让位常规稿 "
                    "draft=%s conv=%s（同会话同批双通道出稿，防同问双答）",
                    draft_id, _conv)
                continue
            # O-1 A（#252 #253 · D-O1）停联 / 自伤硬停门禁——本 worker **唯一**一处：
            # 会话已冻结（stop_contact.frozen_reason 非空）→ 只放行带 HARD_STOP_PASS_MARK /
            # FAREWELL_MARK 的「最多一条」（停联告别 / 自伤那一句陪伴），其余 L2 稿一律取消
            # （decided_by=stop_contact_frozen）。放行稿同时豁免下方「档位已降级」取消（冻结时
            # 档位已被按成 manual，否则这一条也会被它扫掉）。
            # 判定只读 stop_contact 模块，不按 risk_level 再算一遍档位。fail-open：判定异常放行。
            _store_sc = getattr(self._svc, "_store", None)
            _is_farewell = False
            if _store_sc is not None and _conv:
                try:
                    from src.inbox.stop_contact import (
                        frozen_reason as _sc_frozen, is_hard_stop_pass_draft as _sc_is_pass,
                        log_action as _sc_log,
                    )
                    _frz = _sc_frozen(_store_sc, _conv)
                except Exception:
                    _frz, _sc_is_pass, _sc_log = "", None, None
                if _frz:
                    _is_farewell = bool(_sc_is_pass(d)) if _sc_is_pass else False
                    if _is_farewell:
                        _sc_log("farewell" if _frz == "stop_contact" else "one_reply",
                                conversation_id=_conv, reason=_frz,
                                draft_id=str(draft_id), extra="stage=worker_deliver")
                    else:
                        try:
                            if hasattr(_store_sc, "update_draft_status"):
                                _store_sc.update_draft_status(
                                    draft_id, status="cancelled",
                                    decided_by="stop_contact_frozen")
                        except Exception:
                            logger.debug(
                                "[AutosendWorker] 取消冻结会话草稿失败 draft_id=%s",
                                draft_id, exc_info=True)
                        self.total_skipped_stop_contact += 1
                        _sc_log("skipped", conversation_id=_conv, reason=_frz,
                                draft_id=str(draft_id), extra="stage=worker_cancel")
                        continue
            # Sprint1 统一出站闸门 → Q-3（#264 C）人工优先复检**第一处**：会话被显式降级
            # （接管→manual / 改 review 等）/ 坐席 60s 内打字或发送 / 会话级风险持有 /
            # 「需人工」标在场 → 本稿不 resolve、取消。同一函数在 _deliver_one 拟人等待后
            # 再调一次（第二处）——闸只在等待之前 = XBGPBN「切手动了还发」的根因之一。
            # 仅对显式设过档位者生效；未显式设置(None)不干预 → 不改既有默认行为/perf 测试。
            _store_rt = getattr(self._svc, "_store", None)
            _hp_abort = self._human_priority_gate(
                _conv, draft_id=str(draft_id), is_farewell=_is_farewell)
            # Q-18 B（#292）：agent_sent / agent_typing = **让位不丢稿**——稿留 pending（不
            # resolve 不取消），登记 until=坐席最后活动+60s，窗过下一 tick 自然再过闸即发
            # （期间有更新入站 → 下方 fresh_guard 让新稿覆盖）。连续让位超上限才按放弃走老路。
            # mode_changed / risk_hold / needs_human 仍是放弃，下方逻辑一字不变。
            if _hp_abort in _YIELD_DEFER_REASONS:
                _y_until, _y_exhausted = self._yield_mark_defer(
                    _conv, _hp_abort, str(draft_id), stage="batch")
                if not _y_exhausted:
                    continue
            elif not _hp_abort and _conv in self._yield_defer:
                self._yield_resume_if_deferred(_conv, str(draft_id), stage="batch")
            if _hp_abort:
                _decided_by = ("mode_downgraded" if _hp_abort == "mode_changed"
                               else f"abort:{_hp_abort}")
                try:
                    if _store_rt is not None and hasattr(_store_rt, "update_draft_status"):
                        _store_rt.update_draft_status(
                            draft_id, status="cancelled", decided_by=_decided_by)
                except Exception:
                    logger.debug(
                        "[AutosendWorker] 取消 L2 失败 draft_id=%s abort=%s", draft_id,
                        _hp_abort, exc_info=True)
                if _hp_abort == "mode_changed":
                    self.total_skipped_mode += 1
                elif _hp_abort in ("risk_hold", "needs_human"):
                    self.total_skipped_risk_hold += 1
                    # 稿没了不能让人也没得审：按 L1 重拟一次（同一次持有只一次）
                    self._risk_hold_regen_once(d, _conv, _hp_abort)
                else:
                    self.total_abort_recheck += 1
                logger.info("[autosend] abort=%s stage=batch draft=%s conv=%s",
                            _hp_abort, draft_id, _conv)
                self._ledger_abort(_conv, _hp_abort, stage="batch", draft_id=str(draft_id))
                continue
            # 会话处于发送封禁冷却 → 不 resolve/投递，直接取消该 pending 草稿（防堆积）。
            # 冷却到期后新草稿会重新尝试（权限恢复即自动回正常）。
            if self._send_callback is not None and self._conv_send_blocked(_conv):
                try:
                    _store = getattr(self._svc, "_store", None)
                    if _store is not None and hasattr(_store, "update_draft_status"):
                        _store.update_draft_status(
                            draft_id, status="cancelled", decided_by="send_blocked")
                except Exception:
                    logger.debug(
                        "[AutosendWorker] 取消封禁会话草稿失败 draft_id=%s", draft_id,
                        exc_info=True)
                self.total_skipped_blocked += 1
                continue
            # M-2 账号级通道门禁（D-M1 / #232 / #233，2026-09-06）：登录冷静期 / 连续失败
            # 降级 / 边车退避 / 通道未连接 → **留 pending 不投递**（不 resolve 不取消：
            # 冷静期内它们就是「积压待你过目」，账号栏与草稿面板可见可批可拒；通道
            # 恢复且门禁解除后下一 tick 自然接续）。判定单点＝account_channel_gate +
            # platform_session_health（与封顶层 gate_caps / 账号栏快照同源）。fail-open：
            # 判定异常一律放行；同账号同原因 60s 只落一条 INFO 防刷屏。
            if self._send_callback is not None:
                _ag_hold = self._account_gate_hold(
                    str(d.get("platform") or ""), str(d.get("account_id") or "default"))
                if _ag_hold:
                    self.total_skipped_channel_gate += 1
                    continue
            # 驾驶权互斥锁（surface_fusion P0，2026-08-13，默认关）：该账号的
            # 自动化持有者是「原生面板」→ 工作台自动链让位，取消本稿防双发
            # （语义与 send_blocked 同族：cancel 防堆积；切回 workspace 托管后
            # 新草稿照常自动发）。人工通过走 deliver_human_approved 不经本闸。
            # 判定 fail-open：guard 异常一律放行——锁故障绝不闸死自动回复。
            if self._send_callback is not None and self._pilot_guard is not None:
                _pg_block = False
                try:
                    _pg_block = bool(self._pilot_guard(
                        str(d.get("platform") or ""),
                        str(d.get("account_id") or "default")))
                except Exception:
                    logger.debug(
                        "[AutosendWorker] 驾驶权判定异常（放行）", exc_info=True)
                if _pg_block:
                    try:
                        _store_pg = getattr(self._svc, "_store", None)
                        if _store_pg is not None and hasattr(
                                _store_pg, "update_draft_status"):
                            _store_pg.update_draft_status(
                                draft_id, status="cancelled",
                                decided_by="pilot_native")
                    except Exception:
                        logger.debug(
                            "[AutosendWorker] 取消原生托管账号草稿失败 draft_id=%s",
                            draft_id, exc_info=True)
                    self.total_skipped_pilot += 1
                    continue
            # 工作时间闸（inbox.work_schedule，2026-08-04，默认关）：账号休息中
            # → 草稿**留 pending 不处置**（不 resolve 不取消——复班后自动接续
            # 投递/补觉重拟），危机消息（severe/elevated）在判定内穿透照发。
            # 人工通过投递走 deliver_human_approved 独立方法，天然不经本闸；
            # 判定 fail-open：任何异常一律放行，闸门故障绝不闸死自动回复。
            if self._ws_provider is not None:
                _ws_hold = ""
                try:
                    from src.inbox.work_hours_gate import (
                        in_work_hours,
                        should_hold_auto_reply,
                    )
                    _ws_cfg_hold = self._ws_provider() or {}
                    _ws_plat = str(d.get("platform") or "")
                    _ws_acct = str(d.get("account_id") or "default")
                    _ws_hold = should_hold_auto_reply(
                        _ws_cfg_hold, _ws_plat, _ws_acct,
                        peer_text=str(d.get("peer_text") or ""))
                    if _ws_hold:
                        # 下班收尾宽限：稿在班内拟出、刚过下班边界（≤15min）
                        # → 放行把最后一句说完——聊到一半突然蒸发比晚几分钟
                        # 下班更穿帮。窗口硬顶防「worker 停摆隔夜稿凌晨漏发」。
                        _ws_created = float(
                            d.get("created_ts") or d.get("created_at") or 0)
                        if (_ws_created > 0
                                and time.time() - _ws_created <= 900
                                and in_work_hours(
                                    _ws_cfg_hold, _ws_plat, _ws_acct,
                                    now_ts=_ws_created)):
                            _ws_hold = ""
                except Exception:
                    _ws_hold = ""
                    logger.debug(
                        "[AutosendWorker] 工作时间闸判定异常（放行）",
                        exc_info=True)
                if _ws_hold:
                    # Q-4 #267 D-Q1：扣留必留痕（同会话同一「到点」只打一条），
                    # 否则老板只看到「不回」查不到「为什么不回」。日志失败不影响扣留。
                    try:
                        from src.inbox.work_hours_gate import log_off_hours_hold
                        _ws_info = log_off_hours_hold(
                            str(d.get("conversation_id") or ""),
                            _ws_cfg_hold, _ws_plat, _ws_acct)
                        # Q-18 D（#293）：扣留同步进拦截台账（同会话同一「到点」只记一条）
                        _ws_stamp = float((_ws_info or {}).get("until_ts") or 0.0)
                        if _conv and self._ledger_ws_stamp.get(_conv) != _ws_stamp:
                            self._ledger_ws_stamp[_conv] = _ws_stamp
                            self._ledger_abort(_conv, "work_schedule", stage="batch",
                                               draft_id=str(draft_id))
                    except Exception:
                        logger.debug("[work_schedule] hold 日志异常（忽略）",
                                     exc_info=True)
                    self.total_skipped_off_hours += 1
                    continue
            # O-1 D（D-O4）首回延迟：首次接触 / 沉寂 >6h 的这一轮，首条回复留 pending
            # 到「沉寂后首条入站 + 60–300s（确定性）」再处置——与班表闸同款「不 resolve
            # 不取消」，下一 tick 接续；不在拟人序列里睡（串行投递会堵别的会话）。
            # 已耗时照扣：放行后 humanize 只剩打字段。危机消息穿透（与班表同口径）。
            # 任何异常 → 放行（判不出就不延）。
            if (self._send_callback is not None and _conv
                    and int(d.get("_attempt", 0)) == 0):
                try:
                    _fr_remain = self._first_reply_remaining(d, _conv)
                except Exception:
                    _fr_remain = 0.0
                    logger.debug("[pacing] first_reply 判定异常（放行）", exc_info=True)
                if _fr_remain > 0:
                    continue
            # 新入站过期守卫（fresh_guard，2026-08-03，默认关）：拟稿的 10-20s 里客户
            # 又补了话 → 本稿按旧输入写成，投出去=答非所问，且 stale_peer 重拟的新稿
            # 随后又到=两连发。判据（draft_fresh_guard.find_superseding_inbound）＝存在
            # **更晚且内容不同、会触发新拟稿**的入站——相同文本/过短文本不拦（那类入站
            # 不会催生新稿，取消本稿=客户没有任何回复）。处置=cancelled/superseded_by_
            # inbound（stale_peer 同族原子闸）；计 skip 不计 error（竞态非故障，不喂
            # 熔断器）。守卫自身任何异常 → 放行投递（宁可发旧稿不可断链）。
            if (self._send_callback is not None
                    and self._fresh_guard_cfg.get("enabled") and _conv):
                try:
                    from src.inbox.draft_fresh_guard import find_superseding_inbound
                    _fg_store = getattr(self._svc, "_store", None)
                    _fg_draft_ts = float(
                        d.get("created_ts") or d.get("created_at") or 0)
                    _fg_hit = None
                    if (_fg_store is not None and _fg_draft_ts > 0
                            and hasattr(_fg_store, "list_recent_messages")):
                        _fg_rows = _fg_store.list_recent_messages(_conv, limit=8)
                        _fg_hit = find_superseding_inbound(
                            _fg_rows,
                            draft_ts=_fg_draft_ts,
                            peer_text=str(d.get("peer_text") or ""),
                            grace_sec=float(
                                self._fresh_guard_cfg.get("grace_sec", 3.0)),
                            min_text_len=int(
                                self._fresh_guard_cfg.get("min_text_len", 0)),
                        )
                    if _fg_hit is not None:
                        try:
                            if hasattr(_fg_store, "update_draft_status"):
                                _fg_store.update_draft_status(
                                    draft_id, status="cancelled",
                                    decided_by="superseded_by_inbound")
                        except Exception:
                            logger.debug(
                                "[AutosendWorker] fresh_guard 作废草稿失败 "
                                "draft_id=%s", draft_id, exc_info=True)
                        self.total_superseded += 1
                        _fg_now = time.time()
                        logger.info(
                            "[AutosendWorker] guard=fresh 新入站过期跳过 draft=%s "
                            "conv=%s 草稿龄=%.1fs 入站晚于拟稿 %.1fs（等新稿覆盖两问）",
                            draft_id, _conv, max(0.0, _fg_now - _fg_draft_ts),
                            float(_fg_hit.get("ts") or 0) - _fg_draft_ts)
                        continue
                except Exception:
                    logger.debug(
                        "[AutosendWorker] fresh_guard 异常（放行投递）", exc_info=True)
            # 复班补觉重拟（work_schedule.off_hours.catch_up，2026-08-04）：
            # 扣留期攒下的稿龄超过 catch_up_regenerate_hours → 原样发会内容穿帮
            # （凌晨拟的「我刚到家」早上发出当场露馅）。作废旧稿 + 经 auto_draft
            # **原产线**重拟（enrich/人设/档位封顶全生效），新稿 created_ts 新鲜、
            # 随后正常投递。铁律：**重拟回调未注入就绝不作废**（宁发陈稿不丢回复）；
            # 作废失败也不重拟（防同会话双稿）。预算外的留 pending 下一 tick 继续。
            if (self._ws_provider is not None
                    and self._catchup_regen_cb is not None
                    and self._send_callback is not None and _conv):
                try:
                    from src.inbox.work_hours_gate import (
                        catch_up_regen_due,
                        off_hours_cfg,
                        schedule_state,
                    )
                    _ws_cfg = self._ws_provider() or {}
                    _oh = off_hours_cfg(_ws_cfg)
                    _peer_txt = str(d.get("peer_text") or "")
                    _draft_ts = float(
                        d.get("created_ts") or d.get("created_at") or 0)
                    # Q-4 #267：阈值 0（默认）= 过夜积压（拟于本班次开始前）全部
                    # 重拟；班次锚点拿不到时 catch_up_regen_due 判 False（宁发陈稿）。
                    _shift = 0.0
                    if _ws_cfg.get("enabled") and _oh.get("catch_up"):
                        _shift = float(schedule_state(
                            _ws_cfg, str(d.get("platform") or ""),
                            str(d.get("account_id") or "default"),
                        ).get("shift_started_ts") or 0)
                    if (_ws_cfg.get("enabled") and _oh.get("catch_up")
                            and _draft_ts > 0 and _peer_txt
                            and catch_up_regen_due(
                                _oh, _draft_ts, shift_started_ts=_shift)):
                        if catchup_budget <= 0:
                            continue  # 本批预算用完，留 pending 下一 tick
                        _cancelled = False
                        try:
                            _cu_store = getattr(self._svc, "_store", None)
                            if (_cu_store is not None
                                    and hasattr(_cu_store, "update_draft_status")):
                                _cu_store.update_draft_status(
                                    draft_id, status="cancelled",
                                    decided_by="work_schedule_regen")
                                _cancelled = True
                        except Exception:
                            logger.debug(
                                "[AutosendWorker] 补觉作废陈稿失败 draft_id=%s"
                                "（留待下一 tick）", draft_id, exc_info=True)
                        if not _cancelled:
                            continue
                        catchup_budget -= 1
                        # Q-3（#264 E）：触发源 reason 随重拟走（autodraft 侧 pop 后落 [draft] trigger）
                        try:
                            from src.inbox.draft_trigger import note as _trig_note
                            _trig_note(_conv, "catchup_regen")
                        except Exception:
                            pass
                        logger.info("[draft] regen conv=%s reason=catchup_regen", _conv)
                        try:
                            self._catchup_regen_cb({
                                "conversation_id": _conv,
                                "platform": str(d.get("platform") or ""),
                                "account_id": str(
                                    d.get("account_id") or "default"),
                                "chat_key": str(d.get("chat_key") or ""),
                            }, _peer_txt)
                            self.total_catchup_regenerated += 1
                            logger.info(
                                "[AutosendWorker] 补觉重拟 conv=%s 稿龄=%.1fh"
                                "（旧稿已作废，新稿走原拟稿产线）",
                                _conv, (time.time() - _draft_ts) / 3600.0)
                        except Exception:
                            logger.warning(
                                "[AutosendWorker] 补觉重拟派发失败 conv=%s"
                                "（旧稿已作废，该会话本轮无回复）",
                                _conv, exc_info=True)
                        continue
                except Exception:
                    logger.debug(
                        "[AutosendWorker] 补觉判定异常（按正常投递）", exc_info=True)
            # 投递用文本优先取最终文本，回落草稿文本
            text = str(d.get("final_text") or d.get("draft_text") or "").strip()
            # 投递模式下，空正文草稿绝不 resolve：否则会被标记 approved/已发，却因
            # text 为空而被跳过投递（sent_at=0、客户收不到），形成「只标记不真发」的静默
            # 丢失，且每会话幂等占位会阻断后续自动回复。留作 pending，等人设产线回填或
            # 人工补全后下一轮再发；同时打 warning 让该异常可见。
            if self._send_callback is not None and not text:
                logger.warning(
                    "[AutosendWorker] 跳过空正文 L2 草稿（不标记已发，待回填/人工补全）"
                    "draft_id=%s conv=%s",
                    draft_id, d.get("conversation_id", ""),
                )
                continue
            try:
                result = self._svc.resolve_with_audit(draft_id, "autosend", by="autosend_worker")
                if result.get("ok"):
                    sent += 1
                    if self._send_callback is not None and text:
                        to_deliver.append({
                            "draft_id": draft_id,
                            "conversation_id": d.get("conversation_id", ""),
                            "platform": str(d.get("platform") or ""),
                            "account_id": str(d.get("account_id") or "default"),
                            "chat_key": str(d.get("chat_key") or ""),
                            "text": text,
                            # 草稿创建时间作「已耗时」基准（自适应延迟扣除；≈入站到现在）
                            "created_ts": float(
                                d.get("created_at") or d.get("created_ts") or 0),
                            # 拟稿所回应的客户原话：延迟后二次过期复查须同文本免拦
                            # （同文本新入站不会催生新稿，拦了=客户零回复）
                            "peer_text": str(d.get("peer_text") or ""),
                            # Q-3：告别稿（停联「最多一条」）豁免真发前人工优先复检
                            "_farewell": bool(_is_farewell),
                        })
                elif int(result.get("code") or 0) == 409:
                    # 撞状态闸门＝该草稿刚被人工窗口处置（含人工通过后自带投递）。
                    # 属正常竞态而非故障：不计 error（防喂熔断器）、不投递（防双发）。
                    self.total_skipped_raced += 1
                    logger.debug(
                        "[AutosendWorker] draft_id=%s 已被他方处置，跳过（409）", draft_id)
                else:
                    errors += 1
                    logger.debug(
                        "[AutosendWorker] draft_id=%s resolve 返回 not-ok: %s",
                        draft_id, result.get("error"),
                    )
            except Exception as exc:
                errors += 1
                logger.warning(
                    "[AutosendWorker] draft_id=%s 发送异常: %s", draft_id, exc
                )
        return sent, errors, to_deliver

    def _adapt_interval(self, sent: int) -> None:
        """自适应间隔：有发送→缩短；无发送→指数扩张到 max_interval。"""
        if sent > 0:
            self._current_interval = self._min_interval
        else:
            self._current_interval = min(
                self._current_interval * 1.5, self._max_interval
            )

    # ── 运维动作 ──────────────────────────────────────────────

    def apply_send_callbacks(
        self,
        *,
        send_callback: Any = _UNSET,
        translate_callback: Any = _UNSET,
        mark_read_callback: Any = _UNSET,
        typing_callback: Any = _UNSET,
        persona_resolver: Any = _UNSET,
        dup_guard_cfg: Any = _UNSET,
        fresh_guard_cfg: Any = _UNSET,
        work_schedule_provider: Any = _UNSET,
        pilot_guard: Any = _UNSET,
    ) -> None:
        """运行时热接线投递能力（P1 2026-08-22「一键全自动」）。

        为什么需要：deliver 此前是**构造期冻结**——用户在能力看板/向导点开
        「全自动真发」只写了 overlay，worker 的 ``_send_callback`` 仍是 None，
        实际要等下次重启才生效（B37 实录「开了全自动还是不回复」的第三个成因）。
        本入口与 ``apply_deliver_delay`` 同一模式：路由在写 overlay 成功后调它，
        把 bootstrap 同款回调注入运行中的 worker，开关即时生效。

        仅覆盖**显式传入**的项（哨兵 ``_UNSET``）；``send_callback=None`` 是合法
        值＝撤掉自动链真发能力（deliver 关闭方向），人工链回调不在此处变更。
        """
        if send_callback is not _UNSET:
            self._send_callback = send_callback
            # 签名探测缓存按回调对象记，换了对象必须重探
            self._send_cb_accepts_original = None
        if translate_callback is not _UNSET:
            self._translate_callback = translate_callback
        if mark_read_callback is not _UNSET:
            self._mark_read_callback = mark_read_callback
        if typing_callback is not _UNSET:
            self._typing_callback = typing_callback
        if persona_resolver is not _UNSET:
            self._persona_resolver = persona_resolver
        if dup_guard_cfg is not _UNSET:
            self._dup_guard_cfg = dict(dup_guard_cfg or {})
        if fresh_guard_cfg is not _UNSET:
            self._fresh_guard_cfg = (
                dict(fresh_guard_cfg) if isinstance(fresh_guard_cfg, dict)
                else {"enabled": False})
        if work_schedule_provider is not _UNSET:
            self._ws_provider = work_schedule_provider
        if pilot_guard is not _UNSET:
            self._pilot_guard = pilot_guard

    def ensure_auto_loop(self) -> bool:
        """把「从未跑自动循环」的实例（deliver_only 兜底 / enabled=false 构造）
        升格为常规自动循环（P1 一键全自动热接线）。

        已在跑 → False（幂等）；无运行中事件循环 → False（调用方在异步路由内，
        正常不会发生）。二次启动由 run() 的 ``_RUNNING_SVC_KEYS`` 防线兜底。
        """
        if self._running:
            return False
        self._enabled = True
        self._deliver_only = False
        try:
            asyncio.ensure_future(self.run())
            return True
        except RuntimeError:
            logger.warning("[AutosendWorker] ensure_auto_loop：无运行中事件循环")
            return False

    def set_catchup_regenerate_cb(self, cb: Optional[Callable[..., bool]]) -> None:
        """注入复班补觉重拟回调（bootstrap 在 auto_draft 装配完成后调用）。

        worker 先于 auto_draft 子系统构造，构造期拿不到拟稿回调——后装配
        避免顺序耦合。未注入期间补觉分支整体跳过（陈稿按旧行为原样投递）。
        """
        self._catchup_regen_cb = cb if callable(cb) else None

    def reset_circuit(self) -> bool:
        """手动重置熔断器（H2 一键动作）。

        当熔断因连续错误开启、但根因已被人工排除时，主管可立即闭合熔断让 worker
        恢复，无需等冷却期。返回「调用前是否处于熔断态」（True=确实做了重置）。
        """
        was_open = self._circuit_open
        self._circuit_open = False
        self._circuit_open_ts = 0.0
        self._consecutive_errors = 0
        if was_open:
            logger.info("[AutosendWorker] 熔断器被手动重置（运维一键动作）")
        return was_open

    # ── 指标快照 ──────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "cycles": self.cycles,
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
            "last_sent": self.last_sent,
            "last_run_ts": self.last_run_ts,
            "last_error": self.last_error,
            "circuit_open": self._circuit_open,
            "consecutive_errors": self._consecutive_errors,
            "current_interval_sec": round(self._current_interval, 1),
            "event_triggers": self.event_triggers,  # C3：事件驱动唤醒次数
            "total_cleaned": self.total_cleaned,     # H3：历史清理草稿总数
            "total_sent_session": self.total_sent,   # E3 健康面板兼容字段
            "deliver_enabled": self._send_callback is not None,  # 自动链是否真正投递到平台
            # 本实例只作人工投递载体、自动循环从未 run()（l2_autosend.enabled=false 的
            # 人审档）。读者据此别把「worker 存在」当成「自动回复在跑」。
            "deliver_only": self._deliver_only,
            # 人工通过链是否具备真发能力（与 deliver_enabled 正交——见 human_send_callback）
            "human_deliver_enabled": (
                self._human_send_callback is not None
                or self._send_callback is not None),
            "total_delivered": self.total_delivered,
            "total_deliver_errors": self.total_deliver_errors,
            "translate_enabled": self._translate_callback is not None,  # 是否投递前出站翻译
            # gate-only=常规翻译关闭、仅语言硬闸在岗（P1-198）——不标出来的话
            # translate_enabled=true 会让运营误以为常规出站翻译开着。
            "translate_gate_only": bool(
                getattr(self._translate_callback, "gate_only", False)),
            "total_translated": self.total_translated,
            # 「能力在（回调装配了）且开关开」才算 enabled——P1 起开关可热更，
            # 只看回调在不在会在运营关闭后仍谎报 true。
            "mark_read_enabled": (
                self._mark_read_callback is not None and self._mark_read_enabled),
            "total_marked_read": self.total_marked_read,
            "typing_enabled": (
                self._typing_callback is not None and self._typing_enabled),
            "total_skipped_blocked": self.total_skipped_blocked,  # 因会话发送封禁跳过取消数
            "total_skipped_mode": self.total_skipped_mode,  # 因会话被降级(接管)取消的 L2 数
            # Q-3（#264）人工优先：真发前复检放弃 / 在途点名取消 / 风险持有取消 / 按 L1 重拟
            "total_abort_recheck": self.total_abort_recheck,
            "total_inflight_cancelled": self.total_inflight_cancelled,
            "total_skipped_risk_hold": self.total_skipped_risk_hold,
            "total_risk_hold_regen": self.total_risk_hold_regen,
            "inflight_now": len(self._inflight),
            # Q-18 B（#292）：让位 = 延后不是丢弃
            "total_yield_deferred": self.total_yield_deferred,
            "total_yield_resumed": self.total_yield_resumed,
            "total_yield_exhausted": self.total_yield_exhausted,
            "yield_now": len(self._yield_defer),
            # 驾驶权互斥锁（surface_fusion）：owner=native 让位取消的 L2 数
            "total_skipped_pilot": self.total_skipped_pilot,
            "pilot_guard_wired": self._pilot_guard is not None,
            "recoverable": self._recoverable,
            "retry_pending": len(self._retry_queue),
            "total_retry_scheduled": self.total_retry_scheduled,
            "total_retry_recovered": self.total_retry_recovered,
            "total_retry_exhausted": self.total_retry_exhausted,
            # 实施86 域B-1：按边车 retry_after_ms 改期重投的次数（#49 限频退避）
            "total_deferred": self.total_deferred,
            "total_skipped_raced": self.total_skipped_raced,  # resolve 撞闸门（他方已处置）
            # B41（2026-08-22）：幂等钉拒绝数 + 通道互斥让位数——恒 0 是常态，
            # 涨了说明真拦到了双投/双答（去日志看 guard=deliver_once/channel_mutex）
            "total_skipped_already_sent": self.total_skipped_already_sent,
            "total_skipped_mutex": self.total_skipped_mutex,
            # O-1 A：冻结会话（停联/自伤）取消的 L2 稿数——涨了＝硬停真拦到了
            "total_skipped_stop_contact": self.total_skipped_stop_contact,
            # M-2：账号级通道门禁扣住次数（冷静期/降级/退避/未连接）+ 降级发生次数
            "total_skipped_channel_gate": self.total_skipped_channel_gate,
            "total_account_degraded": self.total_account_degraded,
            "total_retry_gated": self.total_retry_gated,
            "deliver_once": self._deliver_once.stats_snapshot(),
            "total_human_delivered": self.total_human_delivered,  # 人工通过经 worker 投递成功
            "total_human_deliver_errors": self.total_human_deliver_errors,
            # Q-23（#303）stage=soft_reply：守卫触发的自动软回应经单一闸门投递 / 失败（含 gen=skip）/ 闸拦
            "total_soft_reply_delivered": self.total_soft_reply_delivered,
            "total_soft_reply_errors": self.total_soft_reply_errors,
            "total_soft_reply_aborted": self.total_soft_reply_aborted,
            "total_dup_blocked": self.total_dup_blocked,  # 出站近重复守卫拦截数
            "total_dup_rewritten": self.total_dup_rewritten,  # 拦截后换说法得救数
            # #144 拆计数：同轮双发拦截 / 跨轮拦截（仅 dup 档原样复读）/
            # 跨轮相近被 since 边界放行（修前会误拦）/ 拦下后打「需人工」数
            "total_dup_blocked_same_round": self.total_dup_blocked_same_round,
            "total_dup_blocked_cross_round": self.total_dup_blocked_cross_round,
            "total_dup_cross_round_released": self.total_dup_cross_round_released,
            "total_dup_blocked_flagged": self.total_dup_blocked_flagged,
            "dup_guard_enabled": bool(self._dup_guard_cfg.get("enabled")),
            "total_superseded": self.total_superseded,  # 新入站过期守卫跳过数
            "fresh_guard_enabled": bool(self._fresh_guard_cfg.get("enabled")),
            # P1 连发地板（2026-08-12）：min_gap_sec 抬升过延迟的次数——
            # 「第 2/3 条秒回」修复是否真在生效，零流量即可判（恒 0 + 配置>0
            # ＝没有连发场景或链没接上）。
            "total_gap_floored": self.total_gap_floored,
            # 并行投递（2026-08-09）：开关/并发上限/走过并行分发的批次数——
            # 「配置开了没生效」零流量即可判（enabled=false 而运营以为开了）。
            "parallel_deliver": {
                "enabled": self._parallel_enabled,
                "max_concurrent": self._parallel_max,
                "batches": self.parallel_batches,
            },
            "blocked_conversations": sum(
                1 for c in list(self._blocked_conv_until) if self._conv_send_blocked(c)
            ),  # 当前处于发送封禁冷却的会话数
            # 工作时间班表（enabled=配置总闸而非「已接线」——provider 未注入时
            # 恒 False；扣留计数是每 tick 重扫的活动事件数，非唯一草稿数）
            "work_schedule_enabled": self._work_schedule_enabled(),
            "total_skipped_off_hours": self.total_skipped_off_hours,
            "total_first_reply_held": self.total_first_reply_held,
            "total_catchup_regenerated": self.total_catchup_regenerated,
            "catchup_regen_wired": self._catchup_regen_cb is not None,
        }

    def _work_schedule_enabled(self) -> bool:
        if self._ws_provider is None:
            return False
        try:
            return bool((self._ws_provider() or {}).get("enabled"))
        except Exception:
            return False
