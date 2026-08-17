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
            m.state = "error"
            m.last_error = "no worker factory"
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
            return True
        except Exception as ex:  # noqa: BLE001
            m.state = "error"
            m.last_error = str(ex)
            m.restarts += 1
            self._schedule_backoff(m)
            logger.debug("[orchestrator] 启动账号失败 %s", key, exc_info=True)
            return False

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

    async def mark_read(self, platform: str, account_id: str, chat_key: str) -> bool:
        """把该会话标记已读（向平台发「已读」回执，拟人「先看后回」）。

        best-effort：无运行中 worker / worker 不支持 mark_read（WA/LINE/Messenger 暂无）
        / 平台异常 → 一律 False 且绝不抛——已读只是拟人增强，失败不得阻断投递主流程。
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
        worker / worker 不支持（WA/LINE/Messenger worker 暂无）/ 异常 → False 且绝不抛。
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

    async def send_media(
        self, platform: str, account_id: str, chat_key: str, *,
        media_path: str, media_url: str, media_type: str, caption: str = "",
        inbox_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """经 worker 发送媒体，并把出站媒体消息回写收件箱线程（media_ref 用 /static URL）。

        ``inbox_text``：**仅**回写给收件箱（坐席台可读）的文本，不发给客户；为 None 时回落
        ``caption``（向后兼容）。语音出站用它把「念了什么」带进会话视图——坐席不播放也能读，
        客户那边仍是纯语音（caption 不变）。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——富媒体与文本同守。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry,
            chat_key=str(chat_key or ""))
        if _blk:
            logger.warning("[orchestrator] 媒体发送被护栏拦截 %s:%s (%s)",
                           platform, account_id, _reason)
            return {"delivered": False, "blocked": _reason}
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
        _kw: Dict[str, Any] = dict(
            media_path=_send_path, media_type=media_type, caption=caption)
        try:
            import inspect
            if "media_url" in inspect.signature(_sm).parameters:
                _kw["media_url"] = media_url
        except (ValueError, TypeError):
            pass
        try:
            res = await _sm(chat_key, **_kw)
        finally:
            # 无论成功失败都清理微扰临时副本（原图不受影响）
            try:
                from src.integrations.shared.media_dedup import cleanup_temp
                cleanup_temp(_send_path, _dedup_temp)
            except Exception:
                logger.debug("[orchestrator] 微扰临时文件清理失败", exc_info=True)
        # P0-4：带回平台消息 id(wamid)，让出站回写与 worker 的 fromMe 回显同键去重
        _mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            _itext = inbox_text if inbox_text is not None else caption
            emit_incoming(make_message(
                platform=platform, account_id=account_id, chat_key=chat_key,
                text=_itext, direction="out", msg_id=_mid,
                media_type=media_type, media_ref=media_url,
            ))
        except Exception:
            logger.debug("[orchestrator] 出站媒体回写收件箱失败", exc_info=True)
        return res if isinstance(res, dict) else {"delivered": True}

    async def send(
        self, platform: str, account_id: str, chat_key: str, text: str,
        *, reply_to: Optional[Dict[str, Any]] = None,
        mentions: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """经受管 worker 发送，并把出站消息回写收件箱线程。

        P4-5B：``reply_to`` 携带原生引用回复上下文——若 worker 的 send 支持该 kwarg
        （WhatsApp 协议 worker）则透传发原生引用；否则退回普通发送（TypeError 兜底）。
        引用摘要一并写进出站消息的 source.reply_to，使本端气泡也渲染引用条。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——所有经编排器的
        # 外发（主动问候/唤醒/关怀/接管）都从这里走，旁路发送不再绕过急停与反封号。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry,
            chat_key=str(chat_key or ""))
        if _blk:
            logger.warning("[orchestrator] 发送被护栏拦截 %s:%s (%s)",
                           platform, account_id, _reason)
            return {"delivered": False, "blocked": _reason}
        m = self._managed.get(account_key(platform, account_id))
        if not (m is not None and m.state == "running"
                and m.worker is not None and hasattr(m.worker, "send")):
            raise RuntimeError(f"无可用的运行中 worker: {platform}:{account_id}")
        # P4-5B reply_to + P4-11 mentions：仅协议 worker 支持这些 kwarg；用逐级降级
        # 探测其签名（先全带、再退 reply_to、最后裸发），非协议 worker 一律安全回落。
        _kw: Dict[str, Any] = {}
        if reply_to:
            _kw["reply_to"] = reply_to
        if mentions:
            _kw["mentions"] = mentions
        if _kw:
            try:
                res = await m.worker.send(chat_key, text, **_kw)
            except TypeError:
                if reply_to:
                    try:
                        res = await m.worker.send(chat_key, text, reply_to=reply_to)
                    except TypeError:
                        res = await m.worker.send(chat_key, text)
                else:
                    res = await m.worker.send(chat_key, text)
        else:
            res = await m.worker.send(chat_key, text)
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
                if reply_to and (reply_to.get("id") or reply_to.get("text")):
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
        if (tg_enabled(config) and is_pyrogram_available()
                and resolve_credentials(config) is not None
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
        前端出站气泡即显示蓝色双勾（best-effort，不影响主消息流）。"""
        try:
            from pyrogram.handlers import RawUpdateHandler
            from pyrogram import raw

            account_id = self.account_id

            async def _on_raw(_client: Any, update: Any, _users: Any, _chats: Any) -> None:  # noqa: ANN401
                try:
                    from src.integrations.protocol_bridge import (
                        report_read_upto, tg_peer_to_chat_key,
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
                except Exception:
                    logger.debug("[tg-worker] 已读回执处理失败", exc_info=True)

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

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发文本；带 ``reply_to`` 时走 Telegram 原生引用回复（``reply_to_message_id``）。

        引用**失败必须回落普通发送**——被引用的消息可能太旧/已撤回/不在本会话，引用只是
        气泡装饰，不该因它整条消息发不出去（与 WhatsApp/LINE worker 的引用哲学一致）。
        """
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        _rid: Optional[int] = None
        _ref = str((reply_to or {}).get("id") or "").strip()
        if _ref:
            try:
                _rid = int(_ref)
            except (TypeError, ValueError):
                _rid = None
        msg = None
        if _rid is not None:
            try:
                msg = await self.client.send_message(
                    target, text, reply_to_message_id=_rid)
            except Exception:
                logger.debug("[tg-worker] 引用回复失败，回落普通发送 ref=%s",
                             _rid, exc_info=True)
                msg = None
        if msg is None:
            msg = await self.client.send_message(target, text)
        return {"delivered": True, "message_id": str(getattr(msg, "id", "") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
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
            msg = await self.client.send_voice(target, media_path, caption=caption)
        elif kind == "video":
            msg = await self.client.send_video(target, media_path, caption=caption)
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
        if reply_to and reply_to.get("id"):
            payload["quoted"] = {
                "id": str(reply_to.get("id") or ""),
                "from_me": bool(reply_to.get("from_me")),
                "participant": str(reply_to.get("participant") or ""),
                "text": str(reply_to.get("text") or ""),
            }
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send", payload,
        )
        return {"delivered": bool((res or {}).get("ok", True)),
                "message_id": str((res or {}).get("message_id") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        from src.integrations.whatsapp_baileys_login import _post_json
        if self._session_unhealthy():
            return {"delivered": False, "blocked": "session_unhealthy",
                    "error": "whatsapp session unhealthy (logged out / reconnect gave up)"}
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
        try:
            res = await _post_json(
                f"{self._base()}/accounts/{self.account_id}/send",
                {"jid": chat_key, "text": text},
            )
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"messenger send failed: {ex}"}
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
        # peer mid → (显示名, 头像 URL) 缓存（含 ("","")=已查过无，避免每条消息重复打 getContactsV2）
        self._peer_ident_cache: Dict[str, tuple] = {}

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
        self._loop = asyncio.get_running_loop()
        self._start_receiver()
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
                self._peer_ident_cache.setdefault(str(mid), (name, avatar))
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

    def _start_receiver(self) -> None:
        """后台 daemon 线程跑 okline Bot：收到消息 → 落库 + 自动回复（best-effort）。"""
        from okline import Bot
        from src.integrations.protocol_bridge import (
            emit_incoming, make_message, maybe_auto_reply,
        )
        account_id = self.account_id
        client = self.client
        loop = self._loop
        bot = Bot(client)

        @bot.on_message
        def _on_msg(ctx: Any) -> None:  # noqa: ANN401
            try:
                text = ctx.text or ""
                is_group = bool(ctx.is_group)
                chat_key = str((ctx.to if is_group else ctx.sender) or "")
                if not chat_key:
                    return
                # 私聊：按需向 LINE 拉发送者显示名+头像（getContactsV2 同一次调用免费取头像，
                # per-peer 缓存）——修「LINE 私聊只显示裸 mid + 无头像」。查的是**对方** mid，
                # 天然规避「误标成本账号名」。obs 直链稳定 → 直接落库 avatar_url 由前端渲染。
                peer_name, peer_avatar = ("", "") if is_group else self._resolve_peer_identity(chat_key)
                payload = make_message(
                    platform="line", account_id=account_id, chat_key=chat_key,
                    name=peer_name, avatar_url=peer_avatar, text=str(text),
                    msg_id=str((ctx.message or {}).get("id") or ""),
                    direction="in")
                if is_group:
                    payload["chat_type"] = "group"
                emit_incoming(payload)
                if not is_group and loop is not None:
                    asyncio.run_coroutine_threadsafe(maybe_auto_reply(payload), loop)
            except Exception:
                logger.debug("[line-worker] inbound 推送失败", exc_info=True)

        self.bot = bot

        def _run() -> None:
            try:
                bot.run(reconnect=True)
            except Exception:
                logger.debug("[line-worker] receiver 循环退出", exc_info=True)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

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
            _record_line_identity("cache_hit")
            return cached
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
        self._peer_ident_cache[mid] = (name, avatar)
        _record_line_identity("resolved" if name else "miss")
        return name, avatar

    def _resolve_peer_name(self, mid: str) -> str:
        """向后兼容薄封装：仅取显示名（内部走 ``_resolve_peer_identity``，头像一并缓存）。"""
        return self._resolve_peer_identity(mid)[0]

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("line client 未连接")
        res = await asyncio.to_thread(self.client.send_text, chat_key, text)
        mid = ""
        try:
            if isinstance(res, dict):
                mid = str(res.get("id") or "")
        except Exception:
            mid = ""
        return {"delivered": True, "message_id": mid}

    async def stop(self) -> None:
        self.state = "stopped"
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass

    async def healthy(self) -> bool:
        return bool(self.client is not None and self._thread is not None
                    and self._thread.is_alive())

    def status(self) -> Dict[str, Any]:
        return {"type": "line_protocol", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


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
    pl = (config or {}).get("platform_login", {}) or {}
    return bool(pl.get("orchestrator_enabled", False))
