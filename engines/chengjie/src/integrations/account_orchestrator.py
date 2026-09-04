"""账号池编排器（M5）。

把「多账号 7×24 在线」真正跑起来的最后一块：进程启动时读 ``account_registry``，按每个账号
的 ``(platform, mode)`` 用对应 **worker** 拉起，并持续**健康监督**（失败指数退避重启），
绑定的 ``proxy`` / ``fingerprint`` 自动注入底层连接。

设计要点（多轮打磨后的取舍）：
- **worker 注册表**与登录 provider 同构（``register_worker(platform, mode, factory)``），
  protocol 等可挂载真实 worker，device/web 不在此编排（device 由既有 RPA runner 管，
  web 待 M6）。
- **监督与时钟解耦**：``tick()`` 是一次幂等监督步，``_now`` / ``_sleep`` 可注入 → 单测用
  假 worker + 假时钟**确定性**驱动启动/重启/退避/下线，无需真账号、无需长 sleep。
- **零副作用默认**：所有真实 worker 受各自 feature flag + ``orchestrator_enabled`` 门控，
  默认全关；关闭时编排器不持有任何连接，主进程行为不变。
- **WhatsApp 不双重监督**：Baileys（Node）自身在服务内重连保活，故 WA worker 是「确保 Node
  已恢复 + 读其状态」的薄监督，避免 Python/Node 两侧重复拉连接。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.shared.send_guard import send_blocked

logger = logging.getLogger(__name__)


def _record_line_identity(outcome: str) -> None:
    """记 LINE 私聊发送者显示名解析结果到 peer_identity 观测（best-effort，绝不影响主流程）。"""
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record("line", outcome)
    except Exception:
        pass


# 仅这些 mode 由编排器接管（device 归既有 RPA runner）
# official = 官方 API 出站 worker（LINE/Messenger/WhatsApp Cloud，无状态 HTTP，G 延伸）
# web = 网页自动化 worker（Messenger 网页模式经隔离浏览器/Playwright Node 微服务；M6 落地）——
#   连接由 Node 微服务保活，Python 侧薄监督 + 路由出站（见 MessengerWebWorker）。
# 注：worker_supported 还要求对应 (platform, mode) 工厂已注册，故仅登记了工厂的 web 平台
#   （messenger）会被接管；未注册工厂的 web 账号（如遗留 telegram web 占位）自然不被拾取。
ORCHESTRATED_MODES = ("protocol", "official", "web")

# 监督参数
DEFAULT_INTERVAL = 15.0      # 监督步间隔（秒）
BACKOFF_BASE = 2.0           # 退避基数（秒）
BACKOFF_MAX = 120.0          # 退避上限（秒）
MAX_RESTARTS = 8             # 连续失败上限 → 熔断（标 error，停止重试直到人工/sync 重置）
# LINE 接收轮回的日志抽样间隔：网关 ~10s 一轮，逐轮打日志＝每天上万行，
# 故只每 N 轮打一行（约 1h），日常读数走 status 的 recv_cycles。
_LINE_RECV_CYCLE_LOG_EVERY = 360

# ── #180 LINE receiver 连败放弃的**跨重启**退避（2026-09-05）───────────────────
# 3PZ95W（钧机）19:55–20:02：receiver 对 line-chrome-gw /api/operation/receive 连败
# 6 次（≈2 分钟）→ 放弃 → 编排器重启 worker → 新 receiver 立刻又连败 6 次 → …
# 多轮循环。每轮重启期 worker 不在 running，工作台媒体/语音按钮全灰、发语音 501。
# 监督器（line_recv_supervisor）只管**一条接收线程内**的退避；跨重启的节奏由这里
# 按账号记账：连续放弃 n 次 → 下次 start() 后 receiver **延后** hold(n) 秒再拉
# （60s→2m→4m→…封顶 30m），延后期 worker 仍 running（client 在、拉取兜底在，能发
# 能收），状态标 ``reconnecting`` 而非静默。receiver 稳跑 ≥ 10 分钟即清零。
_LINE_RECV_HOLD_BASE_SEC = 60.0
_LINE_RECV_HOLD_CAP_SEC = 1800.0
_LINE_RECV_STABLE_RESET_SEC = 600.0
#: account_id → {"streak": 连续放弃次数, "ts": 上次放弃时刻, "reason": 最后一次异常摘要}
_LINE_RECV_GIVEUP: Dict[str, Dict[str, Any]] = {}


def line_recv_hold_sec(streak: int) -> float:
    """连续放弃 ``streak`` 次后，下一次 receiver 拉起前该等多久（纯函数）。"""
    try:
        n = int(streak)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    return float(min(_LINE_RECV_HOLD_CAP_SEC, _LINE_RECV_HOLD_BASE_SEC * (2 ** (n - 1))))

# send_media 超时兜底默认值（秒）——2026-09-02 WEXX7E 实锤：LINE worker 的签名桥
# Node 进程僵死后，媒体发送 await 永久无果、UI 只能靠前端 fetch 超时猜「网络异常」。
# 45s 的依据：出站媒体上限 20MB，okline 单请求 HTTP 超时 30s，正常最慢一笔
# （占位→OBS 上传→配文）也该在 40s 内出结果；再慢就是悬死，明确报失败比无限等强。
# 可经 config.orchestrator.send_media_timeout_sec 调整，<=0 = 关闭兜底（旧行为）。
DEFAULT_SEND_MEDIA_TIMEOUT_SEC = 45.0


def _send_media_timeout_sec(config: Optional[Dict[str, Any]]) -> float:
    try:
        v = ((config or {}).get("orchestrator") or {}).get("send_media_timeout_sec")
        return float(v) if v is not None else DEFAULT_SEND_MEDIA_TIMEOUT_SEC
    except (TypeError, ValueError):
        return DEFAULT_SEND_MEDIA_TIMEOUT_SEC


def account_key(platform: str, account_id: str) -> str:
    return f"{str(platform).lower()}:{account_id}"


# ── worker 注册表 ────────────────────────────────────────────────────────────
# factory(account: dict, config: dict) -> Worker
#   Worker 需实现 async start()/stop()、async healthy()->bool、status()->dict
_WORKER_FACTORIES: Dict[str, Callable[..., Any]] = {}


def register_worker(platform: str, mode: str, factory: Callable[..., Any]) -> None:
    _WORKER_FACTORIES[f"{str(platform).lower()}:{str(mode).lower()}"] = factory


def get_worker_factory(platform: str, mode: str) -> Optional[Callable[..., Any]]:
    return _WORKER_FACTORIES.get(f"{str(platform).lower()}:{str(mode).lower()}")


def worker_supported(platform: str, mode: str) -> bool:
    return (mode in ORCHESTRATED_MODES) and get_worker_factory(platform, mode) is not None


# ── 被管理账号 ───────────────────────────────────────────────────────────────

@dataclass
class _Managed:
    key: str
    platform: str
    account_id: str
    mode: str
    worker: Any = None
    state: str = "stopped"      # stopped|starting|running|error|stopping
    restarts: int = 0
    last_error: str = ""
    backoff_until: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        ws = {}
        if self.worker is not None and hasattr(self.worker, "status"):
            try:
                ws = self.worker.status() or {}
            except Exception:
                ws = {}
        return {
            "key": self.key, "platform": self.platform,
            "account_id": self.account_id, "mode": self.mode,
            "state": self.state, "restarts": self.restarts,
            "last_error": self.last_error, "worker": ws,
            "updated_at": self.updated_at,
        }


class AccountOrchestrator:
    def __init__(
        self,
        *,
        registry: Any = None,
        config: Optional[Dict[str, Any]] = None,
        interval: float = DEFAULT_INTERVAL,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry if registry is not None else get_account_registry()
        self._config = config or {}
        self._interval = interval
        self._now = now
        self._sleep = sleep
        self._managed: Dict[str, _Managed] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()   # 串行化监督循环与手动 API，避免并发 start/stop 竞态

    # ── 期望状态 ─────────────────────────────────────────────────────────

    def desired_accounts(self) -> List[Dict[str, Any]]:
        """仅 ``online`` 账号进期望集；``offline/pending/removed`` 不拉起（防无绑定号串话）。"""
        out = []
        for a in self._registry.list():
            if a.get("status") != "online":
                continue
            if worker_supported(a.get("platform", ""), a.get("mode", "")):
                out.append(a)
        return out

    # ── 单账号生命周期（公开方法加锁；内部 _* 无锁，仅供已持锁的监督步调用） ──

    async def start_account(self, account: Dict[str, Any]) -> bool:
        async with self._lock:
            return await self._start_account(account)

    async def stop_account(self, key: str) -> None:
        async with self._lock:
            await self._stop_account(key)

    async def restart_account(self, key: str) -> bool:
        async with self._lock:
            await self._stop_account(key)
            m = self._managed.get(key)
            if m is None:
                return False
            m.restarts = 0
            m.backoff_until = 0.0
            return await self._start_account({
                "platform": m.platform, "account_id": m.account_id, "mode": m.mode,
                **(self._registry.get(m.platform, m.account_id) or {}),
            })

    async def _start_account(self, account: Dict[str, Any]) -> bool:
        platform = str(account.get("platform") or "")
        account_id = str(account.get("account_id") or "")
        mode = str(account.get("mode") or "")
        key = account_key(platform, account_id)
        m = self._managed.get(key)
        if m is None:
            m = _Managed(key=key, platform=platform, account_id=account_id, mode=mode)
            self._managed[key] = m
        if m.state in ("running", "starting"):
            return True
        factory = get_worker_factory(platform, mode)
        if factory is None:
            # 自愈重试（P1-198）：工厂注册条件可能在启动之后才满足（托管凭据
            # 由网关守护线程晚注入 / 运营热开 protocol_enabled）。
            # ensure_builtin_workers 幂等且便宜——判死刑前再给一次机会，
            # 「凭据到位后第一次拉号」即自我修复，不必等重启。
            try:
                ensure_builtin_workers(self._config)
                factory = get_worker_factory(platform, mode)
            except Exception:
                factory = None
        if factory is None:
            m.state = "error"
            m.last_error = "no worker factory"
            logger.warning(
                "[orchestrator] 无可用 worker 工厂 %s（协议开关/凭据/依赖未就绪，"
                "该账号本轮不拉起）", key)
            return False
        m.state = "starting"
        m.updated_at = self._now_wall()
        try:
            # 上线无人设 → 写入默认人设（防宏图棋牌类空绑定串话/错声）
            try:
                from src.ai.persona_voice import ensure_account_default_persona
                ensure_account_default_persona(
                    self._registry, platform, account_id, self._config,
                )
                account = self._registry.get(platform, account_id) or account
            except Exception:
                pass
            if m.worker is None:
                m.worker = factory(account, self._config)
            await m.worker.start()
            m.state = "running"
            m.last_error = ""
            m.restarts = 0
            m.backoff_until = 0.0
            # telegram 协议/伴聊 worker 的 start 成功＝pyrogram 真连上（授权有效），
            # 回报健康表：若此前被标 logged_out（手机端退出），重扫后编排器拉起
            # 即自动转「已恢复」（清坐席离线提示 + 发恢复通知）。其他平台的
            # worker start 成功不代表登录态（如 messenger 边车），不在此上报。
            if platform == "telegram":
                self._report_session_health(
                    platform, account_id, "authorized",
                    detail="orchestrator start ok")
            return True
        except Exception as ex:  # noqa: BLE001
            m.state = "error"
            m.last_error = str(ex)
            m.restarts += 1
            self._schedule_backoff(m)
            logger.debug("[orchestrator] 启动账号失败 %s", key, exc_info=True)
            # 会话已死（SESSION_REVOKED/AUTH_KEY_UNREGISTERED…＝手机端退出/被吊销）
            # 是确定性故障：重试救不回来，必须人重新扫码。上报健康表+注册表落
            # offline+告警，坐席账号抽屉据此显示「已退出，点重连重新扫码」——
            # 修「手机上退了号、系统这边毫无提示」的静默盲区。
            if platform == "telegram":
                try:
                    from src.integrations.protocol_bridge import tg_error_kind
                    if tg_error_kind(str(ex)) == "session_revoked":
                        self._report_session_health(
                            platform, account_id, "logged_out",
                            detail=str(ex)[:200])
                except Exception:  # noqa: BLE001
                    logger.debug("[orchestrator] 会话死因分类失败 %s", key,
                                 exc_info=True)
            return False

    def _report_session_health(self, platform: str, account_id: str,
                               status: str, *, detail: str = "") -> None:
        """把账号登录态转移上报健康表（best-effort，绝不影响拉起主流程）。"""
        try:
            from src.integrations.platform_session_health import (
                report_session_transition,
            )
            report_session_transition(platform, account_id, status,
                                      detail=detail)
        except Exception:  # noqa: BLE001
            logger.debug("[orchestrator] 会话健康上报失败 %s:%s",
                         platform, account_id, exc_info=True)

    async def _stop_account(self, key: str) -> None:
        m = self._managed.get(key)
        if m is None:
            return
        m.state = "stopping"
        try:
            if m.worker is not None and hasattr(m.worker, "stop"):
                await m.worker.stop()
        except Exception:
            logger.debug("[orchestrator] 停止账号失败 %s", key, exc_info=True)
        m.state = "stopped"
        m.worker = None
        m.updated_at = self._now_wall()

    # ── 监督 ─────────────────────────────────────────────────────────────

    async def sync(self) -> None:
        """对齐：拉起期望但未在管的；下线已移除/不再期望的。"""
        async with self._lock:
            desired = {account_key(a["platform"], a["account_id"]): a
                       for a in self.desired_accounts()}
            for key, acc in desired.items():
                m = self._managed.get(key)
                if m is None or m.state == "stopped":
                    await self._start_account(acc)
            for key in list(self._managed.keys()):
                if key not in desired and self._managed[key].state != "stopped":
                    await self._stop_account(key)

    async def tick(self) -> None:
        """一次监督步：健康检查 + 退避重启（幂等，可被测试直接驱动）。"""
        async with self._lock:
            now = self._now()
            for key, m in list(self._managed.items()):
                if m.state == "running":
                    healthy = await self._safe_healthy(m)
                    if not healthy:
                        m.state = "error"
                        m.last_error = m.last_error or "unhealthy"
                        m.restarts += 1
                        self._schedule_backoff(m)
                elif m.state == "error":
                    if m.restarts >= MAX_RESTARTS:
                        continue  # 熔断，等待人工/sync 重置
                    if now >= m.backoff_until:
                        acc = self._registry.get(m.platform, m.account_id) or {
                            "platform": m.platform, "account_id": m.account_id,
                            "mode": m.mode}
                        await self._start_account(acc)

    async def _safe_healthy(self, m: _Managed) -> bool:
        try:
            if m.worker is not None and hasattr(m.worker, "healthy"):
                return bool(await m.worker.healthy())
        except Exception:
            logger.debug("[orchestrator] healthy 检查异常 %s", m.key, exc_info=True)
            return False
        return True

    def _schedule_backoff(self, m: _Managed) -> None:
        delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** max(0, m.restarts - 1)))
        delay *= 0.8 + 0.4 * random.random()  # ±20% 抖动，避免雪崩
        m.backoff_until = self._now() + delay
        m.updated_at = self._now_wall()

    def _now_wall(self) -> float:
        return time.time()

    # ── 后台循环 ─────────────────────────────────────────────────────────

    async def start_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[orchestrator] 监督循环已启动 (interval=%ss)", self._interval)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.sync()
                await self.tick()
            except Exception:
                logger.debug("[orchestrator] 监督步异常", exc_info=True)
            await self._sleep(self._interval)

    async def stop_loop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for key in list(self._managed.keys()):
            await self.stop_account(key)

    # ── 收发桥接（M6①：protocol 账号接入统一收件箱） ─────────────────────

    def owns(self, platform: str, account_id: str) -> bool:
        """该 (platform, account_id) 是否有正在运行、且可发送的受管 worker。"""
        m = self._managed.get(account_key(platform, account_id))
        return bool(
            m is not None and m.state == "running"
            and m.worker is not None and hasattr(m.worker, "send")
        )

    def worker_for(self, platform: str, account_id: str) -> Any:
        """返回该账号**正在运行**的受管 worker（供取 pyrogram client 做头像/身份解析）。

        多账号头像/补名的取数入口：非主账号的 pyrogram client 藏在其受管 worker 里
        （companion A 线 worker.client.client / protocol B 线 worker.client）。无运行中
        worker → None（调用方回落进程主 client）。
        """
        m = self._managed.get(account_key(platform, account_id))
        if m is not None and m.state == "running" and m.worker is not None:
            return m.worker
        return None

    def owns_media(self, platform: str, account_id: str) -> bool:
        """该账号是否有运行中、且支持发送媒体的 worker。"""
        m = self._managed.get(account_key(platform, account_id))
        return bool(
            m is not None and m.state == "running"
            and m.worker is not None and hasattr(m.worker, "send_media")
        )

    def media_capability(self, platform: str, account_id: str) -> Dict[str, Any]:
        """``owns_media`` 的分因版（#180）：为什么不能从工作台发媒体/语音。

        ``reason``：``""``（可发）/ ``no_worker``（账号未托管或该平台无 worker 工厂）/
        ``no_send_media``（worker 在但该平台/开关不支持发媒体，如 LINE 未开
        ``platform_login.line.media.outbound``）/ ``worker_not_running``（worker 在
        error/starting/stopping 等重连期——钧机 3PZ95W 00:48 那种：同账号两小时前
        还 delivered=True，此刻只是 receiver 连败正在被编排器重启）。前端据此把 501
        文案与媒体按钮灰态 tooltip 分成「该平台不支持」vs「通道正在重连，稍后再试」，
        后者带 ``state``/``backoff_sec`` 让坐席知道等多久。
        """
        m = self._managed.get(account_key(platform, account_id))
        if m is None:
            return {"owns": False, "reason": "no_worker", "state": "",
                    "restarts": 0, "backoff_sec": 0}
        has_send = m.worker is not None and hasattr(m.worker, "send_media")
        backoff = max(0.0, float(m.backoff_until or 0.0) - self._now())
        out: Dict[str, Any] = {
            "owns": False, "reason": "", "state": str(m.state or ""),
            "restarts": int(m.restarts or 0), "backoff_sec": int(round(backoff)),
            "last_error": str(m.last_error or "")[:160],
        }
        if m.state == "running" and has_send:
            out["owns"] = True
            return out
        if m.state != "running":
            out["reason"] = "worker_not_running"
            return out
        out["reason"] = "no_send_media"
        return out

    async def mark_read(self, platform: str, account_id: str, chat_key: str) -> bool:
        """把该会话标记已读（向平台发「已读」回执，拟人「先看后回」）。

        best-effort：无运行中 worker / worker 不支持 mark_read（当前 Messenger web 暂无；
        TG/WA/LINE 均已实现）/ 平台异常 → 一律 False 且绝不抛——已读只是拟人增强，
        失败不得阻断投递主流程。
        不过 send_blocked 护栏：读消息不是外发行为，冻结期也应照常已读（真人被限制发言
        仍会看消息）。
        """
        from src.integrations.humanize_metrics import record_read
        m = self._managed.get(account_key(platform, account_id))
        w = m.worker if (m is not None and m.state == "running") else None
        if w is None or not hasattr(w, "mark_read"):
            record_read(platform, False)
            return False
        try:
            ok = bool(await w.mark_read(chat_key))
            record_read(platform, ok)
            return ok
        except Exception:
            record_read(platform, False)
            logger.debug("[orchestrator] mark_read 失败 %s:%s chat=%s",
                         platform, account_id, chat_key, exc_info=True)
            return False

    async def send_chat_action(
        self, platform: str, account_id: str, chat_key: str,
        action: str = "typing",
    ) -> bool:
        """挂会话「正在输入 / 正在录音」状态（拟人：回复前对方看到打字/录音气泡）。

        ``action``：``typing`` | ``record_audio``（其余按 typing）。best-effort：无运行中
        worker / worker 不支持 / 异常 → False 且绝不抛。当前 TG+WA 有；**LINE 是协议层
        硬限制**（okline 无 typing/presence 端点，不是我们没接）；Messenger web 亦无。
        与 mark_read 同——状态提示不是外发消息，不过 send_blocked 护栏。
        """
        from src.integrations.humanize_metrics import record_typing
        m = self._managed.get(account_key(platform, account_id))
        w = m.worker if (m is not None and m.state == "running") else None
        if w is None or not hasattr(w, "send_chat_action"):
            record_typing(platform, False)
            return False
        try:
            ok = bool(await w.send_chat_action(chat_key, action))
            record_typing(platform, ok)
            return ok
        except Exception:
            record_typing(platform, False)
            logger.debug("[orchestrator] send_chat_action 失败 %s:%s chat=%s action=%s",
                         platform, account_id, chat_key, action, exc_info=True)
            return False

    async def delete_messages(
        self, platform: str, account_id: str, chat_key: str,
        message_ids: List[str], *, revoke: bool = True,
    ) -> Dict[str, Any]:
        """删除该会话的若干条消息（2026-08-17 官方级消息管理：双端撤回）。

        ``revoke=True``＝「对所有人删除」（TG delete_messages(revoke=True) /
        LINE unsend）。刻意**不过** send_blocked 护栏：撤回不是外发内容，
        与 mark_read 同理——冻结期坐席仍应能撤回发错的消息（只减暴露不增）。
        无运行中 worker / worker 无该能力 → ``{"ok": False, "reason": "no_worker"}``
        （不抛——调用方按 reason 给坐席人话提示）。
        """
        m = self._managed.get(account_key(platform, account_id))
        w = m.worker if (m is not None and m.state == "running") else None
        if w is None or not hasattr(w, "delete_messages"):
            return {"ok": False, "reason": "no_worker"}
        try:
            res = await w.delete_messages(
                chat_key, list(message_ids or []), revoke=bool(revoke))
            return res if isinstance(res, dict) else {"ok": bool(res)}
        except Exception as exc:
            logger.warning("[orchestrator] delete_messages 失败 %s:%s chat=%s: %s",
                           platform, account_id, chat_key, exc)
            return {"ok": False, "reason": str(exc)[:200] or "error"}

    async def delete_history(
        self, platform: str, account_id: str, chat_key: str, *,
        revoke: bool = True,
    ) -> Dict[str, Any]:
        """整段会话历史删除（2026-08-17「清空对方设备」）：当前仅 TG worker 有该能力。

        与 ``delete_messages`` 同语义：刻意**不过** send_blocked 护栏（删除只减
        暴露不增）；无运行中 worker / worker 无该能力（含 default 主账号会话）
        → ``{"ok": False, "reason": "no_worker"}``（不抛——调用方按 reason 给
        坐席人话提示）。
        """
        m = self._managed.get(account_key(platform, account_id))
        w = m.worker if (m is not None and m.state == "running") else None
        if w is None or not hasattr(w, "delete_history"):
            return {"ok": False, "reason": "no_worker"}
        try:
            res = await w.delete_history(chat_key, revoke=bool(revoke))
            return res if isinstance(res, dict) else {"ok": bool(res)}
        except Exception as exc:
            logger.warning("[orchestrator] delete_history 失败 %s:%s chat=%s: %s",
                           platform, account_id, chat_key, exc)
            return {"ok": False, "reason": str(exc)[:200] or "error"}

    async def send_media(
        self, platform: str, account_id: str, chat_key: str, *,
        media_path: str, media_url: str, media_type: str, caption: str = "",
        inbox_text: Optional[str] = None, sender_name: str = "",
        origin: str = "auto", mirror_media_type: str = "",
    ) -> Dict[str, Any]:
        """经 worker 发送媒体，并把出站媒体消息回写收件箱线程（media_ref 用 /static URL）。

        ``inbox_text``：**仅**回写给收件箱（坐席台可读）的文本，不发给客户；为 None 时回落
        ``caption``（向后兼容）。语音出站用它把「念了什么」带进会话视图——坐席不播放也能读，
        客户那边仍是纯语音（caption 不变）。

        ``sender_name``（P1-3，2026-08-02）：出站媒体行的「谁的音色/人设」显示名——仅回写
        收件箱（经 ``source`` 落 ``messages.sender_name``，坐席语音气泡显示徽标），不发给
        客户；空串＝不带（旧行为）。

        ``mirror_media_type``（2026-08-17 表情包主线）：收件箱镜像行的 media_type 覆写——
        发送形态与展示形态分叉时用（TG 动图贴纸经 ``animation``(GIF) 发出，工作台气泡
        仍按 ``sticker`` 渲染 webp）；空串＝跟随 ``media_type``（旧行为）。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——富媒体与文本同守。
        # origin（P1 2026-08-12）：manual=坐席人工路径用满额度；auto=自动链让路
        # 人工预留额度（companion_send_gate.reserve_for_manual）。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry,
            chat_key=str(chat_key or ""), origin=str(origin or "auto"))
        if _blk:
            # #77（0830 AW7MUV 实锤）：拦截日志必须带目标 peer——只记账号时
            # 「拦的是白名单客户还是其他客户」无从定性，豁免生效与否不可验证。
            logger.warning(
                "[orchestrator] 媒体发送被护栏拦截 %s:%s → peer=%s (%s, origin=%s)",
                platform, account_id, chat_key, _reason, origin)
            return {"delivered": False, "blocked": _reason}
        # #143（0902，接 #64/#106/#133）：媒体 caption 与文本同过出站语种收口——
        # 此前守卫只罩 send()，主动发图的配文（autosend/承诺兑现/相册秒发）从
        # send_media 出门零防护，「手机里存的这张…」中文配文直达英文客户。
        # 混语剥除 + 铆定/客户语言画像冲突 → 注入的翻译器修正；翻译 HOLD 时
        # **弃配文照发图**（图本身语言无关，宁可无配文，不发错语言配文）。
        # 人工路径（origin=manual）绝不动；镜像行 inbox_text 同步替换旧配文。
        if str(origin or "auto") != "manual" and str(caption or "").strip():
            _orig_cap = caption
            try:
                from src.ai.outbound_text_guard import (
                    resolve_cfg as _otg_cfg_m, sendpoint_lang_mix_pass)
                _gm_cfg = _otg_cfg_m(self._config)
                if _gm_cfg.get("enabled", True) and _gm_cfg.get("lang_mix", True):
                    _ccap, _cact = sendpoint_lang_mix_pass(caption)
                    if _cact == "hard_stripped":
                        logger.warning(
                            "[orchestrator] 媒体配文混语兜底已剥 CJK（#143）"
                            " %s:%s → peer=%s: %r → %r",
                            platform, account_id, chat_key,
                            caption[:60], _ccap[:60])
                        caption = _ccap
                if _gm_cfg.get("enabled", True) and _gm_cfg.get("lang_pin", True):
                    from src.ai.sendpoint_guard import sendpoint_lang_pin_fix
                    _cpt, _cpact = await sendpoint_lang_pin_fix(
                        platform, account_id, str(chat_key or ""), caption)
                    if _cpt is None:
                        logger.warning(
                            "[orchestrator] 媒体配文语言修正 HOLD → 弃配文照发图"
                            "（#143） %s:%s → peer=%s: %r",
                            platform, account_id, chat_key, caption[:60])
                        caption = ""
                    elif _cpt != caption:
                        caption = _cpt
                if caption != _orig_cap and inbox_text and _orig_cap:
                    # 镜像行别带旧配文（调用方惯用「[图片] 配文」格式）
                    inbox_text = inbox_text.replace(
                        _orig_cap, caption).strip() or inbox_text
            except Exception:
                logger.debug("[orchestrator] 媒体配文语种兜底异常（原样放行）",
                             exc_info=True)
        m = self._managed.get(account_key(platform, account_id))
        if not (m is not None and m.state == "running"
                and m.worker is not None and hasattr(m.worker, "send_media")):
            raise RuntimeError(f"无可用的运行中 worker(媒体): {platform}:{account_id}")
        # 反封号·去重微扰（默认关，opt-in）：同一张图发多人 → 文件哈希相同是垃圾信号。
        # 发送前产出「视觉无差、字节唯一」临时副本喂 worker，发完删除；canonical /static 原图不动
        # （仍供收件箱展示 + 下次微扰的源）。软失败回落原图，绝不阻断发送。仅本地文件路径可微扰。
        _send_path = media_path
        _dedup_temp = False
        try:
            from src.integrations.shared.media_dedup import perturb_for_send
            _send_path, _dedup_temp = perturb_for_send(
                media_path, media_type, self._config)
        except Exception:
            logger.debug("[orchestrator] 媒体去重微扰跳过", exc_info=True)
            _send_path, _dedup_temp = media_path, False
        # 透传 media_url 给支持的 worker（LINE/Messenger 官方通道需公网 URL 拉取）；
        # 旧 worker（telegram/wa-protocol/测试 fake）签名无此参 → 经签名探测跳过，零回归。
        _sm = m.worker.send_media
        # 排障插桩（2026-08-17 主号贴纸 loop 悬案）：静态推理与运行时行为矛盾
        # （守卫代码在盘在 pyc，发送却零守卫日志且跨 loop 崩）——把「实际派发给
        # 谁、当前 loop 是谁」打成事实。谜底揭开后可降 debug。
        # 2026-09-02 WEXX7E 悬死实锤后升级为三段观测：本行=「已受理」，worker impl
        # 起点打「开始执行」，下方 finally 后打「结果」——三段缺哪段，悬死点即哪段。
        try:
            logger.info("[orchestrator] send_media dispatch %s:%s worker=%s cur_loop=%s"
                        " type=%s origin=%s",
                        platform, account_id, type(m.worker).__name__,
                        id(asyncio.get_running_loop()), media_type, origin)
        except Exception:
            pass
        _kw: Dict[str, Any] = dict(
            media_path=_send_path, media_type=media_type, caption=caption)
        try:
            from src.inbox.group_thread import filter_send_kwargs, lookup_chat_type
            _ctype = lookup_chat_type(platform, account_id, chat_key)
            extra: Dict[str, Any] = {"media_url": media_url}
            if _ctype:
                extra["chat_type"] = _ctype
            _kw.update(filter_send_kwargs(_sm, extra))
        except Exception:
            try:
                import inspect
                if "media_url" in inspect.signature(_sm).parameters:
                    _kw["media_url"] = media_url
            except (ValueError, TypeError):
                pass
        _timeout = _send_media_timeout_sec(self._config)
        _t0 = time.monotonic()
        try:
            try:
                if _timeout > 0:
                    res = await asyncio.wait_for(_sm(chat_key, **_kw), timeout=_timeout)
                else:
                    res = await _sm(chat_key, **_kw)
            except asyncio.TimeoutError:
                # 超时兜底（2026-09-02 WEXX7E）：worker 悬死时 await 永久无果，
                # UI 只能靠前端超时猜「网络异常」。这里把悬死翻译成明确失败
                # （delivered=False → 路由回 502，坐席 5 秒内可读到真话），并
                # 尝试踢醒 worker（LINE 的签名桥僵死是已证成因，见
                # LineProtocolWorker.kick_stuck_send）。注意 wait_for 只取消
                # 协程侧，卡死的线程还在——不踢醒的话下一笔照样悬死。
                logger.warning(
                    "[orchestrator] send_media result %s:%s chat=%s type=%s "
                    "delivered=False error=send_timeout（%.0fs 无结果，worker 疑似悬死）",
                    platform, account_id, chat_key, media_type, _timeout)
                _kick = getattr(m.worker, "kick_stuck_send", None)
                if callable(_kick):
                    try:
                        _kick()
                    except Exception:
                        logger.debug("[orchestrator] kick_stuck_send 失败",
                                     exc_info=True)
                return {"delivered": False, "error": "send_timeout",
                        "timeout_sec": _timeout}
            except Exception as exc:
                # 三段观测之「结果=异常」：此前 worker 抛错只有调用方（路由/自动链）
                # 各自处置，编排器侧零痕迹——诊断包里对不上「派发了却没下文」。
                logger.warning(
                    "[orchestrator] send_media result %s:%s chat=%s type=%s "
                    "delivered=False elapsed=%dms exc=%s",
                    platform, account_id, chat_key, media_type,
                    int((time.monotonic() - _t0) * 1000), str(exc)[:200])
                raise
        finally:
            # 无论成功失败都清理微扰临时副本（原图不受影响）
            try:
                from src.integrations.shared.media_dedup import cleanup_temp
                cleanup_temp(_send_path, _dedup_temp)
            except Exception:
                logger.debug("[orchestrator] 微扰临时文件清理失败", exc_info=True)
        # 三段观测之「结果」：成功路径此前完全静默（send_line_media 成功只记 stats
        # 不打日志）——「dispatch 后没下文」到底是悬死还是成功，诊断包无从分辨。
        logger.info(
            "[orchestrator] send_media result %s:%s chat=%s type=%s delivered=%s "
            "mid=%s error=%s elapsed=%dms",
            platform, account_id, chat_key, media_type,
            (res.get("delivered", True) if isinstance(res, dict) else bool(res)),
            (str(res.get("message_id") or "") if isinstance(res, dict) else ""),
            (str(res.get("error") or "-") if isinstance(res, dict) else "-"),
            int((time.monotonic() - _t0) * 1000))
        # P0-4：带回平台消息 id(wamid)，让出站回写与 worker 的 fromMe 回显同键去重
        _mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            _itext = inbox_text if inbox_text is not None else caption
            emit_incoming(make_message(
                platform=platform, account_id=account_id, chat_key=chat_key,
                text=_itext, direction="out", msg_id=_mid,
                media_type=(mirror_media_type or media_type), media_ref=media_url,
                source=({"sender_name": str(sender_name)}
                        if sender_name else None),
            ))
        except Exception:
            logger.debug("[orchestrator] 出站媒体回写收件箱失败", exc_info=True)
        return res if isinstance(res, dict) else {"delivered": True}

    async def send(
        self, platform: str, account_id: str, chat_key: str, text: str,
        *, reply_to: Optional[Dict[str, Any]] = None,
        mentions: Optional[Any] = None,
        origin: str = "auto",
    ) -> Dict[str, Any]:
        """经受管 worker 发送，并把出站消息回写收件箱线程。

        P4-5B：``reply_to`` 携带原生引用回复上下文——若 worker 的 send 支持该 kwarg
        （WhatsApp 协议 worker）则透传发原生引用；否则退回普通发送（TypeError 兜底）。
        引用摘要一并写进出站消息的 source.reply_to，使本端气泡也渲染引用条。

        ``origin``（P1 2026-08-12 人工预留额度）：``manual``=坐席人工路径（收件箱
        发送路由/人工通过草稿投递）用完整日额度；缺省 ``auto``=自动链（主动问候/
        唤醒/关怀/L2 autosend）在 ``cap - reserve_for_manual`` 即让路。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——所有经编排器的
        # 外发（主动问候/唤醒/关怀/接管）都从这里走，旁路发送不再绕过急停与反封号。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry,
            chat_key=str(chat_key or ""), origin=str(origin or "auto"))
        if _blk:
            # #77：拦截日志带目标 peer（同媒体路径，白名单豁免可验证性）
            logger.warning(
                "[orchestrator] 发送被护栏拦截 %s:%s → peer=%s (%s, origin=%s)",
                platform, account_id, chat_key, _reason, origin)
            return {"delivered": False, "blocked": _reason}
        # #97/#105/#106（实施91）：出站收口点守卫三连——L2 autosend / 主动触达 /
        # 关怀 / 唤醒等全部**自动链**经编排器出门前统一过检：①混语确定性剥除
        # （0830 击穿实锤：deferred 链英文文案不经出稿口也不触发翻译出口，
        # 「I'm 我 …」直发英文客户）；②呼格纠正（peer_calls_you→call_peer 互换 +
        # call_peer 近形 baba→babe + 人设名当客户呼格剥除——#105 语音问候链、
        # #96 Steven 案的文本面同款病）；③铆定语言兜底（B67「发→X」explicit ×
        # 文字系统冲突 → 注入的翻译器修正；HOLD=放弃本条，无兜底纪律；
        # #133 起无显式铆定时回落客户语言画像——主动关怀/SOP/目标推进等一切
        # proactive 链经此总出口，纯中文再也到不了英文客户）。
        # **人工路径绝不动**（origin=manual 是坐席亲手打的字/人审后的终稿）；
        # 开关随 companion.outbound_text_guard.{enabled,lang_mix,vocative,
        # lang_pin}（默认开）。
        if str(origin or "auto") != "manual" and text:
            try:
                from src.ai.outbound_text_guard import (
                    resolve_cfg as _otg_cfg, sendpoint_lang_mix_pass)
                _g = _otg_cfg(self._config)
                if _g.get("enabled", True) and _g.get("lang_mix", True):
                    _clean, _act = sendpoint_lang_mix_pass(text)
                    if _act == "hard_stripped":
                        logger.warning(
                            "[orchestrator] 发送口混语兜底已剥 CJK（#97）"
                            " %s:%s → peer=%s: %r → %r",
                            platform, account_id, chat_key,
                            text[:60], _clean[:60])
                        text = _clean
                    elif _act == "hard_kept":
                        logger.warning(
                            "[orchestrator] 发送口混语命中但剥后过短，保留原文"
                            "（#97） %s:%s: %r",
                            platform, account_id, text[:60])
                if _g.get("enabled", True) and _g.get("vocative", True):
                    from src.ai.sendpoint_guard import (
                        resolve_sendpoint_names, sendpoint_vocative_pass)
                    _nm = resolve_sendpoint_names(
                        self._config, platform, account_id,
                        str(chat_key or ""), registry=self._registry)
                    # #155：爱称按联系人取（守卫与 prompt 注入必须同源，否则
                    # 守卫会拿人设的 babe 去「纠正」本该是联系人 honey 的正确文本）
                    try:
                        from src.inbox.contact_names import (
                            overlay_sendpoint_names)
                        _nm = overlay_sendpoint_names(
                            _nm, platform, account_id, str(chat_key or ""))
                    except Exception:
                        pass
                    if _nm:
                        _vt, _vm = sendpoint_vocative_pass(text, _nm)
                        if _vt != text:
                            logger.warning(
                                "[orchestrator] 发送口呼格已纠正（#105/#96）"
                                " %s:%s → peer=%s: swap=%s near=%s self=%s",
                                platform, account_id, chat_key,
                                _vm.get("swap_hits"), _vm.get("near_hits"),
                                _vm.get("self_voc_hits"))
                            text = _vt
                if _g.get("enabled", True) and _g.get("lang_pin", True):
                    from src.ai.sendpoint_guard import sendpoint_lang_pin_fix
                    _pt, _pact = await sendpoint_lang_pin_fix(
                        platform, account_id, str(chat_key or ""), text)
                    if _pt is None:
                        # HOLD：发错语言比不发更糟（2026-08-17 无兜底纪律）
                        return {"delivered": False,
                                "blocked": "lang_pin_hold"}
                    if _pt != text:
                        text = _pt
            except Exception:
                logger.debug("[orchestrator] 收口点守卫异常（原样放行）",
                             exc_info=True)
        m = self._managed.get(account_key(platform, account_id))
        if not (m is not None and m.state == "running"
                and m.worker is not None and hasattr(m.worker, "send")):
            raise RuntimeError(f"无可用的运行中 worker: {platform}:{account_id}")
        # P4-5B reply_to + P4-11 mentions + 群 chat_type：只透 worker **具名**形参
        # （filter_send_kwargs 刻意不把 **kwargs 当全收）。引用条能否镜像看
        # quote_applied 回执，绝不因「传了 reply_to」就在工作台画引用
        # （2026-08-14 173 实录：坐席见引用、客户端没有）。
        from src.inbox.group_thread import (
            annotate_quote_applied, filter_send_kwargs, lookup_chat_type,
            should_mirror_quote,
        )
        _want: Dict[str, Any] = {}
        if reply_to:
            _want["reply_to"] = reply_to
        if mentions:
            _want["mentions"] = mentions
        _ctype = lookup_chat_type(platform, account_id, chat_key)
        if _ctype:
            _want["chat_type"] = _ctype
        _kw = filter_send_kwargs(m.worker.send, _want)
        if _kw:
            res = await m.worker.send(chat_key, text, **_kw)
        else:
            res = await m.worker.send(chat_key, text)
        res = annotate_quote_applied(res)
        # P0-4：带回平台消息 id(wamid)，让出站回写与 worker 的 fromMe 回显同键去重
        _mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        # 失败不镜像：worker 明确报 delivered=False 的消息**没有发出去**，回写会在
        # 收件箱伪造一条对端根本看不到的出站气泡，且群发言台账（speech ledger 从
        # inbox 读 out 方向）会把它算进暴露面——2026-07-27 群演灰度实锤：连炸三场
        # 的 6 条失败台词全部进了线程，坐席视角"发了"、群里啥也没有。
        _delivered = (not isinstance(res, dict)) or (res.get("delivered", True)
                                                     is not False)
        if _delivered:
            try:
                from src.integrations.protocol_bridge import emit_incoming, make_message
                _src = None
                if should_mirror_quote(reply_to, res):
                    _src = {"reply_to": {
                        "id": str(reply_to.get("id") or ""),
                        "text": str(reply_to.get("text") or ""),
                        "sender": str(reply_to.get("sender") or ""),
                    }}
                emit_incoming(make_message(
                    platform=platform, account_id=account_id, chat_key=chat_key,
                    text=text, direction="out", msg_id=_mid, source=_src,
                ))
                # P4-4：Telegram 发送成功即置「已发送」（单勾）；对端读后由
                # UpdateReadHistoryOutbox 回执升级为「已读」（蓝色双勾）。
                if platform == "telegram" and _mid:
                    from src.integrations.protocol_bridge import report_message_status
                    report_message_status(platform, account_id, chat_key, _mid, "sent")
            except Exception:
                logger.debug("[orchestrator] 出站回写收件箱失败", exc_info=True)
        return res if isinstance(res, dict) else {"delivered": True}

    async def invite_to_group(self, platform: str, inviter_id: str,
                              chat_key: str, user_ref: str) -> Dict[str, Any]:
        """经群内受管号把 ``user_ref`` 拉进群（排班补位）。``{ok, kind, error}``。

        邀请属高风控出站动作 → 与 send 同过 ``send_blocked`` 护栏（Kill-Switch/
        反封号闸门冻结中的号不许去拉人）。worker 无该能力 → unsupported 如实回报。
        """
        _blk, _reason = send_blocked(
            platform, inviter_id, config=self._config, registry=self._registry,
            chat_key=str(chat_key or ""))
        if _blk:
            return {"ok": False, "kind": "blocked", "error": str(_reason)}
        m = self._managed.get(account_key(platform, inviter_id))
        w = getattr(m, "worker", None) if m is not None else None
        fn = getattr(w, "invite_to_group", None) if w is not None else None
        if fn is None or not (m is not None and m.state == "running"):
            return {"ok": False, "kind": "unsupported",
                    "error": f"无可用的运行中 worker(invite): {platform}:{inviter_id}"}
        try:
            res = await fn(str(chat_key), str(user_ref))
            return res if isinstance(res, dict) else {
                "ok": bool(res), "kind": "invited" if res else "error", "error": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[orchestrator] invite_to_group 异常 %s:%s: %s",
                           platform, inviter_id, exc)
            return {"ok": False, "kind": "error", "error": str(exc)}

    async def ensure_peer(self, platform: str, account_id: str,
                          chat_key: str) -> Dict[str, Any]:
        """群 peer 可达性体检（开演前 preflight 入口）：``{ok, checked, error?}``。

        worker 具备 ``ensure_peer`` 能力才真查（当前=Telegram companion worker）；
        无能力/查询自身异常 → ``ok=True, checked=False`` **放行不拦**——preflight
        只拦「确定不可达」，不确定时交给发送路径的 peer 自愈兜底，避免网络抖动
        把一场好戏误杀在后台。
        """
        m = self._managed.get(account_key(platform, account_id))
        w = getattr(m, "worker", None) if m is not None else None
        fn = getattr(w, "ensure_peer", None) if w is not None else None
        if fn is None:
            return {"ok": True, "checked": False}
        try:
            ok = bool(await fn(str(chat_key)))
            return {"ok": ok, "checked": True}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[orchestrator] ensure_peer 异常 %s:%s", platform,
                         account_id, exc_info=True)
            return {"ok": True, "checked": False, "error": str(exc)}

    # ── 状态 ─────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        accts = [m.to_dict() for m in self._managed.values()]
        return {
            "running_loop": self._running,
            "interval": self._interval,
            "total": len(accts),
            "by_state": _count_by_state(accts),
            "accounts": accts,
        }


def _count_by_state(accts: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for a in accts:
        out[a["state"]] = out.get(a["state"], 0) + 1
    return out


# ── 内置 worker（懒注册，门控） ───────────────────────────────────────────────

def ensure_builtin_workers(config: Dict[str, Any]) -> None:
    """按需注册内置 worker（幂等、门控）。"""
    try:
        from src.integrations.telegram_protocol_login import (
            is_pyrogram_available, protocol_enabled as tg_enabled, resolve_credentials,
        )
        # B 线薄连接 worker 不经 telegram_client 模块 → 这里补同一发 channel id
        # 边界补丁（幂等），防「A 线好好的、B 线薄壳撞超 int32 群 id」的半修状态。
        try:
            from src.client.pyrogram_compat import ensure_wide_channel_ids
            ensure_wide_channel_ids()
        except Exception:
            pass
        # 凭据判定与登录侧 maybe_register 同款豁免（P1-198，2026-08-05）：
        # 中央池/托管部署无需本地自带 api_id——runner 起号时按账号 meta 粘定键
        # 向池取凭据（或托管网关已在 init 期注入 config）。旧门槛在「网关/池
        # 暂不可达的启动窗口」里会永远注册不上工厂 → 重启后在线号全部不恢复，
        # 且 error 只有 debug 级（198 取证实锤：backend.log 零编排器痕迹）。
        _tg_creds_ok = resolve_credentials(config) is not None
        if not _tg_creds_ok:
            try:
                from src.integrations.credpool_bridge import credpool_enabled
                _tg_creds_ok = credpool_enabled(config)
            except Exception:
                _tg_creds_ok = False
        if (tg_enabled(config) and is_pyrogram_available() and _tg_creds_ok
                and get_worker_factory("telegram", "protocol") is None):
            # N 线 核心4：companion_runtime 开 → 协议号跑 A 线"有灵魂"client；否则用 B 线薄连接
            from src.integrations.telegram_companion_worker import (
                TelegramCompanionWorker, companion_runtime_enabled,
            )
            if companion_runtime_enabled(config):
                register_worker("telegram", "protocol",
                                lambda acc, cfg: TelegramCompanionWorker(acc, cfg))
                logger.info("[orchestrator] Telegram 协议号将使用 A 线统一运行时（companion_runtime）")
            else:
                register_worker("telegram", "protocol",
                                lambda acc, cfg: TelegramProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 telegram worker 失败", exc_info=True)
    try:
        from src.integrations.whatsapp_baileys_login import protocol_enabled as wa_enabled
        if wa_enabled(config) and get_worker_factory("whatsapp", "protocol") is None:
            register_worker("whatsapp", "protocol",
                            lambda acc, cfg: WhatsAppProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 whatsapp worker 失败", exc_info=True)
    try:
        from src.integrations.messenger_web_login import web_enabled as mg_web_enabled
        if mg_web_enabled(config) and get_worker_factory("messenger", "web") is None:
            register_worker("messenger", "web",
                            lambda acc, cfg: MessengerWebWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 messenger web worker 失败", exc_info=True)
    try:
        from src.integrations.zalo_personal_login import web_enabled as zl_web_enabled
        if zl_web_enabled(config) and get_worker_factory("zalo", "web") is None:
            register_worker("zalo", "web",
                            lambda acc, cfg: ZaloPersonalWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 zalo personal worker 失败", exc_info=True)
    try:
        from src.integrations.instagram_web_login import web_enabled as ig_web_enabled
        if ig_web_enabled(config) and get_worker_factory("instagram", "web") is None:
            register_worker("instagram", "web",
                            lambda acc, cfg: InstagramWebWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 instagram web worker 失败", exc_info=True)
    try:
        from src.integrations.line_protocol_login import (
            protocol_enabled as line_enabled, is_okline_available,
        )
        if (line_enabled(config) and is_okline_available()
                and get_worker_factory("line", "protocol") is None):
            register_worker("line", "protocol",
                            lambda acc, cfg: LineProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 line protocol worker 失败", exc_info=True)
    # 官方 API 出站 worker（LINE/Messenger/WhatsApp Cloud，mode=official；G 延伸）
    try:
        from src.integrations.official_api_worker import register_official_workers
        register_official_workers(config)
    except Exception:
        logger.debug("[orchestrator] 注册官方 worker 失败", exc_info=True)


class TelegramProtocolWorker:
    """保活一个 Telegram pyrogram 协议连接（从 M2 落地的 session 拉起）。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.session_name = str((account.get("meta") or {}).get("session_name") or "")
        # N2/N4：优先 session_string 内存启动（抗文件 session SQLite 锁 / DC 迁移不稳）
        self.session_string = str((account.get("meta") or {}).get("session_string") or "")
        self.client: Any = None
        self.state = "stopped"
        self.detail = ""

        self._voice_transcriber: Any = None
        self._voice_transcriber_tried = False

    def _get_voice_transcriber(self) -> Any:
        """惰性建 ASR（B 线视频音轨理解用；失败则不再重试）。"""
        if self._voice_transcriber is not None:
            return self._voice_transcriber
        if self._voice_transcriber_tried:
            return None
        self._voice_transcriber_tried = True
        try:
            vc = (self.config.get("voice_recognition") or {})
            if not vc.get("enabled"):
                return None
            from src.voice_transcriber import (
                VoiceTranscriberFactory,
                register_shared_transcriber,
            )
            self._voice_transcriber = VoiceTranscriberFactory.create_transcriber(vc)
            register_shared_transcriber(self._voice_transcriber)
        except Exception:
            logger.debug("[tg-worker] voice transcriber 初始化失败", exc_info=True)
        return self._voice_transcriber

    def _proxy(self) -> Optional[Dict[str, Any]]:
        pid = self.account.get("proxy_id") or ""
        if not pid:
            return None
        try:
            from src.integrations.proxy_pool import get_proxy_pool
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy
            return _to_pyrogram_proxy(get_proxy_pool().get(pid, mask=False))
        except Exception:
            return None

    async def start(self) -> None:
        # 凭据解析统一走 credpool_bridge：账号 meta 里有中央池粘定键就向池索取
        # （池按 key 粘定返回同一组凭据，与该 session 登录时所用的一致），
        # 没有/池不可达则回落配置自带凭据——行为与接池之前完全一致。
        from src.integrations.credpool_bridge import aresolve_for_account
        alloc = await aresolve_for_account(self.config, account=self.account)
        if alloc is None or not (self.session_name or self.session_string):
            raise RuntimeError("缺少 api 凭据或 session_name/session_string")
        api_id, api_hash = alloc.api_id, alloc.api_hash
        # 重试前先清理可能残留的旧 client，避免连接泄漏
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None
        from pyrogram import Client
        kwargs: Dict[str, Any] = dict(api_id=api_id, api_hash=api_hash)
        # 出口优先级：账号上显式绑定的代理 > 中央池按付费档下发的独立出口。
        # 显式绑定代表运营意图，不该被自动分配悄悄覆盖。
        proxy = self._proxy()
        if not proxy and alloc.proxy:
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy
            proxy = _to_pyrogram_proxy(alloc.proxy)
        if proxy:
            kwargs["proxy"] = proxy
        # 设备指纹：与该号扫码时用的同一种子派生，逐次连接恒定。
        # 存量账号（meta 无种子）不受影响，保持 pyrogram 默认值。
        from src.integrations.device_fingerprint import client_kwargs_for_account
        kwargs.update(client_kwargs_for_account(self.config, self.account))
        if self.session_string:
            # N2/N4：内存会话启动——不碰 sessions/*.session 文件，规避扫码 client
            # 残留连接造成的 "database is locked"，也更抗 DC 迁移。
            name = self.session_name or f"mem_{self.account_id}"
            self.client = Client(name, session_string=self.session_string, **kwargs)
        else:
            kwargs["workdir"] = "sessions"
            self.client = Client(self.session_name, **kwargs)
        await self.client.start()
        self._wire_inbound()
        self._wire_receipts()
        self.state = "running"
        self.detail = ""
        await self._backfill()
        # 目录同步（好友名单 + 会话占位）：与 LINE worker 同构的 best-effort 后台任务——
        # 刻意**不 await**，大号上千好友会把 start 拖成分钟级，而账号在线态不该等名单。
        try:
            asyncio.create_task(self._sync_directory_bootstrap())
        except Exception:  # noqa: BLE001
            logger.debug("[tg-worker] 目录同步调度失败", exc_info=True)

    async def _sync_directory_bootstrap(self) -> None:
        """登录后一次性目录同步（协议号无常驻 loop，不为它改架构）。

        只写通讯录 + 会话占位，绝不喂消息管道（红线见
        ``src/integrations/telegram_directory_sync.py`` 的模块 docstring）。
        """
        try:
            from src.integrations.telegram_directory_sync import (
                directory_sync_cfg, sync_directory_once,
            )
            cfg = directory_sync_cfg(self.config)
            stats = await sync_directory_once(self.client, self.account_id, cfg)
            if stats.get("contacts") or stats.get("chats"):
                logger.info("[tg-worker] 目录同步完成 通讯录=%d 会话占位=%d account=%s",
                            stats.get("contacts", 0), stats.get("chats", 0), self.account_id)
        except Exception:  # noqa: BLE001
            logger.debug("[tg-worker] 目录同步失败 account=%s", self.account_id, exc_info=True)

    def _wire_inbound(self) -> None:
        """注册 pyrogram 消息处理器：收到消息 → 推入统一收件箱（best-effort）。"""
        try:
            from pyrogram.handlers import MessageHandler

            account_id = self.account_id

            async def _on_msg(_client: Any, message: Any) -> None:  # noqa: ANN401
                try:
                    from src.ai.inbound_video import (
                        enrich_tg_video_payload,
                        resolve_inbound_video_max_bytes,
                    )
                    from src.integrations.protocol_bridge import (
                        download_tg_media, emit_incoming, maybe_auto_reply,
                        tg_message_payload,
                    )
                    _max_v = resolve_inbound_video_max_bytes(self.config)
                    media_type, media_ref = await download_tg_media(
                        message, account_id, max_bytes=_max_v,
                    )
                    payload = tg_message_payload(
                        message, account_id,
                        media_type=media_type, media_ref=media_ref)
                    if payload is not None:
                        payload = await enrich_tg_video_payload(
                            message, payload,
                            config=self.config,
                            voice_transcriber=self._get_voice_transcriber(),
                        )
                        emit_incoming(payload)
                        await maybe_auto_reply(payload)
                except Exception:
                    logger.debug("[tg-worker] inbound 推送失败", exc_info=True)

            self.client.add_handler(MessageHandler(_on_msg))
        except Exception:
            logger.debug("[tg-worker] 注册消息处理器失败", exc_info=True)

    def _wire_receipts(self) -> None:
        """注册 pyrogram 原始更新处理器：对端读了我们发的消息（``UpdateReadHistoryOutbox``
        / 频道版 ``UpdateReadChannelOutbox``）→ 把该会话 ≤max_id 的出站消息升级为「已读」，
        前端出站气泡即显示蓝色双勾；对端删了消息（``UpdateDeleteMessages`` /
        ``UpdateDeleteChannelMessages``，#146）→ 镜像软删 + 关联记忆清理
        （``protocol_bridge.report_deleted_messages``）。均 best-effort，不影响主消息流。"""
        try:
            from pyrogram.handlers import RawUpdateHandler
            from pyrogram import raw

            account_id = self.account_id

            async def _on_raw(_client: Any, update: Any, _users: Any, _chats: Any) -> None:  # noqa: ANN401
                try:
                    from src.integrations.protocol_bridge import (
                        report_deleted_messages, report_read_upto,
                        tg_peer_to_chat_key,
                    )
                    if isinstance(update, raw.types.UpdateReadHistoryOutbox):
                        ck = tg_peer_to_chat_key(getattr(update, "peer", None))
                        if ck:
                            report_read_upto("telegram", account_id, ck,
                                             getattr(update, "max_id", 0))
                    elif isinstance(update, raw.types.UpdateReadChannelOutbox):
                        chid = getattr(update, "channel_id", None)
                        if chid is not None:
                            report_read_upto("telegram", account_id, f"-100{int(chid)}",
                                             getattr(update, "max_id", 0))
                    # #146（2026-09-02）：对端手机删消息 → 工作台镜像软删 + 关联记忆清理。
                    # B87（实施68）只接在 A 线 companion client 上；协议多开账号走本 worker，
                    # 此前删除事件根本没人听——「手机删了、工作台还在、AI 记忆还在」。
                    # UpdateDeleteMessages（私聊/小群）只带裸 id → 全账号按 platform_msg_id；
                    # UpdateDeleteChannelMessages 带 channel_id → 收窄到该会话。
                    elif isinstance(update, raw.types.UpdateDeleteMessages):
                        mids = [str(m) for m in (getattr(update, "messages", None) or [])]
                        if mids:
                            report_deleted_messages("telegram", account_id, mids)
                    elif isinstance(update, raw.types.UpdateDeleteChannelMessages):
                        chid = getattr(update, "channel_id", None)
                        mids = [str(m) for m in (getattr(update, "messages", None) or [])]
                        if chid is not None and mids:
                            report_deleted_messages(
                                "telegram", account_id, mids, chat_key=f"-100{int(chid)}")
                except Exception:
                    logger.debug("[tg-worker] 原始更新（已读/删除）处理失败", exc_info=True)

            self.client.add_handler(RawUpdateHandler(_on_raw))
        except Exception:
            logger.debug("[tg-worker] 注册已读回执处理器失败", exc_info=True)

    def _backfill_limit(self) -> int:
        try:
            tg = ((self.config.get("platform_login") or {}).get("telegram") or {})
            return int(tg.get("backfill_dialogs", 20) or 0)
        except Exception:
            return 0

    async def _backfill(self) -> None:
        """首连历史回填（best-effort，不阻断启动）。"""
        try:
            from src.integrations.protocol_bridge import backfill_telegram
            limit = self._backfill_limit()
            if limit > 0:
                await backfill_telegram(self.client, self.account_id, limit)
        except Exception:
            logger.debug("[tg-worker] 历史回填失败", exc_info=True)

    async def _on_client_loop(self, coro_factory: Any) -> Dict[str, Any]:
        """在 client 自己的事件循环上执行发送协程（loop 亲和守卫）。

        pyrogram 的 Client/Session 在**创建时**绑定 ``asyncio.get_event_loop()``，
        从别的 loop ``await`` 其 invoke/上传必炸 ``attached to a different loop``
        （2026-08-17 实锤：web 路由对主号发贴纸在 upload.SaveFilePart 首块即崩；
        2026-08-16 voice_sender 同族先例）。守卫语义与实现见共享助手
        ``telegram_companion_worker.run_on_client_loop``（两个 TG worker 单源）。
        """
        from src.integrations.telegram_companion_worker import (
            client_bound_loop, run_on_client_loop,
        )
        return await run_on_client_loop(
            client_bound_loop(self.client), coro_factory)

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._on_client_loop(
            lambda: self._send_impl(chat_key, text, reply_to=reply_to))

    async def _send_impl(self, chat_key: str, text: str,
                         *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        # 引用回复（P3 双面板融合 2026-08-13）：此前无 reply_to 形参 → 编排器签名探测
        # 降级到裸发＝引用被静默丢弃（平台注册表曾如实标 quote_reply workspace=none）。
        # TG 的 platform_msg_id 就是 pyrogram 消息 id → 原生 reply_to_message_id；
        # id 解析不了（异常形态）→ 降级普通发送，绝不阻断。
        kw: Dict[str, Any] = {}
        if reply_to and reply_to.get("id"):
            try:
                kw["reply_to_message_id"] = int(str(reply_to.get("id")))
            except (TypeError, ValueError):
                pass
        msg = await self.client.send_message(target, text, **kw)
        return {
            "delivered": True,
            "message_id": str(getattr(msg, "id", "") or ""),
            "quote_applied": "reply_to_message_id" in kw,
        }

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        return await self._on_client_loop(
            lambda: self._send_media_impl(
                chat_key, media_path=media_path, media_type=media_type,
                caption=caption))

    async def _send_media_impl(self, chat_key: str, *, media_path: str,
                               media_type: str, caption: str = "") -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        kind = str(media_type or "").lower()
        if kind == "image":
            msg = await self.client.send_photo(target, media_path, caption=caption)
        elif kind == "voice":
            # #130：显式 duration（三层探测，纯 python 兜底）——见
            # telegram_companion_worker 同分支注释；探测不出＝旧行为不带。
            _vdur: Optional[int] = None
            try:
                from src.client.voice_sender import probe_audio_duration_ms
                _vms = probe_audio_duration_ms(media_path)
                if _vms and _vms > 0:
                    _vdur = max(1, int(round(_vms / 1000.0)))
            except Exception:
                _vdur = None
            if _vdur:
                msg = await self.client.send_voice(
                    target, media_path, caption=caption, duration=_vdur)
            else:
                msg = await self.client.send_voice(
                    target, media_path, caption=caption)
        elif kind == "video":
            msg = await self.client.send_video(target, media_path, caption=caption)
        elif kind == "sticker":
            # 2026-08-17 表情包主线：webp → Telegram 原生贴纸（pyrogram 2.0.106
            # 的 send_sticker 无 caption 形参——贴纸本就无配文语义，忽略 caption）。
            msg = await self.client.send_sticker(target, media_path)
        elif kind == "animation":
            # 动图贴纸在 TG 走 GIF 动画（animated webp 无视频贴纸语义，GIF 观感最好）
            msg = await self.client.send_animation(target, media_path, caption=caption)
        else:
            msg = await self.client.send_document(target, media_path, caption=caption)
        return {"delivered": True, "message_id": str(getattr(msg, "id", "") or "")}

    async def mark_read(self, chat_key: str) -> bool:
        """对该会话发「已读」回执（pyrogram read_chat_history）。

        拟人「先看后回」：投递自动回复前调用，对端客户端上先出现「已读」、再收到回复，
        消除「消息还未读却被回复」的机器人破绽。
        """
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        await self.client.read_chat_history(target)
        return True

    async def send_chat_action(self, chat_key: str, action: str = "typing") -> bool:
        """挂 Telegram「正在输入 / 正在录制语音」状态（约 5s 自动过期）。

        投递前的拟人打字延迟期间周期性调用，让对端看到「对方正在输入…」，与真人一致。
        """
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        from pyrogram.enums import ChatAction
        act = ChatAction.RECORD_AUDIO if str(action) == "record_audio" else ChatAction.TYPING
        await self.client.send_chat_action(target, act)
        return True

    async def delete_messages(self, chat_key: str, message_ids: List[str],
                              *, revoke: bool = True) -> Dict[str, Any]:
        """删除若干条消息（2026-08-17 双端撤回）：pyrogram ``delete_messages``。

        ``revoke=True``＝对所有人删除（TG 私聊自己的消息基本无时限；群聊受权限
        限制，平台拒绝时如实回传）。platform_msg_id 即 pyrogram 消息 id（int）。
        """
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        ids: List[int] = []
        for i in (message_ids or []):
            try:
                ids.append(int(str(i)))
            except (TypeError, ValueError):
                continue
        if not ids:
            return {"ok": False, "reason": "bad_ids"}
        n = await self.client.delete_messages(target, ids, revoke=bool(revoke))
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = len(ids)
        return {"ok": n > 0, "deleted": n}

    async def delete_history(self, chat_key: str,
                             *, revoke: bool = True) -> Dict[str, Any]:
        """整段会话历史双向删除（2026-08-17「清空对方设备」）。

        实现共用 ``telegram_companion_worker.tg_delete_full_history``（raw
        ``messages.DeleteHistory(revoke=True)``，TG 私聊独有官方能力；
        超级群/频道如实 ``unsupported_chat_type``）。
        """
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        from src.integrations.telegram_companion_worker import tg_delete_full_history
        return await tg_delete_full_history(self.client, chat_key, revoke=revoke)

    async def stop(self) -> None:
        try:
            if self.client is not None:
                await self.client.stop()
        except Exception:
            pass
        self.client = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        try:
            return bool(self.client is not None and self.client.is_connected)
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "telegram_protocol", "session": self.session_name,
                "state": self.state, "detail": self.detail}


class WhatsAppProtocolWorker:
    """薄监督一个 WhatsApp(Baileys) 账号：确保 Node 已恢复 + 读其状态。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""
        # P1 身份化：上次已回填的 (昵称, 头像URL)——健康轮询每 ~15s 一次，仅当 Node 侧
        # 身份变化才走 enrich（避免每 tick 白读注册表 + 观测计数虚增）。
        self._last_profile: tuple = ("", "")

    def _base(self) -> str:
        from src.integrations.whatsapp_baileys_login import service_base_url
        return service_base_url(self.config)

    def _session_unhealthy(self) -> bool:
        """Node push 的会话健康登记显示该账号被登出/重连放弃 → 自动发送快速失败
        （与 MessengerWebWorker 同口径；仅拦自动路径，未上报过 = 不拦）。"""
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            return get_platform_session_health().is_unhealthy(
                "whatsapp", self.account_id)
        except Exception:
            return False

    async def start(self) -> None:
        from src.integrations.whatsapp_baileys_login import _get_json, _post_json
        # 触发 Node 恢复所有持久化 session（幂等）；Node 自身也会在开机时恢复
        await _post_json(f"{self._base()}/accounts/restore", {})
        # 真自愈（2026-07 事故）：Node 的 restoreAll 对内存里已存在的 session（哪怕
        # expired 假死态）直接 skip → 断线账号靠 restore 永远拉不活，编排器
        # error→退避→start() 循环空转两小时无人管。restore 后核对 /accounts：自己
        # 不在列表 → 追加账号级 reconnect（Node 新端点契约：404={ok:false}；已在线
        # ={ok:true,already:true}；触发重连={ok:true,reconnecting:true}），第一轮
        # 退避重启就能真正拉活 expired 会话。best-effort：核对/reconnect 失败（Node
        # 可能正在重启、或旧版无此端点）只记 debug 不抛——start() 抛异常会被编排器
        # 计为启动失败再进退避，反而拖慢下一轮自愈。
        try:
            res = await _get_json(f"{self._base()}/accounts")
            rows = (res or {}).get("accounts") or []
            ids = {str(a.get("account_id") or "") for a in rows}
            if self.account_id and self.account_id not in ids:
                await _post_json(
                    f"{self._base()}/accounts/{self.account_id}/reconnect", {})
        except Exception:
            logger.debug("[orchestrator] WA reconnect 自愈调用失败（忽略）account=%s",
                         self.account_id, exc_info=True)
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src.integrations.whatsapp_baileys_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "whatsapp session unhealthy (logged out / reconnect gave up)"}
        payload: Dict[str, Any] = {"jid": chat_key, "text": text}
        # P4-5B 原生引用回复：把被引用消息 key + 文本摘要下发给 Baileys 的 quoted 选项
        quoted = False
        if reply_to and reply_to.get("id"):
            quoted = True
            payload["quoted"] = {
                "id": str(reply_to.get("id") or ""),
                "from_me": bool(reply_to.get("from_me")),
                "participant": str(reply_to.get("participant") or ""),
                "text": str(reply_to.get("text") or ""),
            }
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send", payload,
        )
        delivered = bool((res or {}).get("ok", True))
        return {"delivered": delivered,
                "message_id": str((res or {}).get("message_id") or ""),
                "quote_applied": bool(quoted and delivered)}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        from src.integrations.protocol_bridge import (
            normalize_outbound_media_type,
        )
        from src.integrations.whatsapp_baileys_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "whatsapp session unhealthy (logged out / reconnect gave up)"}
        # 工单 #143（2026-09-02 skuio）：media_type 归一化后必传——相册链的
        # "photo" 裸传边车不在其白名单，落 document 分支 → 对方端图片显示成
        # 点不开的「文档」。别名归一 + 缺失/陌生值按扩展名兜底。
        _raw_mt = str(media_type or "").strip().lower()
        media_type = normalize_outbound_media_type(media_type, media_path)
        if media_type != _raw_mt:
            logger.info(
                "[orchestrator] WA media_type 归一 %r → %r path=%s",
                _raw_mt, media_type, media_path)
        # Python 侧 PTT 闸：边车 400 之前先拦，失败原因进 delivered=False
        # 供 autosend 回落文字（与 Baileys looksLikeOggOpus 同口径）。
        if str(media_type or "").strip().lower() == "voice":
            try:
                from src.client.voice_ptt_gate import (
                    is_ptt_ready, ptt_ready_reason,
                )
                if not is_ptt_ready(media_path):
                    why = ptt_ready_reason(media_path) or "ptt_not_ogg_opus"
                    return {
                        "delivered": False,
                        "blocked": "ptt_format",
                        "error": why,
                    }
            except Exception:
                return {
                    "delivered": False,
                    "blocked": "ptt_format",
                    "error": "ptt_gate_error",
                }
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send-media",
            {"jid": chat_key, "path": media_path,
             "media_type": media_type, "caption": caption},
        )
        return {"delivered": bool((res or {}).get("ok", True)),
                "message_id": str((res or {}).get("message_id") or "")}

    async def mark_read(self, chat_key: str) -> bool:
        """已读回执：把该会话对端最近消息标记已读（Baileys readMessages）。

        session 不健康 → 跳过（不打死会话）。返回是否真的标了未读（无未读→False）。
        """
        if self._session_unhealthy():
            return False
        from src.integrations.whatsapp_baileys_login import _post_json
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/read",
            {"jid": chat_key}, timeout=10.0,
        )
        return bool((res or {}).get("ok") and (res or {}).get("marked"))

    async def send_chat_action(self, chat_key: str, action: str = "typing") -> bool:
        """打字/录音状态：向对端发 presence（typing→composing / record_audio→recording）。"""
        if self._session_unhealthy():
            return False
        state = "recording" if str(action) == "record_audio" else "composing"
        from src.integrations.whatsapp_baileys_login import _post_json
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/typing",
            {"jid": chat_key, "state": state}, timeout=10.0,
        )
        return bool((res or {}).get("ok"))

    async def stop(self) -> None:
        # 不登出（Baileys 连接由 Node 保活）；仅停止 Python 侧监督
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.whatsapp_baileys_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            rows = (res or {}).get("accounts") or []
            ids = {str(a.get("account_id") or "") for a in rows}
            await self._maybe_enrich_self_profile(rows)
            return self.account_id in ids
        except Exception:
            return False

    async def _maybe_enrich_self_profile(self, rows: Any) -> None:
        """P1 身份化机会式回填：健康轮询的 /accounts 已带回自身昵称/头像 →
        写 registry meta.self_*（连接中心/切换条显真实身份）。

        关键在覆盖「服务重启后 Node restoreAll 自动重连」的存量账号——它们不经
        登录轮询，此前永远拿不到身份。仅当 Node 侧身份较上次变化才 enrich
        （enrich 内部另有幂等跳写）；flag 关时 enrich 自身 no-op。绝不抛。
        """
        try:
            row = next((a for a in (rows or [])
                        if str(a.get("account_id") or "") == self.account_id), None)
            if row is None:
                return
            name = str(row.get("pushname") or "")
            avatar = str(row.get("avatar_url") or "")
            if not (name or avatar) or (name, avatar) == self._last_profile:
                return
            from src.integrations.account_self_profile import enrich_from_fields
            await enrich_from_fields(
                "whatsapp", self.account_id,
                name=name, avatar_url=avatar, config=self.config)
            self._last_profile = (name, avatar)
        except Exception:
            logger.debug("[orchestrator] WA self_profile 回填失败（忽略）", exc_info=True)

    def status(self) -> Dict[str, Any]:
        return {"type": "whatsapp_protocol", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class MessengerWebWorker:
    """薄监督一个 Messenger(网页模式) 账号：确保 Node/Playwright 微服务已恢复 + 读其状态。

    与 ``WhatsAppProtocolWorker`` 同构（连接由 Node 微服务保活，Python 侧只监督 + 路由出站）。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""

    def _base(self) -> str:
        from src.integrations.messenger_web_login import service_base_url
        return service_base_url(self.config)

    def _session_unhealthy(self) -> bool:
        """Node push 的会话健康登记显示该账号掉线/需重登 → 自动发送快速失败，
        免去注定失败的 20-30s DOM 尝试。仅拦**自动路径**（worker/编排器）；
        人工适配器路径不查此表——即使登记陈旧也不锁死人工操作。"""
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            return get_platform_session_health().is_unhealthy(
                "messenger", self.account_id)
        except Exception:
            return False

    async def start(self) -> None:
        from src.integrations.messenger_web_login import _post_json
        # 触发 Node 恢复所有持久化 profile（幂等）；Node 自身也会在开机时恢复
        await _post_json(f"{self._base()}/accounts/restore", {})
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src.integrations.messenger_web_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "messenger session unhealthy (needs manual re-login)"}
        # Node 侧现以 HTTP 502 + {ok:false, delivered:false} 如实上报「发送未确认」
        # （composer 未清空＝多半没发出）。_post_json 对非 2xx 抛错 → 归一为未送达，
        # 绝不把「没发出去」当成功（此前恒 delivered=True → 静默丢消息）。
        payload: Dict[str, Any] = {"jid": chat_key, "text": text}
        # 引用回复（P2 双面板融合 2026-08-13）：把被引用消息文本下发给 Node 做 DOM 引用
        # （Messenger web 无 wamid，只能按文本定位气泡）。此前 reply_to 收了却丢弃＝
        # 「接了线没接通」。Node 侧 best-effort + degrade-safe：定位不到就普通发送。
        if reply_to and reply_to.get("text"):
            payload["quoted"] = {
                "text": str(reply_to.get("text") or ""),
                "id": str(reply_to.get("id") or ""),
            }
        try:
            res = await _post_json(
                f"{self._base()}/accounts/{self.account_id}/send",
                payload,
            )
        except Exception as ex:  # noqa: BLE001
            # 附带边车响应体里的真实败因（reason_code：render_timeout/needs_accept/
            # e2ee_pin_prompt…）——裸 httpx 文本只有状态码，2026-08-15 173 事故
            # 排查为此绕了一整圈。实施86 域B-1（#49）：429/423 的 retry_after_ms
            # 结构化透出，autosend 据此改期而不是当场终局失败。
            from src.integrations.messenger_web_login import http_error_fields
            _f = http_error_fields(ex)
            _detail = str(_f["detail"])
            # B63-②（实施64 P1-4）：会话性败因（PIN/接受浮层/登出）→ 账号级
            # 健康登记（账号卡/横幅/看门狗点亮），不再逐条静默 500。
            try:
                from src.integrations.platform_session_health import (
                    note_send_auth_failure)
                note_send_auth_failure("messenger", self.account_id, _detail)
            except Exception:
                logger.debug("[messenger] 发送败因会话登记失败", exc_info=True)
            return {"delivered": False,
                    "error": f"messenger send failed: {_detail}",
                    "error_kind": str(_f["reason_code"]),
                    "retry_after_ms": int(_f["retry_after_ms"])}
        res = res or {}
        # 双重口径：ok 或 delivered 任一显式为 False，或 sent 显式为 False，都判未送达。
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        # 回读二次确认（P1）：verified=False 表示 Node 发出后回读没锚到我们的气泡
        # （不定态，为防重发仍按已送达）。留日志观测频率——若常态化说明 DOM 改版。
        if delivered and res.get("verified") is False:
            logger.info(
                "[messenger] 发送已确认（composer 清空）但回读未锚定气泡 "
                "account=%s chat=%s（按已送达处理）", self.account_id, chat_key)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or ""),
                "error": str(res.get("error") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        """出站媒体（图片/视频/音频/文件）——与 Telegram 的 send_media 对称。

        Node 与 Python 同机，直接把本地绝对路径 media_path 交给 Node 挂到 composer 发送
        （无需上传）；语音走 media_type=voice（作为音频文件发出，Messenger 内联可播放）。
        **本方法存在即被编排器 owns_media 判为 True** → 统一收件箱「图片/语音/视频/文件」
        按钮对 Messenger 点亮，send_media/send_voice 路由到此。
        """
        import os
        from src.integrations.messenger_web_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "messenger session unhealthy (needs manual re-login)"}
        abs_path = os.path.abspath(media_path) if media_path else ""
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send-media",
            {"jid": chat_key, "media_path": abs_path,
             "media_type": str(media_type or ""), "caption": caption},
            timeout=120.0,
        )
        res = res or {}
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or "")}

    async def stop(self) -> None:
        # 不登出（浏览器上下文由 Node 保活）；仅停止 Python 侧监督
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.messenger_web_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            for a in (res.get("accounts") or []):
                if str(a.get("account_id") or "") != self.account_id:
                    continue
                # 假健康修复（P0-2）：Node /accounts 现带 logged_in（轮询周期性
                # pageLoggedIn 复检）。status=authorized 但登录态已丢（cookie 失效
                # 停在登录页）→ 判不健康，让编排器进入 error/告警，而非带病待命。
                if a.get("logged_in") is False:
                    self.detail = "session listed but not logged in (cookie expired?)"
                    return False
                return True
            return False
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "messenger_web", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class ZaloPersonalWorker:
    """薄监督一个 Zalo 个人号（zca-js Node 边车）：确保边车已恢复登录 + 读状态 + 路由出站。

    与 ``MessengerWebWorker`` 同构（连接由 Node 微服务保活，Python 侧只监督 + 路由出站）。
    出站能力：文字 + 媒体（图片/语音/贴纸）——个人号比官方 OA（仅文字）能力更全，故
    实现 ``send_media`` 使编排器 ``owns_media("zalo",*)`` 判为 True，工作台媒体按钮点亮。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""

    def _base(self) -> str:
        from src.integrations.zalo_personal_login import service_base_url
        return service_base_url(self.config)

    def _session_unhealthy(self) -> bool:
        """Node push 的会话健康登记显示该账号掉线/需重登 → 自动路径快速失败，
        免去注定失败的出站尝试。仅拦自动路径（worker/编排器），人工路径不查此表。"""
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            return get_platform_session_health().is_unhealthy("zalo", self.account_id)
        except Exception:
            return False

    async def start(self) -> None:
        from src.integrations.zalo_personal_login import _post_json
        # 触发 Node 恢复所有持久化会话（幂等）；Node 自身也会在开机时恢复。
        await _post_json(f"{self._base()}/accounts/restore", {})
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None,
                   chat_type: Optional[str] = None) -> Dict[str, Any]:
        from src.integrations.zalo_personal_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "zalo session unhealthy (needs manual re-login)"}
        payload: Dict[str, Any] = {"thread_id": chat_key, "text": text}
        # 显式 chat_type 永远优先于 Node 群注册表（冷注册表 + 从未入站过的群）。
        if chat_type:
            payload["chat_type"] = str(chat_type)
        try:
            res = await _post_json(
                f"{self._base()}/accounts/{self.account_id}/send",
                payload,
            )
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"zalo send failed: {ex}"}
        res = res or {}
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or ""),
                "error": str(res.get("error") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "",
                         chat_type: Optional[str] = None) -> Dict[str, Any]:
        """出站媒体（图片/语音/贴纸）。Node 与 Python 同机，直接把本地绝对路径交给
        Node（zca-js 上传发送）。**本方法存在即被编排器 owns_media 判为 True**。"""
        import os
        from src.integrations.zalo_personal_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "zalo session unhealthy (needs manual re-login)"}
        abs_path = os.path.abspath(media_path) if media_path else ""
        payload: Dict[str, Any] = {
            "thread_id": chat_key, "media_path": abs_path,
            "media_type": str(media_type or ""), "caption": caption,
        }
        if chat_type:
            payload["chat_type"] = str(chat_type)
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send-media",
            payload,
            timeout=120.0,
        )
        res = res or {}
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or "")}

    async def stop(self) -> None:
        # 不登出（会话由 Node 保活）；仅停止 Python 侧监督。
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.zalo_personal_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            for a in (res.get("accounts") or []):
                if str(a.get("account_id") or "") != self.account_id:
                    continue
                if a.get("logged_in") is False:
                    self.detail = "session listed but not logged in (cookie expired?)"
                    return False
                return True
            return False
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "zalo_personal", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class InstagramWebWorker:
    """薄监督一个 Instagram(网页托管) 账号：确保 Playwright 边车已恢复 + 读状态 + 路由出站。

    与 ``MessengerWebWorker`` 同构（连接由 Node/Playwright 微服务保活，Python 侧只监督 +
    路由出站）。实现 ``send_media`` → 编排器 ``owns_media("instagram",*)`` 判 True，工作台
    对 IG 个人号点亮媒体按钮（IG DM 支持图片/视频）。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""

    def _base(self) -> str:
        from src.integrations.instagram_web_login import service_base_url
        return service_base_url(self.config)

    def _session_unhealthy(self) -> bool:
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            return get_platform_session_health().is_unhealthy(
                "instagram", self.account_id)
        except Exception:
            return False

    async def start(self) -> None:
        from src.integrations.instagram_web_login import _post_json
        await _post_json(f"{self._base()}/accounts/restore", {})
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src.integrations.instagram_web_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "instagram session unhealthy (needs manual re-login)"}
        try:
            res = await _post_json(
                f"{self._base()}/accounts/{self.account_id}/send",
                {"thread_id": chat_key, "text": text},
            )
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"instagram send failed: {ex}"}
        res = res or {}
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or ""),
                "error": str(res.get("error") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        import os
        from src.integrations.instagram_web_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "instagram session unhealthy (needs manual re-login)"}
        abs_path = os.path.abspath(media_path) if media_path else ""
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send-media",
            {"thread_id": chat_key, "media_path": abs_path,
             "media_type": str(media_type or ""), "caption": caption},
            timeout=120.0,
        )
        res = res or {}
        delivered = (res.get("ok", True) is not False
                     and res.get("delivered", True) is not False
                     and res.get("sent", True) is not False)
        return {"delivered": bool(delivered),
                "message_id": str(res.get("message_id") or "")}

    async def stop(self) -> None:
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.instagram_web_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            for a in (res.get("accounts") or []):
                if str(a.get("account_id") or "") != self.account_id:
                    continue
                if a.get("logged_in") is False:
                    self.detail = "session listed but not logged in (cookie expired?)"
                    return False
                return True
            return False
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "instagram_web", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class LineProtocolWorker:
    """保活一个 LINE(okline 协议) 连接：从落库的 tokens 拉起 client + 后台 Bot 收消息。

    进程内 worker（仿 ``TelegramProtocolWorker``）：okline 是同步(requests)库，收消息用
    ``Bot.run`` 阻塞长轮询 → 放后台 daemon 线程；入站经 ``emit_incoming`` 同步落库，
    自动回复经 ``run_coroutine_threadsafe`` 调度回主事件循环。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.tokens_path = str((account.get("meta") or {}).get("tokens_path") or "")
        self.client: Any = None
        self.bot: Any = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.state = "stopped"
        self.detail = ""
        # peer mid → (显示名, 头像 URL, 缓存时刻) 缓存（含 ("","")=已查过无，避免每条消息
        # 重复打 getContactsV2）。带 TTL（2026-08-16 统一实时刷新）：正结果 6h / 空结果
        # 10min——对方改名/换头像后，下条入站消息即触发重查自愈，而不是钉死到 worker 重启。
        self._peer_ident_cache: Dict[str, tuple] = {}
        # chat_key → 该会话末条**入站** msg id（LINE 的已读回执要带「读到哪条」）
        self._last_in_msg_id: Dict[str, str] = {}
        # 账号级 API 串行锁：okline 的 ``next_req_seq()`` 是裸 ``self._reqseq += 1``
        # （无锁的读-改-写）→ 两个线程并发调用可能拿到**同一个 reqSeq**。
        # 2026-07-31 真机实测的服务端去重键是 **(reqSeq, 消息内容)** 二者兼具：
        # 故撞号且内容相同（同一句自动回复并发投同一会话）才会被判重丢掉，内容不同
        # 时两条都会送达。也就是说这把锁修的是一个**窄但真实**的丢消息面，外加
        # 保住「reqSeq 单调」这个协议基本假设——别据此以为不加锁就会大面积丢消息。
        # 发文本/发媒体/已读都从线程池打同一个 client，故在这里串起来。长轮询
        # （Bot.run）不取 reqSeq、存量同步另建 client，都不参与竞争。
        # 每账号一把（不是模块级）：两个 LINE 号各有自己的 _reqseq，不该互相等。
        self._api_lock = threading.Lock()
        # B98（实施68 P1-15）入站活性观测：接收线程起点 + 末条入站时刻 + 累计入站数。
        # okline Bot.run(reconnect=True) 内部重连失败时线程可能不退（假活）——只看
        # 线程存活的 healthy() 判不出「登录在、收不到消息」。这几个戳让诊断包/看门狗
        # 能读出「接收线程活了多久、上次真收到消息是什么时候、总共收过几条」，把
        # 「LINE 只出不进」从无据可查变成可读数。纯观测，绝不改收发行为。
        self._recv_started_ts: float = 0.0
        self._last_inbound_ts: float = 0.0
        self._inbound_count: int = 0
        # SSE 重连轮回累计（worker 实例跨接收线程重启复用，故是累计值）：
        # 网关正常关流 ~10s 一轮，故它稳步上涨＝接收链在正常工作；配
        # last_inbound_ts 长期不动即「在轮回却收不到 op」＝多半 token 该续期。
        self._recv_cycles: int = 0
        # impl85 阶段1：拉取兜底（LINE「只出不进」修复，见 line_pull_sync 模块注释）。
        # _sse_inbound_ts 只在 SSE 路径刷新——兜底的「SSE 活着就休眠」判据必须与
        # _last_inbound_ts（任意路径入站，观测口径）分开，否则兜底自己拉到消息
        # 就会把自己休眠掉。
        self._sse_inbound_ts: float = 0.0
        self._pull_sync: Any = None
        self._pull_thread: Optional[threading.Thread] = None
        self._pull_stop = threading.Event()
        # A2（2026-09-03，钧机 3U298U 20:46-20:47）：签名桥被 kick_stuck_send 踢掉后
        # E2EE 密钥失效待重建的脏标。okline 的 E2EEManager 存的是**那个 Node 进程里
        # ltsm.wasm 的句柄**，桥换进程后句柄全成野值，而 is_ready() 只看 my_keys
        # 非空 → 仍返回 True，加封发送于是拿野句柄去算、条条失败到 worker 重启。
        # 懒重建：踢桥只打标（此刻桥还没起来，装不了），下一笔发送前才真重建。
        self._e2ee_dirty: bool = False
        self._e2ee_rebuilds: int = 0
        self._e2ee_retries: int = 0
        # #180 receiver 跨重启退避观测：idle|running|reconnecting（延后拉起中）|gave_up
        self._recv_state: str = "idle"
        self._recv_hold_until: float = 0.0
        self._recv_hold_timer: Optional[threading.Timer] = None
        self._recv_last_error: str = ""
        # 出站媒体能力**按开关绑定**，而不是写成普通方法——因为 owns_media() 的判据就是
        # ``hasattr(worker, "send_media")``。写成普通方法即等于「LINE 恒有发媒体能力」，
        # 会把自拍/相册/克隆语音/命理 K 线在 LINE 上一次性全部放开（逆向协议发媒体有
        # 账号风险，且开关关时应当维持「编排器判定 LINE 不支持媒体」的旧语义）。
        # ⚠ 别顺手「简化」成 `async def send_media`，那会静默改变生产行为。
        try:
            from src.integrations.line_media import resolve_line_media_cfg
            if resolve_line_media_cfg(config).get("outbound"):
                self.send_media = self._send_media_impl  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.debug("[line-worker] 出站媒体开关解析失败（按关处理）", exc_info=True)

    async def start(self) -> None:
        from src.integrations.line_protocol_login import (
            ensure_node_runtime, is_okline_available, tokens_path as _tp,
        )
        if not is_okline_available():
            raise RuntimeError("okline 未安装")
        # okline 每个请求都要 Node 桥算 X-Hmac：worker 走 OkLine.from_tokens_file()（不带
        # config），只能靠 LINE_NODE env 拿到运行时。必须在建 client 之前调，**为的是那个
        # 副作用**——把随包 Electron 钉进 env，否则没装系统 Node 的机器上这个号收发条条失败。
        # 刻意**不**在这里 raise：探不到 node 就拒绝启动等于让一次环境探测否决一个也许能跑的
        # worker，而真缺 node 时 okline 自己会抛 "Node.js not found (...)" —— 那条消息本就
        # 准确可照做，worker 照常进 failed 态带上它，比我们提前拦下更不容易误判。
        if not ensure_node_runtime(self.config):
            logger.warning("[line-worker] 未探到 Node 运行时，okline 的 X-Hmac 桥可能起不来 account=%s",
                           self.account_id)
        path = self.tokens_path or _tp(self.config, self.account_id)
        if not path or not os.path.exists(path):
            raise RuntimeError(f"缺少 LINE session tokens: {path}")
        from okline import OkLine
        self.client = OkLine.from_tokens_file(path)
        # HTTP 423 病理修复（2026-09-02，skuio 客户机实锤）：新 client 的 _reqseq
        # 从 0 起，而服务端去重键是 (reqSeq, 消息内容)——应用重启后媒体占位（内容
        # 彼此相同）撞上一轮进程的历史 reqSeq 被判重 → 返回旧 id → OBS 上传 423 →
        # 按失败撤回＝误删客户已收到的消息。启动即推进到时间基线新鲜区间（跨重启
        # 单调），侧车文件 <tokens>.reqseq 防时钟回拨。软失败：还有发送侧的 423
        # 判重重试兜底（见 send_line_media）。
        try:
            from src.integrations.line_media import bump_client_reqseq
            bump_client_reqseq(self.client, account_id=self.account_id,
                               floor_file=path + ".reqseq")
        except Exception:  # noqa: BLE001
            logger.warning("[line-worker] reqSeq 推进失败（重启后首条媒体可能撞判重）"
                           " account=%s", self.account_id, exc_info=True)
        self._loop = asyncio.get_running_loop()
        self._start_receiver_with_hold()
        self._start_pull_sync(path)
        self.state = "running"
        self.detail = ""
        # 存量名单同步刻意放在 running 之后且**不 await**：前端在线态就看编排器 state，
        # 先让账号亮起来；名单后台补齐，免得大号（上千好友）把 start 拖成分钟级。
        try:
            asyncio.create_task(self._sync_bootstrap(path))
        except Exception:  # noqa: BLE001
            logger.debug("[line-worker] 存量同步调度失败", exc_info=True)

    # ── 登录后存量同步（好友通讯录 / 群会话占位）─────────────────────────────────

    def _sync_cfg(self) -> Dict[str, Any]:
        line_cfg = ((self.config or {}).get("platform_login") or {}).get("line") or {}
        cfg = line_cfg.get("sync") if isinstance(line_cfg, dict) else None
        return cfg if isinstance(cfg, dict) else {}

    async def _sync_bootstrap(self, tokens_file: str) -> None:
        """登录后一次性存量同步：好友 → 通讯录，群 → 会话占位。

        ⚠ LINE 副设备协议**不下发历史消息**——官方 Chrome 版扩展登录后同样是空列表，
        只靠后续 ops 流拿新消息。所以这里能同步的存量只有「名单」：好友进
        ``protocol_contacts``（通讯录可见 + 入站自动补名），群进会话占位。别期待历史。

        okline 是同步 requests 库 → 整段丢线程池；且**另建一次性 client**，不与
        ``Bot.run`` 的长轮询共用连接/Session（跨线程并发复用同一实例是 requests 的雷区）。
        全程 best-effort：任何一步失败只记日志，不动已 running 的收发主职能。
        """
        cfg = self._sync_cfg()
        if not bool(cfg.get("enabled", True)):
            return
        try:
            await asyncio.to_thread(self._sync_bootstrap_blocking, tokens_file, cfg)
        except Exception:  # noqa: BLE001
            logger.debug("[line-worker] 存量同步失败 account=%s", self.account_id, exc_info=True)

    def _sync_bootstrap_blocking(self, tokens_file: str, cfg: Dict[str, Any]) -> None:
        from okline import OkLine
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None:
            logger.debug("[line-worker] inbox store 未就绪，跳过存量同步")
            return
        client = OkLine.from_tokens_file(tokens_file)
        try:
            contacts = self._fetch_contact_rows(client, int(cfg.get("max_contacts") or 1000))
            if contacts:
                n = store.upsert_protocol_contacts("line", self.account_id, contacts)
                logger.info("[line-worker] 通讯录同步 %d 条 account=%s", n, self.account_id)
            groups = self._fetch_group_rows(client, int(cfg.get("max_groups") or 200))
            rows: List[Dict[str, Any]] = list(groups)
            # 好友建会话占位只对小号做：这样工作台立刻能主动发起对话；大号（上千好友）
            # 全建会话会把列表灌成噪音，那种情况只留通讯录（新消息到了自然冒出会话）。
            seed_max = int(cfg.get("seed_chats_max", 200) or 0)
            if contacts and seed_max and len(contacts) <= seed_max:
                rows += [{"jid": r["jid"], "name": r.get("name") or ""} for r in contacts]
            elif contacts and seed_max:
                logger.info(
                    "[line-worker] 好友 %d 个 > seed_chats_max=%d：只同步通讯录，不建会话占位",
                    len(contacts), seed_max)
            if rows:
                n = store.upsert_protocol_chats("line", self.account_id, rows)
                logger.info("[line-worker] 会话占位同步 %d 条（其中群 %d）account=%s",
                            n, len(groups), self.account_id)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _fetch_contact_rows(self, client: Any, limit: int) -> List[Dict[str, Any]]:
        """好友名单 → ``upsert_protocol_contacts`` 的 rows，顺带预热 peer 身份缓存。

        ``getAllContactIds`` 实测直接回 mid 列表（也兼容被包一层 dict 的形态）；
        ``getContactsV2`` 由 okline 自动按 100 分块，mid 数量无上限。
        """
        rows: List[Dict[str, Any]] = []
        mids = self._extract_mids(client.get_all_contact_ids())
        if not mids:
            return rows
        if limit > 0:
            mids = mids[:limit]
        res = client.get_contacts(mids)
        entries = ((res or {}).get("contacts") or {}) if isinstance(res, dict) else {}
        for mid, entry in entries.items():
            name, avatar = self._contact_identity(entry)
            rows.append({"jid": str(mid), "name": name})
            # 预热缓存：首条消息进来时不必再打 getContactsV2，名字/头像当场就有
            if name or avatar:
                self._peer_ident_cache.setdefault(
                    str(mid), (name, avatar, time.time()))
        return rows

    def _fetch_group_rows(self, client: Any, limit: int) -> List[Dict[str, Any]]:
        """群名单 → 会话占位 rows（``is_group`` → chat_type=group，不刷 SLA）。"""
        raw = client.get_all_chat_mids()
        mids = list((raw or {}).get("memberChatMids") or []) if isinstance(raw, dict) else []
        mids = [m for m in mids if isinstance(m, str) and m]
        if not mids:
            return []
        if limit > 0:
            mids = mids[:limit]
        res = client.get_chats(mids)
        rows: List[Dict[str, Any]] = []
        for chat in (((res or {}).get("chats") or []) if isinstance(res, dict) else []):
            if not isinstance(chat, dict):
                continue
            ck = str(chat.get("chatMid") or chat.get("mid") or "")
            if not ck:
                continue
            rows.append({
                "jid": ck,
                "name": str(chat.get("chatName") or chat.get("name") or ""),
                "is_group": True,
            })
        return rows

    @staticmethod
    def _extract_mids(raw: Any) -> List[str]:
        if isinstance(raw, list):
            return [m for m in raw if isinstance(m, str) and m]
        if isinstance(raw, dict):
            for key in ("contactIds", "ids", "mids"):
                v = raw.get(key)
                if isinstance(v, list):
                    return [m for m in v if isinstance(m, str) and m]
        return []

    @staticmethod
    def _contact_identity(entry: Any) -> tuple:
        """从 ``getContactsV2`` 的一条 entry 抽 ``(显示名, 头像 URL)``，备注名优先。

        直接读 raw dict（字段名已由真机探针确认）——比绕 ``Contact`` 模型少一层
        「okline 换字段名就静默变空」的风险；``entry`` 形如 ``{contact: {...}}``。
        """
        from src.integrations.line_protocol_login import line_picture_url
        inner = entry
        if isinstance(entry, dict) and isinstance(entry.get("contact"), dict):
            inner = entry["contact"]
        if not isinstance(inner, dict):
            return "", ""
        name = str(inner.get("displayNameOverridden") or inner.get("displayName") or "").strip()
        return name, line_picture_url(str(inner.get("picturePath") or ""))

    def _ingest_inbound(
        self, message: Dict[str, Any], *, chat_key: str, is_group: bool,
        text: str, via: str = "sse",
    ) -> None:
        """入站消息统一投递口（SSE 实时路径与拉取兜底路径共用，best-effort 绝不抛）。

        impl85 阶段1：从 ``_on_msg`` 闭包提出来——拉取兜底（``line_pull_sync``）补拉
        到的消息必须与 SSE 路径走**同一条**落库/身份解析/媒体/自动回复链，否则两条
        路径行为分叉（比如兜底的消息没有头像/不触发自动回复）比收不到更难排查。
        """
        try:
            # B98 入站活性戳：收到任何一条 op（含群/非文本）即刷新——它证明的是
            # 「接收链还在真收东西」，与「私聊入站是否落库」是两个层次，放在最前。
            self._last_inbound_ts = time.time()
            self._inbound_count += 1
            if via == "sse":
                # 兜底的休眠判据只认 SSE 真送到的 op（见 __init__ 注释）
                self._sse_inbound_ts = self._last_inbound_ts
            if not chat_key:
                return
            from src.integrations.protocol_bridge import (
                emit_incoming, make_message, maybe_auto_reply,
            )
            # 私聊：按需向 LINE 拉发送者显示名+头像（getContactsV2 同一次调用免费取头像，
            # per-peer 缓存）——修「LINE 私聊只显示裸 mid + 无头像」。查的是**对方** mid，
            # 天然规避「误标成本账号名」。obs 直链稳定 → 直接落库 avatar_url 由前端渲染。
            peer_name, peer_avatar = ("", "") if is_group else self._resolve_peer_identity(chat_key)
            msg_id = str((message or {}).get("id") or "")
            if msg_id:
                # 记在下载之前：下载可能耗时甚至失败，但已读回执只需要这个 id。
                # 上界＝账号真实会话数（每条 ~40B）；仍设个天花板防异常膨胀。
                if len(self._last_in_msg_id) > 2000:
                    self._last_in_msg_id.clear()
                self._last_in_msg_id[chat_key] = msg_id
            _media_miss: Dict[str, Any] = {}
            media_type, media_ref = self._inbound_media(
                message, is_group=is_group, out=_media_miss)
            _miss_hint = ""
            if media_type and not media_ref:
                # A2（2026-09-03）：OBS 404 终局 → 「已过期，让客户重发」；
                # #172（2026-09-05）：404 不再等于过期——可重试档（拉取失败/对方还在
                # 上传/服务端转码中/加密媒体无钥）各给一句自解释的话，且**收到即预取
                # 失败进回填**：后台按退避再拉两次，拉到即原地回填媒体（见
                # ``_schedule_media_retry``）。文案单源 ``line_media.inbound_miss_text``。
                from src.integrations.line_media import inbound_miss_text
                _miss_hint = inbound_miss_text(media_type, _media_miss)
                if _miss_hint:
                    if not str(text).strip():
                        text = _miss_hint
                    else:
                        text = f"{text}\n{_miss_hint}"
                if _media_miss.get("retryable"):
                    self._schedule_media_retry(
                        message, chat_key=chat_key, is_group=is_group,
                        kind=media_type, hint=_miss_hint)
            if media_type == "sticker" and not media_ref and not str(text).strip():
                # 贴纸图没下来（CDN 变更/动图/网络）→ 退到贴纸自带文字。用 A 线同款
                # ``[表情] 语义`` 口径，inbound_enrich 的表情块解析可直接吃。
                from src.integrations.line_media import sticker_text_hint
                _hint = sticker_text_hint(message or {})
                text = f"[表情] {_hint}" if _hint else "[表情]"
            if not str(text).strip() and not media_type and (message or {}).get("chunks"):
                # 拉取路径解不开的 Letter-Sealed 消息：宁可占位也不静默丢——
                # 本修复的对象就是「消息没到工作台」。
                text = "[消息]"
            payload = make_message(
                platform="line", account_id=self.account_id, chat_key=chat_key,
                name=peer_name, avatar_url=peer_avatar, text=str(text),
                msg_id=msg_id,
                media_type=media_type, media_ref=media_ref,
                direction="in")
            if is_group:
                payload["chat_type"] = "group"
            emit_incoming(payload)
            if not is_group and self._loop is not None:
                asyncio.run_coroutine_threadsafe(maybe_auto_reply(payload), self._loop)
        except Exception:
            logger.debug("[line-worker] inbound 推送失败 via=%s", via, exc_info=True)

    # ── #180 receiver 跨重启退避 ─────────────────────────────────────────────

    def _recv_hold_remaining(self, now: Optional[float] = None) -> float:
        """按账号账本算本次 start() 后 receiver 该延后多少秒（0＝立刻拉）。"""
        ts = time.time() if now is None else now
        rec = _LINE_RECV_GIVEUP.get(self.account_id) or {}
        streak = int(rec.get("streak") or 0)
        if streak <= 0:
            return 0.0
        hold = line_recv_hold_sec(streak)
        elapsed = max(0.0, ts - float(rec.get("ts") or 0.0))
        return max(0.0, hold - elapsed)

    def _note_recv_giveup(self, reason: str) -> float:
        """receiver 放弃一次：账本 streak+1、记原因、算下次 hold、进观测。返回 hold 秒数。"""
        rec = _LINE_RECV_GIVEUP.setdefault(self.account_id, {"streak": 0, "ts": 0.0, "reason": ""})
        rec["streak"] = int(rec.get("streak") or 0) + 1
        rec["ts"] = time.time()
        rec["reason"] = str(reason or "")[:200]
        hold = line_recv_hold_sec(rec["streak"])
        self._recv_state = "gave_up"
        try:
            from src.integrations.line_media_stats import get_line_media_stats
            get_line_media_stats().record_receiver_giveup(
                self.account_id, reason=rec["reason"], hold_sec=hold)
        except Exception:
            pass
        # 根因单列观测（不猜修）：最后一次异常原文 + 连续放弃次数 + 本轮 hold。
        # 「为什么 gw operation/receive 连败」的证据链从这一行开始攒。
        logger.error(
            "[line-worker] receiver 放弃 #%d account=%s → 交编排器重启；下次拉起前 hold=%.0fs"
            "（期间 worker 仍 running、拉取兜底在收）last_error=%s",
            rec["streak"], self.account_id, hold, rec["reason"] or "-")
        return hold

    def _maybe_reset_recv_giveup(self) -> None:
        """receiver 稳跑 ≥ _LINE_RECV_STABLE_RESET_SEC → 清账本（连败已停，退避归零）。"""
        rec = _LINE_RECV_GIVEUP.get(self.account_id)
        if not rec or int(rec.get("streak") or 0) <= 0:
            return
        if self._recv_started_ts and (time.time() - self._recv_started_ts) >= _LINE_RECV_STABLE_RESET_SEC:
            logger.info("[line-worker] receiver 已稳跑 %.0fs，连败账本清零 account=%s（此前 streak=%d）",
                        time.time() - self._recv_started_ts, self.account_id, rec["streak"])
            _LINE_RECV_GIVEUP.pop(self.account_id, None)

    def _start_receiver_with_hold(self) -> None:
        """start() 用：连败账本要求 hold 则延后拉 receiver（状态 reconnecting），否则立刻拉。"""
        hold = self._recv_hold_remaining()
        if hold <= 0:
            self._start_receiver()
            return
        self._recv_state = "reconnecting"
        self._recv_hold_until = time.time() + hold
        rec = _LINE_RECV_GIVEUP.get(self.account_id) or {}
        logger.warning(
            "[line-worker] receiver 延后 %.0fs 再拉起 account=%s（连续放弃 %d 次，退避中；"
            "拉取兜底照常收消息，发送不受影响）", hold, self.account_id,
            int(rec.get("streak") or 0))

        def _fire() -> None:
            try:
                if self.state != "running" or self.client is None:
                    return  # worker 已被停掉：不要在尸体上起线程
                self._start_receiver()
            except Exception:
                logger.warning("[line-worker] 延后拉起 receiver 失败 account=%s",
                               self.account_id, exc_info=True)

        t = threading.Timer(hold, _fire)
        t.daemon = True
        t.name = f"line-recv-hold-{self.account_id[:8]}"
        self._recv_hold_timer = t
        t.start()

    def _start_receiver(self) -> None:
        """后台 daemon 线程跑 okline Bot：收到消息 → 落库 + 自动回复（best-effort）。"""
        from okline import Bot
        account_id = self.account_id
        client = self.client
        bot = Bot(client)
        self._recv_state = "running"
        self._recv_hold_until = 0.0
        self._recv_hold_timer = None

        @bot.on_message
        def _on_msg(ctx: Any) -> None:  # noqa: ANN401
            try:
                is_group = bool(ctx.is_group)
                chat_key = str((ctx.to if is_group else ctx.sender) or "")
                self._ingest_inbound(
                    dict(ctx.message or {}), chat_key=chat_key,
                    is_group=is_group, text=str(ctx.text or ""), via="sse")
            except Exception:
                logger.debug("[line-worker] inbound 推送失败", exc_info=True)

        self.bot = bot

        def _run() -> None:
            # 2026-08-27 06:11 宕机事故：okline stream(reconnect=True) 的内建重连
            # **零退避**——HMAC 签名桥崩坏后每秒上百轮「即抛即重试」，与日志轮转
            # 故障共振把实例打死。改为一击语义（reconnect=False）+ 本侧监督器
            # 指数退避；连败达阈值即放弃退出线程 → healthy() 变假 → 交编排器
            # 既有「error→退避→重启」接管。决策纯函数有门禁，勿改回内建重连。
            #
            # ⚠ ``clean=``（本轮有没有抛异常）必须如实传：网关的 SSE 本来就是
            # ~10s 发个 ping 后**正常关流**等客户端重连，把它当失败会连败放弃→
            # 编排器重启→无限循环，LINE 收消息全停（2026-08-27 07:19–09:30 实锤，
            # 详见 line_recv_supervisor 模块注释）。
            from src.integrations.line_recv_supervisor import LineRecvSupervisor
            sup = LineRecvSupervisor()
            try:
                while True:
                    started = time.time()
                    clean = True
                    try:
                        bot.run(reconnect=False)
                    except Exception as exc:  # noqa: BLE001
                        clean = False
                        self._recv_last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                        logger.warning(
                            "[line-worker] receiver 断开 account=%s streak=%d: %s",
                            account_id, sup.streak + 1,
                            str(exc)[:200])
                    lived = time.time() - started
                    # 正常轮回本身不打日志（~10s 一轮＝每天上万行），改成可读数：
                    # status 的 recv_cycles 配 last_inbound_ts 就能判「在正常轮回
                    # 却一条 op 都不来」——那是 token 该续期，不是重连节奏的事。
                    self._recv_cycles += 1
                    if self._recv_cycles % _LINE_RECV_CYCLE_LOG_EVERY == 0:
                        logger.info(
                            "[line-worker] receiver 轮回 %d 次 account=%s "
                            "（本轮 %.1fs clean=%s 入站累计 %d）",
                            self._recv_cycles, account_id, lived, clean,
                            self._inbound_count)
                    verdict = sup.on_attempt_end(lived, clean=clean)
                    if verdict["give_up"]:
                        logger.error(
                            "[line-worker] receiver 连败 %d 次，放弃并交编排器"
                            "重启 account=%s", verdict["streak"], account_id)
                        # #180：跨重启退避记账（编排器重启后 start() 按它延后拉 receiver）
                        self._note_recv_giveup(self._recv_last_error)
                        break
                    if verdict["streak"] == 0:
                        self._maybe_reset_recv_giveup()
                    if verdict["sleep_sec"] > 0:
                        time.sleep(verdict["sleep_sec"])
            except Exception:
                logger.debug("[line-worker] receiver 监督循环异常退出", exc_info=True)
            finally:
                # 循环真退出（放弃/异常）→ 清起点戳，让 healthy()/诊断看得出
                # 「接收线程已死」而非停在旧的 running 假象。
                self._recv_started_ts = 0.0

        self._recv_started_ts = time.time()
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    # ── 拉取兜底（impl85 阶段1：修「LINE 只出不进」，见 line_pull_sync 模块注释） ──

    def _emit_pulled(self, message: Dict[str, Any], *, chat_key: str, is_group: bool) -> None:
        """拉取兜底 → SSE 同一条投递链（text 直接取消息体，解密已在 pull 模块做完）。"""
        self._ingest_inbound(
            dict(message or {}), chat_key=chat_key, is_group=is_group,
            text=str((message or {}).get("text") or ""), via="pull")

    def _start_pull_sync(self, tokens_file: str) -> None:
        """独立 daemon 线程跑位点拉取兜底；启动失败只降级不阻断 worker。

        拉取线程用**独立的 OkLine 实例**（``_sync_bootstrap`` 同款纪律：requests
        Session 跨线程并发复用是雷区），凭据从会话文件懒加载——worker 主 client
        刷新 token 后回写文件，拉取侧重建 client 时自动跟上。
        """
        try:
            from src.integrations.line_pull_sync import (
                LinePullSync, resolve_line_pull_cfg,
            )
            cfg = resolve_line_pull_cfg(self.config)
            if not cfg.get("enabled", True):
                logger.info("[line-worker] pull_sync 已禁用 account=%s", self.account_id)
                return

            def _factory() -> Any:
                from okline import OkLine
                return OkLine.from_tokens_file(tokens_file)

            self._pull_stop = threading.Event()
            self._pull_sync = LinePullSync(
                client_factory=_factory, self_mid=self.account_id,
                state_path=str(tokens_file) + ".pullsync.json",
                emit=self._emit_pulled, cfg=cfg)
            interval = float(cfg.get("interval_sec") or 20.0)
            pull = self._pull_sync
            stop_evt = self._pull_stop

            def _run() -> None:
                relogin_reported = False
                while not stop_evt.wait(interval):
                    try:
                        res = pull.tick(sse_last_op_ts=self._sse_inbound_ts)
                        status = str(res.get("status") or "")
                        if status == "relogin_required" and not relogin_reported:
                            relogin_reported = True
                            self._report_relogin_required()
                        elif status in ("pulled", "unchanged", "scanned"):
                            relogin_reported = False
                    except Exception:
                        logger.debug("[line-worker] pull_sync tick 异常", exc_info=True)
                try:
                    pull.close()
                except Exception:
                    logger.debug("[line-worker] pull_sync 收尾关闭失败（忽略）",
                                 exc_info=True)

            self._pull_thread = threading.Thread(target=_run, daemon=True)
            self._pull_thread.start()
        except Exception:
            logger.debug("[line-worker] pull_sync 启动失败（忽略）", exc_info=True)

    def _report_relogin_required(self) -> None:
        """LINE 登录凭据过期 → 显式告警（客户报障点名「系统没弹提醒」的缺失面）。

        经 ``report_session_transition``（status=expired）接通既有告警链：
        坐席顶栏横幅 / ops「平台会话健康」卡 / watchdog 升级提醒 / webhook。
        """
        detail = "LINE 登录凭据已过期，请在平台管理里重新扫码登录（收消息已受影响）"
        self.detail = detail
        try:
            from src.integrations.platform_session_health import (
                report_session_transition,
            )
            report_session_transition(
                "line", self.account_id, "expired", detail=detail)
        except Exception:
            logger.debug("[line-worker] 会话过期上报失败（忽略）", exc_info=True)

    def _resolve_peer_identity(self, mid: str) -> tuple:
        """惰性解析 LINE 私聊发送者 ``(显示名, 头像 URL)``（备注名优先），per-peer 缓存、best-effort。

        在 okline 接收线程的 dispatch 内同步调用——此刻长轮询处于空闲（op 已收妥），故
        单发 ``getContactsV2`` 不与轮询争用连接，安全。查的是**对方 mid**（非本账号），故
        不会把「对方」误标成本账号名。取不到/异常 → ``("","")``（缓存以免逐条重打），交由
        no-clobber + 通讯录补名兜底。备注名 ``displayNameOverridden`` 优先（贴合账号主
        在客户端看到的称呼），否则回落公开 ``displayName``。**头像随同一次 get_contacts 免费
        取得**（零额外 API），``picturePath`` 经 ``line_picture_url`` 拼成稳定 obs 直链。
        """
        if not mid:
            return "", ""
        cached = self._peer_ident_cache.get(mid)
        if cached is not None:
            c_name, c_avatar = cached[0], cached[1]
            c_ts = float(cached[2]) if len(cached) > 2 else 0.0
            # TTL：正结果 6h / 空结果 10min（改名换头像随下条入站自愈；无 ts 的
            # legacy 条目按过期处理，一次重查后带上时间戳）
            ttl = 6 * 3600 if (c_name or c_avatar) else 600
            if c_ts and (time.time() - c_ts) < ttl:
                _record_line_identity("cache_hit")
                return c_name, c_avatar
        name, avatar = "", ""
        try:
            from okline import Contact
            from src.integrations.line_protocol_login import line_picture_url
            res = self.client.get_contacts([mid])
            entry = ((res or {}).get("contacts") or {}).get(mid) if isinstance(res, dict) else None
            if entry is not None:
                contact = Contact.from_dict(entry)
                name = str(contact.display_name_overridden or contact.display_name or "").strip()
                pic = str(getattr(contact, "picture_path", "") or "")
                if not pic and isinstance(entry, dict):   # 回落原始 dict（Contact 未暴露该字段时）
                    inner = entry.get("contact") if isinstance(entry.get("contact"), dict) else entry
                    pic = str((inner or {}).get("picturePath") or "")
                avatar = line_picture_url(pic)
        except Exception:
            logger.debug("[line-worker] peer 身份解析失败 mid=%s", mid, exc_info=True)
            name, avatar = "", ""
        self._peer_ident_cache[mid] = (name, avatar, time.time())
        _record_line_identity("resolved" if name else "miss")
        return name, avatar

    def _resolve_peer_name(self, mid: str) -> str:
        """向后兼容薄封装：仅取显示名（内部走 ``_resolve_peer_identity``，头像一并缓存）。"""
        return self._resolve_peer_identity(mid)[0]

    # _api_lock 获取限时（秒）：一笔悬死的请求（2026-09-02 WEXX7E：签名桥 Node 僵死）
    # 持锁不放时，后续所有调用原本会在锁上**无限**排队且零日志。限时后排队者会以
    # 明确异常出局（编排器/调用方按失败路径处置），而不是陪着一起悬死。
    # 90s 的依据：合法最慢的持锁操作是 20MB 媒体上传（okline 单请求 30s 超时 ×
    # 占位/上传/配文三步），限时必须显著大于它，否则大文件排队会被误杀。
    _API_LOCK_TIMEOUT_SEC = 90.0

    async def _api_call(self, fn: Any, *args: Any, **kw: Any) -> Any:
        """在线程池里串行调用 okline（同步库）。锁在**线程内**取，见 ``_api_lock`` 说明。"""
        def _locked() -> Any:
            if not self._api_lock.acquire(timeout=self._API_LOCK_TIMEOUT_SEC):
                raise RuntimeError(
                    f"line api 串行锁 {self._API_LOCK_TIMEOUT_SEC:.0f}s 未取得"
                    f"（前一笔请求疑似悬死）account={self.account_id}")
            try:
                return fn(*args, **kw)
            finally:
                self._api_lock.release()

        return await asyncio.to_thread(_locked)

    def kick_stuck_send(self) -> None:
        """恢复锤：编排器 ``send_media`` 超时兜底时调用（2026-09-02 WEXX7E 悬死）。

        该链路唯一**无界**的阻塞点是 okline 的签名桥：``LtsmBridge._readline()``
        对 Node 子进程的 stdout 裸 ``readline()`` 无超时，且 ``_call`` 全程持
        ``bridge._lock``——Node 僵而不死（写不出响应也不退出）时，持锁线程永久
        阻塞，同 client 的一切签名请求（发送、SSE 长轮询）跟着悬死，正是钧机
        「dispatch 后无下文 + SSE 停摆靠拉取兜底」的完整病象。

        处置：**绕开** ``bridge._lock`` 直接 terminate Node 进程——悬死线程的
        readline 立即见 EOF → ``HmacSignerError`` 抛出 → 各级锁释放；下一笔请求
        okline 的 ``_ensure_started`` 会自动重启桥，无需重启 worker。刻意不走
        公开口 ``bridge.close()``：它也要取 ``bridge._lock``，锁正被悬死线程
        持有时会二次死锁。访问 okline 私有属性是清醒的取舍，属性缺失时静默退出
        （版本漂移不该让恢复锤反过来抛错）。
        """
        try:
            signer = getattr(getattr(self.client, "transport", None), "_signer", None)
            proc = getattr(signer, "_proc", None)
            if proc is not None and proc.poll() is None:
                proc.terminate()
                # A2：桥换进程＝E2EE wasm 句柄全失效。此刻新桥还没起来（装密钥会把
                # 它顺带拉起、且此处仍在悬死上下文里），只打脏标，交
                # ``_ensure_e2ee_ready`` 在下一笔发送前懒重建。
                self._e2ee_dirty = True
                logger.warning(
                    "[line-worker] kick_stuck_send: 已终结疑似僵死的签名桥 Node 进程"
                    " pid=%s account=%s（下一笔请求自动重启桥；E2EE 密钥已标记待重建）",
                    getattr(proc, "pid", "?"), self.account_id)
            else:
                logger.info(
                    "[line-worker] kick_stuck_send: 签名桥无存活 Node 进程，无可踢"
                    " account=%s（悬死点不在签名桥）", self.account_id)
        except Exception:
            logger.debug("[line-worker] kick_stuck_send 失败 account=%s",
                         self.account_id, exc_info=True)

    # ── A2（2026-09-03）：Letter-Sealing 密钥自愈 ──────────────────────────────
    # 详见 src/integrations/line_e2ee_recovery.py 模块头（含 3U298U 取证与机制）。

    def _tokens_file(self) -> str:
        """本号 session 文件路径（E2EE 导出块的所在）。"""
        if self.tokens_path:
            return self.tokens_path
        try:
            from src.integrations.line_protocol_login import tokens_path as _tp
            return str(_tp(self.config, self.account_id) or "")
        except Exception:
            return ""

    def _rebuild_e2ee_blocking(self, why: str) -> Dict[str, Any]:
        """同步重建（跑在 ``_api_call`` 的线程里；okline 是同步库）。"""
        from src.integrations.line_e2ee_recovery import rebuild_e2ee_from_session
        res = rebuild_e2ee_from_session(
            self.client, self._tokens_file(), account_id=self.account_id)
        self._e2ee_rebuilds += 1
        logger.info(
            "[line-worker] E2EE 密钥重建 why=%s acct=%s ok=%s keys=%s reason=%s",
            why, self.account_id, res.get("ok"), res.get("keys"),
            res.get("reason") or "-")
        return res

    async def _ensure_e2ee_ready(self) -> None:
        """懒重建：脏标在场才做，且**先清标再重建**（失败不留死循环重试）。

        重建不成也照常返回——让真实发送错误自己浮出来，别在这儿替它判死
        （没扫码登录过 E2EE 的号 reason=no_session_keys，纯文本会话本就不需要）。
        """
        if not self._e2ee_dirty or self.client is None:
            return
        self._e2ee_dirty = False
        try:
            await self._api_call(self._rebuild_e2ee_blocking, "bridge_restart")
        except Exception:
            logger.debug("[line-worker] E2EE 懒重建异常 acct=%s",
                         self.account_id, exc_info=True)

    async def _send_with_e2ee_retry(self, what: str, fn: Any, *args: Any) -> Any:
        """发送 + 「密钥类报错 → 强制重握手 → 重试一次」。

        只重试一次是刻意的：对端真关了 Letter Sealing / 账号被降级时重握手救不回来，
        无限重试只会把节流打满（0827 的零退避重连事故是同一个教训）。
        """
        from src.integrations.line_e2ee_recovery import is_e2ee_key_error
        await self._ensure_e2ee_ready()
        try:
            return await self._api_call(fn, *args)
        except Exception as exc:
            if not is_e2ee_key_error(exc):
                raise
            self._e2ee_retries += 1
            logger.warning(
                "[line-worker] %s 撞 E2EE 密钥错（%s）→ 强制重握手后重试一次 acct=%s",
                what, str(exc)[:120], self.account_id)
            await self._api_call(self._rebuild_e2ee_blocking, "key_error")
            return await self._api_call(fn, *args)

    async def mark_read(self, chat_key: str) -> bool:
        """把会话标记已读（``sendChatChecked``），拟人「先看后回」。

        LINE 的已读要带「读到哪条」→ 用 ``_on_msg`` 记下的该会话末条入站 msg id；
        没记到（重启后该会话还没来过消息）就返回 False，不猜。

        调用时机是**投递前一刻**（``build_autosend_mark_read_cb``），所以不会造成
        LINE 文化里最忌讳的「已讀不回」——已读之后紧跟着就是回复。
        """
        if self.client is None:
            return False
        mid = self._last_in_msg_id.get(str(chat_key or ""))
        if not mid:
            return False
        try:
            await self._api_call(
                self.client.send_chat_checked, str(chat_key), str(mid))
            return True
        except Exception:
            logger.debug("[line-worker] 已读回执失败 chat=%s", chat_key, exc_info=True)
            return False

    # ── 媒体（2026-07-31 补齐：此前 LINE 号只能收发文字）─────────────────────────

    def _inbound_media(self, message: Dict[str, Any], *, is_group: bool,
                       out: Optional[Dict[str, Any]] = None) -> tuple:
        """入站媒体下载 → ``(媒体大类, /static URL)``；非媒体/关闭/失败均软回落。

        ``out``（A2）：透传给 ``download_line_media`` 的 miss 细因出参——调用方靠
        ``out["expired"]`` 把 OBS 404 这种**终局**渲染成「已过期」提示。

        **群聊默认不下载**：群与私聊共用同一条 okline 接收线程，热闹的群会把下载耗时
        叠到私聊 AI 回复的延迟上——而私聊才是 AI 与营收所在。要看群里的图，开
        ``platform_login.line.media.groups``。

        （impl85：形参从 okline ctx 改为裸消息 dict——``download_line_media`` 本就
        只吃消息 dict，SSE 与拉取兜底两条路径共用本方法。）
        """
        try:
            from src.integrations.line_media import (
                download_line_media, resolve_line_media_cfg,
            )
            mcfg = resolve_line_media_cfg(self.config)
            if is_group and not mcfg.get("groups", False):
                logger.info(
                    "[line-worker] 入站媒体跳过（群聊未开 platform_login.line.media.groups）"
                    " acct=%s", self.account_id)
                return "", ""
            return download_line_media(
                self.client, message or {}, self.account_id, cfg=mcfg, out=out)
        except Exception:
            logger.debug("[line-worker] 入站媒体处理失败（回落纯文本）", exc_info=True)
            return "", ""

    #: #172 入站媒体回填退避（秒）：首次 20s（对方视频转码/上传收尾的典型窗），
    #: 再 120s 兜一次；两次都空手＝留给坐席手动「点击重试」（fetch-media 路由）。
    _MEDIA_RETRY_DELAYS: tuple = (20.0, 120.0)

    def _schedule_media_retry(
        self, message: Dict[str, Any], *, chat_key: str, is_group: bool,
        kind: str, hint: str = "", attempt: int = 0,
    ) -> None:
        """入站媒体首拉失败（可重试档）→ 后台定时再拉，拉到即回填落库行（best-effort）。

        「收到即预取，失败进回填队列」的最小实现：不依赖坐席点开。每条消息至多
        ``len(_MEDIA_RETRY_DELAYS)`` 次；daemon Timer，进程退出即散，绝不阻塞
        接收线程。回填走 store 既有的幂等回写（``update_message_media`` 只填空行；
        文案只在仍是本轮写的 miss 提示时才换成标准占位，绝不踩客户原话）。
        """
        delays = self._MEDIA_RETRY_DELAYS
        if attempt >= len(delays):
            return
        msg_id = str((message or {}).get("id") or "")
        if not msg_id or not chat_key:
            return
        try:
            import threading
            snapshot = dict(message or {})

            def _job() -> None:
                self._retry_media_job(
                    snapshot, chat_key=chat_key, is_group=is_group, kind=kind,
                    hint=hint, attempt=attempt)

            t = threading.Timer(float(delays[attempt]), _job)
            t.daemon = True
            t.name = f"line-media-retry-{msg_id}-{attempt + 1}"
            t.start()
            logger.info("[line-worker] 入站媒体回填已排程 kind=%s id=%s attempt=%d in=%ss",
                        kind, msg_id, attempt + 1, delays[attempt])
        except Exception:
            logger.debug("[line-worker] 媒体回填排程失败", exc_info=True)

    def _retry_media_job(
        self, message: Dict[str, Any], *, chat_key: str, is_group: bool,
        kind: str, hint: str, attempt: int,
    ) -> None:
        msg_id = str((message or {}).get("id") or "")
        out: Dict[str, Any] = {}
        try:
            media_type, media_ref = self._inbound_media(message, is_group=is_group, out=out)
        except Exception:  # noqa: BLE001
            media_type, media_ref = "", ""
        if not media_ref:
            logger.info("[line-worker] 入站媒体回填仍未取到 kind=%s id=%s attempt=%d"
                        " reason=%s http=%s obs_status=%s",
                        kind, msg_id, attempt + 1, out.get("reason"),
                        out.get("http_status"), out.get("obs_status") or "-")
            if out.get("retryable"):
                self._schedule_media_retry(
                    message, chat_key=chat_key, is_group=is_group, kind=kind,
                    hint=hint, attempt=attempt + 1)
            return
        try:
            from src.inbox.normalizer import conv_id
            from src.integrations.protocol_bridge import get_inbox_store, media_placeholder
            store = get_inbox_store()
            if store is None:
                logger.info("[line-worker] 入站媒体回填成功但 store 不可用 id=%s", msg_id)
                return
            cid = conv_id("line", self.account_id, chat_key)
            filled = bool(store.update_message_media(
                cid, media_type=media_type or kind, media_ref=media_ref,
                platform_msg_id=msg_id))
            logger.info("[line-worker] 入站媒体回填成功 kind=%s id=%s attempt=%d ref=%s"
                        " row_updated=%s", kind, msg_id, attempt + 1, media_ref, filled)
            if filled and hint:
                # 只把本轮写下的 miss 提示换成标准占位；行上若是别的文本一律不动
                row = store.get_message(f"{cid}:{msg_id}") or {}
                if str(row.get("text") or "").strip() == hint.strip():
                    store.update_message_text(
                        cid, text=media_placeholder(media_type or kind),
                        media_ref=media_ref, only_if_empty=False)
            if filled and self._loop is not None and (media_type or kind) in (
                    "voice", "audio", "image", "video"):
                # 转写/识别回填（与 fetch-media 路由同口径，best-effort）：救活的语音
                # 坐席要能读、AI 要能看——只回填播放器等于修了一半。
                self._enrich_backfilled_media(cid, media_type or kind, media_ref)
        except Exception:
            logger.debug("[line-worker] 入站媒体回填落库失败 id=%s", msg_id, exc_info=True)

    def _enrich_backfilled_media(self, cid: str, media_type: str, media_ref: str) -> None:
        async def _run() -> None:
            try:
                from src.inbox.media_enrich import enrich_inbound_media_text
                from src.integrations.protocol_bridge import get_inbox_store, media_placeholder
                etext, _ = await asyncio.wait_for(
                    enrich_inbound_media_text(
                        media_type=media_type, media_ref=media_ref, config=self.config),
                    timeout=30.0)
                etext = str(etext or "").strip()
                store = get_inbox_store()
                if store is not None and etext and etext != media_placeholder(media_type):
                    store.update_message_text(cid, text=etext, media_ref=media_ref)
            except Exception:
                logger.debug("[line-worker] 回填媒体识别失败 ref=%s", media_ref, exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._loop)
        except Exception:
            logger.debug("[line-worker] 回填媒体识别调度失败", exc_info=True)

    async def _send_media_impl(
        self, chat_key: str, *, media_path: str, media_type: str = "",
        caption: str = "",
    ) -> Dict[str, Any]:
        """发媒体（okline 同步库 → 丢线程，与 ``send`` 同范式）。

        只在 ``platform_login.line.media.outbound`` 打开时才被绑成 ``send_media``
        —— 见 ``__init__`` 里那段说明，别把它改成普通方法。
        """
        if self.client is None:
            raise RuntimeError("line client 未连接")
        from src.integrations.line_media import resolve_line_media_cfg, send_line_media
        # 三段观测之「开始执行」（2026-09-02 WEXX7E）：编排器 dispatch 日志之后、
        # okline 真调用之前的存在证明——缺这行=协程没跑起来（loop/派发问题），
        # 有这行没结果=卡在 okline 调用链（锁/签名桥/网络）。
        _size = -1
        try:
            _size = os.path.getsize(media_path)
        except OSError:
            pass
        logger.info("[line-worker] send_media begin acct=%s chat=%s type=%s size=%s",
                    self.account_id, chat_key, media_type, _size)
        # A2：媒体的「占位消息」走的也是 sendMessage，Letter-Sealing 会话里同样要
        # 加封 → 桥重启后同样会撞野句柄（3U298U 20:46 那笔就是 send_media）。
        await self._ensure_e2ee_ready()
        # 整段（占位→上传→配文）持锁：中途被另一次发送插入会打乱 reqSeq，
        # 也会让「占位」与「上传」之间夹进别人的请求。
        return await self._api_call(
            send_line_media, self.client, chat_key,
            media_path=media_path, media_type=media_type, caption=caption,
            cfg=resolve_line_media_cfg(self.config),
        )

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发文本；带 ``reply_to`` 时走 LINE 原生引用回复（``related_message_id``）。

        引用**失败必须回落普通发送**：被引用的消息可能太旧/已撤回/不在本会话，
        引用只是气泡上的装饰，不该因为它整条消息发不出去。
        """
        if self.client is None:
            raise RuntimeError("line client 未连接")
        ref_id = str((reply_to or {}).get("id") or "")
        res = None
        if ref_id:
            try:
                res = await self._send_with_e2ee_retry(
                    "reply_text", self.client.reply_text, chat_key, text, ref_id)
            except Exception:
                logger.debug("[line-worker] 引用回复失败，回落普通发送 ref=%s",
                             ref_id, exc_info=True)
                res = None
        if res is None:
            res = await self._send_with_e2ee_retry(
                "send_text", self.client.send_text, chat_key, text)
        mid = ""
        try:
            if isinstance(res, dict):
                mid = str(res.get("id") or "")
        except Exception:
            mid = ""
        return {"delivered": True, "message_id": mid}

    async def send_line_sticker(
        self, chat_key: str, package_id: str, sticker_id: str,
    ) -> Dict[str, Any]:
        """发 LINE 官方商店贴纸（2026-08-17 表情包主线）：okline 原生
        ``send_sticker(to, packageId, stickerId)``——纯 ID 消息，不经 OBS 上传，
        不受 ``platform_login.line.media.outbound`` 媒体开关约束（它管的是
        自有文件上传链路，商店贴纸与发文本同级）。

        镜像回写由调用方（sticker_routes）负责——本方法与 ``send`` 同层，只管投递。
        """
        if self.client is None:
            raise RuntimeError("line client 未连接")
        res = await self._send_with_e2ee_retry(
            "send_sticker", self.client.send_sticker, str(chat_key),
            str(package_id), str(sticker_id))
        mid = ""
        try:
            if isinstance(res, dict):
                mid = str(res.get("id") or "")
        except Exception:
            mid = ""
        return {"delivered": True, "message_id": mid}

    async def delete_messages(self, chat_key: str, message_ids: List[str],
                              *, revoke: bool = True) -> Dict[str, Any]:
        """撤回若干条自己发出的消息（2026-08-17）：okline ``unsend_message``。

        LINE 只有「unsend＝对所有人撤回」一种语义（约 24h 时限，超时服务端拒绝）
        —— ``revoke`` 形参仅为编排器统一签名，False 也走 unsend。逐条调用
        （okline 无批量口），单条失败不阻断其余。
        """
        if self.client is None:
            raise RuntimeError("line client 未连接")
        ok_n = 0
        last_err = ""
        for mid in (message_ids or []):
            mid = str(mid or "").strip()
            if not mid:
                continue
            try:
                await self._api_call(self.client.unsend_message, mid)
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
                logger.debug("[line-worker] unsend 失败 msg=%s", mid, exc_info=True)
        if ok_n <= 0:
            return {"ok": False, "reason": last_err or "unsend_failed"}
        return {"ok": True, "deleted": ok_n}

    async def stop(self) -> None:
        self.state = "stopped"
        try:
            self._pull_stop.set()
        except Exception:
            logger.debug("[line-worker] pull_stop 置位失败（忽略）", exc_info=True)
        try:
            t = self._recv_hold_timer
            if t is not None:
                t.cancel()
            self._recv_hold_timer = None
            if self._recv_state == "reconnecting":
                self._recv_state = "idle"
        except Exception:
            pass
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass

    def _recv_holding(self) -> bool:
        """receiver 正处于 #180 的延后拉起窗（Timer 在走、还没到点）。"""
        t = self._recv_hold_timer
        return bool(self._recv_state == "reconnecting" and t is not None and t.is_alive()
                    and time.time() < self._recv_hold_until + 5.0)

    async def healthy(self) -> bool:
        if self.client is None:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        # 延后拉起期不算不健康：client 在、拉取兜底在收；判不健康会让编排器再重启一轮，
        # 把退避变成风暴——那正是 #180 要停掉的循环。
        return self._recv_holding()

    def status(self) -> Dict[str, Any]:
        # B98：入站活性快照进 status——诊断/看门狗据此判「登录在、收不到」
        # （recv_started 有值但 last_inbound 长期不动 = 接收链假活）。
        rec = _LINE_RECV_GIVEUP.get(self.account_id) or {}
        out = {"type": "line_protocol", "account_id": self.account_id,
               "state": self.state, "detail": self.detail,
               "recv_started_ts": round(self._recv_started_ts, 1),
               "last_inbound_ts": round(self._last_inbound_ts, 1),
               "inbound_count": self._inbound_count,
               "recv_cycles": self._recv_cycles,
               "thread_alive": bool(self._thread is not None
                                    and self._thread.is_alive()),
               # #180：receiver 连败/退避观测（reconnecting=延后拉起中）
               "recv_state": self._recv_state,
               "recv_hold_until": round(self._recv_hold_until, 1),
               "recv_giveup_streak": int(rec.get("streak") or 0),
               "recv_last_error": self._recv_last_error}
        # impl85 阶段1：拉取兜底观测（pulled_total>0 = SSE 流确实在漏消息）
        if self._pull_sync is not None:
            try:
                out["pull_sync"] = self._pull_sync.stats()
                out["pull_thread_alive"] = bool(
                    self._pull_thread is not None and self._pull_thread.is_alive())
            except Exception:
                logger.debug("[line-worker] pull_sync 观测读取失败（忽略）",
                             exc_info=True)
        return out


_orchestrator: Optional[AccountOrchestrator] = None


def get_orchestrator(config: Optional[Dict[str, Any]] = None) -> AccountOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AccountOrchestrator(config=config or {})
    elif config:
        # 配置热重载后 config_manager.config 是**新 dict 对象**，单例里存的还是
        # 启动时的旧引用 → 发送护栏（send_gate cap 等运营开关）永远读旧值、
        # 热调参形同虚设。调用方每次都传"当前"配置，这里跟着刷新引用。
        _orchestrator._config = config
    return _orchestrator


def get_orchestrator_if_running() -> Optional[AccountOrchestrator]:
    """返回**已创建**的编排器单例；不存在则 None，**绝不创建**。

    供只读取数（头像/身份解析按 account_id 取 worker client）——避免以空配置误建单例
    而遮蔽后续 app 以真实 config 建的实例。
    """
    return _orchestrator


def orchestrator_enabled(config: Dict[str, Any]) -> bool:
    # 三态（单一事实源见 platform_login.resolve_login_switch）：显式配置优先（含
    # false）；桌面升级安装未写过该键时默认开——否则扫上的号一重启就掉线（种子注释
    # 警告的「功能只交付一半」）。服务器部署无 AITR_DESKTOP_MODE → 保持默认关，零行为
    # 变化。与 line/wa/messenger 登录开关同机制，补齐 orchestrator 这最后一环。
    from src.integrations.platform_login import resolve_login_switch
    return resolve_login_switch(config, "platform_login.orchestrator_enabled")
