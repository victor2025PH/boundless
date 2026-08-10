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
import time
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
    ) -> None:
        cfg = config or {}
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
        self.total_superseded: int = 0           # 新入站过期守卫跳过数（fresh_guard，不算 error）
        # 工作时间闸扣留事件数（同一草稿每 tick 重扫会重复计数——这是「扣留中」的
        # 活动信号而非唯一草稿数；复班后自然归零增长）
        self.total_skipped_off_hours: int = 0
        self.total_catchup_regenerated: int = 0  # 复班补觉：作废陈稿并重拟的条数

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

    def _dup_guard_check(self, item: Dict[str, Any],
                         send_text: str) -> Optional[Dict[str, Any]]:
        """出站近重复判定：DB 出站镜像 + 进程级在途登记表合并比对。

        防御式：store 缺失/查询异常按「不命中」（守卫绝不阻断正常投递）。
        """
        conv = str(item.get("conversation_id") or "")
        if not conv:
            return None
        rows: List[Dict[str, Any]] = []
        store = getattr(self._svc, "_store", None)
        if store is not None:
            try:
                rows = list(store.list_recent_messages(conv, limit=8) or [])
            except Exception:
                rows = []
        try:
            from src.inbox.outbound_dup_guard import (
                near_duplicate_of_recent,
                outbound_registry,
            )
            rows.extend(outbound_registry.recent_rows(conv))
            return near_duplicate_of_recent(
                send_text, rows,
                window_sec=float(self._dup_guard_cfg.get("window_sec", 180.0)),
            )
        except Exception:
            logger.debug("[AutosendWorker] 近重复守卫异常（放行）", exc_info=True)
            return None

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

    def _pick_deliver_delay(
        self, text: str = "", elapsed_sec: float = 0.0, persona_id: str = "",
        platform: str = "",
    ) -> float:
        """按 deliver_delay 配置取本次拟人延迟（秒）。统一走 resolve_pacing：
        先合并覆写层（人设 > 平台 > 全局，见 humanize._apply_scoped_overrides）；
        adaptive=false→uniform(min,max)（旧行为）；adaptive=true→按回复内容长度/
        激活度估时并扣除 ``elapsed_sec`` 已耗时。未配置/非法 → 0。
        顺带按 平台/人设 分维记录节奏观测（best-effort；路径
        ``autosend/{platform|-}/{persona|-}``，旧单段格式的前端解析已兼容两代）。"""
        from src.inbox.humanize import resolve_pacing
        r = resolve_pacing(
            self._deliver_delay_block, text=text, elapsed_sec=elapsed_sec,
            persona_id=persona_id, platform=platform)
        try:
            from src.integrations.humanize_metrics import record_pacing
            record_pacing(
                f"autosend/{platform or '-'}/{persona_id or '-'}", r)
        except Exception:
            pass
        return r.delay

    async def _run_humanize(
        self, platform: str, account_id: str, chat_key: str,
        *, text: str = "", elapsed_sec: float = 0.0,
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
        _tp = None
        if (self._typing_callback is not None
                and self._humanize_flag(platform, "typing")):
            async def _tp(action):
                await self._typing_callback(platform, account_id, chat_key, action)

        def _inc_marked():
            self.total_marked_read += 1

        await run_presend_humanization(
            delay=self._pick_deliver_delay(
                text, elapsed_sec, persona_id=_pid, platform=platform),
            action="typing",
            mark_read=_mr,
            typing=_tp,
            sleep=self._sleep,
            refresh_sec=_TYPING_REFRESH_SEC,
            typing_lead_sec=resolve_typing_lead(
                self._deliver_delay_block, text=text,
                persona_id=_pid, platform=platform),
            on_marked=_inc_marked,
        )

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
        }
        send_cb = self._human_send_callback or self._send_callback
        if send_cb is None or not item["text"] or not item["chat_key"]:
            return {"ok": False, "error": "no_send_path_or_empty"}
        try:
            send_text = item["text"]
            if self._translate_callback is not None:
                _tx = send_text
                try:
                    _tx = await self._translate_callback(item)
                except Exception:
                    logger.warning(
                        "[AutosendWorker] 人工通过出站翻译异常，发原文 conv=%s",
                        item["conversation_id"], exc_info=True)
                # None = 翻译回调的 HOLD 信号（文本含 CJK 而客户语言非 CJK 且翻译
                # 不可用，见 outbound_translate.translate_outbound_text）——发中文
                # 给外语客户=人设穿帮，走投递失败链（审计+坐席铃铛），别发原文。
                if _tx is None:
                    raise RuntimeError(
                        "translate_hold: 出站翻译不可用且文本语言与客户语言冲突，已拦截")
                if _tx:
                    if _tx != send_text:
                        self.total_translated += 1
                    send_text = _tx
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
            self.total_human_delivered += 1
            logger.info(
                "[AutosendWorker] 人工通过草稿已投递 draft=%s conv=%s",
                item["draft_id"], item["conversation_id"])
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            self.total_human_deliver_errors += 1
            self.last_error = f"human_deliver: {exc}"
            logger.warning(
                "[AutosendWorker] 人工通过草稿投递失败 draft=%s conv=%s: %s",
                item["draft_id"], item["conversation_id"], exc)
            try:
                rec = getattr(self._svc, "record_autosend_failure", None)
                if rec is not None:
                    rec(
                        item["draft_id"],
                        conversation_id=item["conversation_id"],
                        reason=f"人工通过投递失败: {exc}",
                    )
            except Exception:
                logger.debug("human_deliver 失败审计写入失败", exc_info=True)
            # 复用坐席铃铛/webhook 的投递失败提醒（工作台已订阅该事件）
            self._publish_deliver_failed(
                item, str(exc), permanent=_is_permanent_send_error(str(exc)))
            return {"ok": False, "error": str(exc)}

    async def run(self) -> None:
        if not self._enabled:
            logger.info("[AutosendWorker] L2 自动发送已禁用（config.enabled=false）")
            return
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
        # Sprint2：把到期重试项并入本轮投递（重发同文本，不 re-resolve）。recoverable 关时
        # _retry_queue 恒空 → deliver_now == to_deliver，行为与旧实现字节级一致。
        deliver_now = list(to_deliver)
        if self._recoverable and self._retry_queue:
            _rt_now = time.time()
            _due = [r for r in self._retry_queue if r.get("next_ts", 0) <= _rt_now]
            for r in _due:
                try:
                    self._retry_queue.remove(r)
                except ValueError:
                    continue
                deliver_now.append(r["item"])
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

        2026-08-09 从 _tick 的串行 for 循环整体抽出（并行化前置）。并发语义边界：
        **同会话必须串行**（调度层 _deliver_parallel 按会话分组保证——顺序与
        防双发不变量都建立在会话内有序上）；跨会话可并发——本方法更新的共享
        状态（计数器/重试队列/封禁表/dup 登记）全部在事件循环单线程语义下写入，
        无跨线程共享。原 for 循环体的 continue 在此为 return（单条早退）。
        """
        _dup_token = 0  # 出站登记 token（失败撤销用），须在 try 外初始化
        try:
            # 出站翻译：投递前把 AI 中文回复译成客户语言（补「全自动聊天翻译」闭环）。
            # 一般不阻塞投递（回调内部异常/不可译回落原文）；唯一例外＝回调返回
            # None（HOLD：文本含 CJK 而客户语言非 CJK 且翻译不可用）→ 按投递失败
            # 处理（进重试队列，翻译引擎恢复后自动补投）——发中文给外语客户是
            # 人设事故，比这条消息迟到更糟（2026-07-31 198 实锤）。
            send_text = str(item.get("text", ""))
            if self._translate_callback is not None:
                _tx = send_text
                try:
                    _tx = await self._translate_callback(item)
                except Exception:
                    logger.warning(
                        "[AutosendWorker] 出站翻译异常，发原文 conv=%s",
                        item.get("conversation_id", "?"), exc_info=True)
                if _tx is None:
                    raise RuntimeError(
                        "translate_hold: 出站翻译不可用且文本语言与客户语言冲突，已拦截")
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
                _hit = self._dup_guard_check(item, send_text)
                _lvl = (_hit or {}).get("level", "")
                _block = bool(_hit) and (
                    _lvl == "dup"
                    or bool(self._dup_guard_cfg.get("block_similar", True)))
                try:
                    _dup_rec(_lvl, source="autosend", blocked=_block)
                except Exception:
                    pass
                if _block:
                    self.total_dup_blocked += 1
                    logger.warning(
                        "[AutosendWorker] guard=near_duplicate 出站近重复"
                        "拦截 conv=%s level=%s sim=%.2f age=%.0fs matched=%r",
                        _conv_id_g, _lvl, _hit.get("similarity", 0.0),
                        _hit.get("age_sec", 0.0),
                        str(_hit.get("matched_text", ""))[:60])
                    return
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
            await self._run_humanize(
                _plat, _acc, _ck, text=send_text, elapsed_sec=_elapsed)
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
                                pass
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
            _send_kw: Dict[str, Any] = self._send_cb_kwargs(
                str(item.get("text", "")))
            res = await self._send_callback(
                item.get("platform", ""), item.get("account_id", "default"),
                item.get("chat_key", ""), send_text, **_send_kw,
            )
            # 投递失败判定：除显式 ok=False 外，编排器/出站闸门返回
            # {delivered: False} 或 {blocked: ...}（如 kill-switch/send-gate 拦截、
            # 桌面出站被闸门拒）也算未送达——否则会把「被拦截」误计为已送达刷指标。
            if isinstance(res, dict) and (
                res.get("ok") is False
                or res.get("delivered") is False
                or res.get("blocked")
            ):
                raise RuntimeError(str(
                    res.get("error") or res.get("blocked") or "send not ok"))
            self.total_delivered += 1
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
                    pass
            _plat = item.get("platform", "?")
            _permanent = _is_permanent_send_error(str(exc))
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
            # Sprint2：开启 recoverable 时，永久/耗尽失败发实时提醒事件，坐席可补发。
            if self._recoverable:
                self._publish_deliver_failed(item, str(exc), permanent=_permanent)


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
        l2 = [d for d in drafts if d.get("autopilot_level") == "L2"]
        sent, errors = 0, 0
        to_deliver: List[Dict[str, Any]] = []
        # 复班补觉每批重拟预算：防复班瞬间对整夜积压一次性打满 LLM；超预算的
        # 留 pending，下一 tick 继续（min_interval 节拍天然把补觉摊开）。
        catchup_budget = 5
        for d in l2:
            draft_id = d.get("draft_id", "")
            _conv = str(d.get("conversation_id") or "")
            # Sprint1 统一出站闸门：会话被**显式**降级（坐席接管→manual / 改 review 等）后，
            # 接管前已入队的 L2 不该再自动发（防「接管前排队的草稿仍被投递」竞态）。
            # 仅对显式设过档位者生效；未显式设置(None)不干预 → 不改既有默认行为/perf 测试。
            _store_rt = getattr(self._svc, "_store", None)
            if _store_rt is not None and _conv and hasattr(_store_rt, "get_automation_mode_if_set"):
                try:
                    _explicit_mode = _store_rt.get_automation_mode_if_set(_conv)
                except Exception:
                    _explicit_mode = None
                # 仅对「真实字符串且属显式降级档位」生效——auto_ai/未设(None)/mock 对象均不跳过，
                # 避免误伤（尤其单测用 MagicMock store 时 getattr 返回 Mock）。
                if isinstance(_explicit_mode, str) and _explicit_mode in (
                        "manual", "review", "multi_choice"):
                    try:
                        if hasattr(_store_rt, "update_draft_status"):
                            _store_rt.update_draft_status(
                                draft_id, status="cancelled", decided_by="mode_downgraded")
                    except Exception:
                        logger.debug(
                            "[AutosendWorker] 取消降级会话 L2 失败 draft_id=%s", draft_id,
                            exc_info=True)
                    self.total_skipped_mode += 1
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
                    self.total_skipped_off_hours += 1
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
                    from src.inbox.work_hours_gate import off_hours_cfg
                    _ws_cfg = self._ws_provider() or {}
                    _oh = off_hours_cfg(_ws_cfg)
                    _regen_h = float(_oh.get("catch_up_regenerate_hours") or 0)
                    _peer_txt = str(d.get("peer_text") or "")
                    _draft_ts = float(
                        d.get("created_ts") or d.get("created_at") or 0)
                    if (_ws_cfg.get("enabled") and _oh.get("catch_up")
                            and _regen_h > 0 and _draft_ts > 0 and _peer_txt
                            and time.time() - _draft_ts > _regen_h * 3600.0):
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
            "recoverable": self._recoverable,
            "retry_pending": len(self._retry_queue),
            "total_retry_scheduled": self.total_retry_scheduled,
            "total_retry_recovered": self.total_retry_recovered,
            "total_retry_exhausted": self.total_retry_exhausted,
            "total_skipped_raced": self.total_skipped_raced,  # resolve 撞闸门（他方已处置）
            "total_human_delivered": self.total_human_delivered,  # 人工通过经 worker 投递成功
            "total_human_deliver_errors": self.total_human_deliver_errors,
            "total_dup_blocked": self.total_dup_blocked,  # 出站近重复守卫拦截数
            "dup_guard_enabled": bool(self._dup_guard_cfg.get("enabled")),
            "total_superseded": self.total_superseded,  # 新入站过期守卫跳过数
            "fresh_guard_enabled": bool(self._fresh_guard_cfg.get("enabled")),
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
