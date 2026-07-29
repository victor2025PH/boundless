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

    def _pick_deliver_delay(
        self, text: str = "", elapsed_sec: float = 0.0, persona_id: str = "",
    ) -> float:
        """按 deliver_delay 配置取本次拟人延迟（秒）。统一走 resolve_pacing：
        先按 ``persona_id`` 合并 persona_overrides；adaptive=false→uniform(min,max)（旧行为）；
        adaptive=true→按回复内容长度/激活度估时并扣除 ``elapsed_sec`` 已耗时。未配置/非法 → 0。
        顺带按人设分维记录节奏观测（best-effort）。"""
        from src.inbox.humanize import resolve_pacing
        r = resolve_pacing(
            self._deliver_delay_block, text=text, elapsed_sec=elapsed_sec,
            persona_id=persona_id)
        try:
            from src.integrations.humanize_metrics import record_pacing
            record_pacing(f"autosend/{persona_id or '-'}", r)
        except Exception:
            pass
        return r.delay

    async def _run_humanize(
        self, platform: str, account_id: str, chat_key: str,
        *, text: str = "", elapsed_sec: float = 0.0,
    ) -> None:
        """投递前拟人序列：已读 → 打字续挂 → 拟人延迟（委托 humanize 协作器）。

        已读/打字回调缺省时各自跳过（无回调=旧行为）；延迟取 deliver_delay 配置
        （adaptive 时按 ``text`` 长度自适应、扣 ``elapsed_sec`` 已耗时）。mark_read 成功
        累计 total_marked_read（与旧口径一致：调用不抛即计数）。
        """
        from src.inbox.humanize import run_presend_humanization

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
        if self._mark_read_callback is not None:
            async def _mr():
                await self._mark_read_callback(platform, account_id, chat_key)
        _tp = None
        if self._typing_callback is not None:
            async def _tp(action):
                await self._typing_callback(platform, account_id, chat_key, action)

        def _inc_marked():
            self.total_marked_read += 1

        await run_presend_humanization(
            delay=self._pick_deliver_delay(text, elapsed_sec, persona_id=_pid),
            action="typing",
            mark_read=_mr,
            typing=_tp,
            sleep=self._sleep,
            refresh_sec=_TYPING_REFRESH_SEC,
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
                try:
                    _tx = await self._translate_callback(item)
                    if _tx:
                        if _tx != send_text:
                            self.total_translated += 1
                        send_text = _tx
                except Exception:
                    logger.warning(
                        "[AutosendWorker] 人工通过出站翻译异常，发原文 conv=%s",
                        item["conversation_id"], exc_info=True)
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
            for item in deliver_now:
                try:
                    # 出站翻译：投递前把 AI 中文回复译成客户语言（补「全自动聊天翻译」闭环）。
                    # 绝不阻塞投递——回调内部已保证异常/不可译时回落原文。
                    send_text = str(item.get("text", ""))
                    if self._translate_callback is not None:
                        try:
                            _tx = await self._translate_callback(item)
                            if _tx:
                                if _tx != send_text:
                                    self.total_translated += 1
                                send_text = _tx
                        except Exception:
                            logger.warning(
                                "[AutosendWorker] 出站翻译异常，发原文 conv=%s",
                                item.get("conversation_id", "?"), exc_info=True)
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
                        continue  # 重试中：本条暂不记 autosend_failed
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
            "total_translated": self.total_translated,
            "mark_read_enabled": self._mark_read_callback is not None,  # 是否投递前补平台已读
            "total_marked_read": self.total_marked_read,
            "typing_enabled": self._typing_callback is not None,  # 是否投递延迟期挂打字状态
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
            "blocked_conversations": sum(
                1 for c in list(self._blocked_conv_until) if self._conv_send_blocked(c)
            ),  # 当前处于发送封禁冷却的会话数
        }
