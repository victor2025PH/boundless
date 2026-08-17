"""N 线 核心4：统一运行时——让协议号跑 A 线"有灵魂"的 TelegramClient。

收敛 A/B 两线的命门：
- **B 线**（扫码/协议登录）把 session 落到 ``sessions/<session_name>.session``，并登记
  ``account_registry(mode=protocol, meta.session_name)``；其默认 worker
  ``TelegramProtocolWorker`` 只起一个**精简** pyrogram 连接（收消息→收件箱/简单 autoreply）。
- **A 线**（``src/client/telegram_client.py::TelegramClient``）是"有灵魂"的丰富 client：
  记忆/人设/情绪增强/语音图片识别/四层触发/人工转接/GXP/定时任务……但原本只能由
  ``main.py`` 按 config 单/多账号拉起。

本 worker 把两者打通：用 B 线落盘的 ``session_name`` 直接拉起 **A 线 TelegramClient**
（session 已授权，无需 phone），从而"扫码登录的号"也获得完整陪聊能力。

零破坏约定：
- 默认关（``platform_login.telegram.companion_runtime: false``）。关时编排器仍用既有
  ``TelegramProtocolWorker``（B 线薄连接），行为不变。
- A 线 client 需要 ``config_manager`` + ``skill_manager``（+ 可选 ``ai_client``），而编排器
  只有 config dict。故由 app 启动时经 ``set_companion_context`` 注入一份进程级运行时上下文，
  本 worker 在 ``start()`` 时读取。上下文未就绪 → 启动报错（被编排器退避兜住），不影响主进程。
- pyrogram / TelegramClient 全程**惰性导入**（在 ``start()`` 内），模块导入零重依赖、可单测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── 进程级运行时上下文（app 启动时注入；worker 读取以构建 A 线 client） ──────────

_CTX: Dict[str, Any] = {
    "config_manager": None,
    "skill_manager": None,
    "ai_client": None,
}


def set_companion_context(
    *, config_manager: Any, skill_manager: Any, ai_client: Any = None
) -> None:
    """app 启动时注入构建 A 线 client 所需的依赖（幂等，可重复覆盖）。"""
    _CTX["config_manager"] = config_manager
    _CTX["skill_manager"] = skill_manager
    _CTX["ai_client"] = ai_client


def get_companion_context() -> Dict[str, Any]:
    return dict(_CTX)


def companion_context_ready() -> bool:
    return _CTX.get("config_manager") is not None and _CTX.get("skill_manager") is not None


def reset_companion_context() -> None:
    """测试钩子：清空注入的上下文。"""
    _CTX["config_manager"] = None
    _CTX["skill_manager"] = None
    _CTX["ai_client"] = None


# ── feature flag ─────────────────────────────────────────────────────────────

def companion_runtime_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """是否让协议号走 A 线丰富运行时（默认关）。"""
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool(tg.get("companion_runtime", False))


# ── worker ───────────────────────────────────────────────────────────────────

class TelegramCompanionWorker:
    """用 B 线落盘 session 拉起 A 线 TelegramClient，并实现编排器 worker 协议。

    worker 协议：``async start()/stop()``、``async healthy()->bool``、``status()->dict``，
    可选 ``async send(chat_key, text)`` / ``send_media(...)`` 供收件箱出站经此号发送。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.account_id = str(self.account.get("account_id") or "")
        meta = self.account.get("meta") or {}
        self.session_name = str(meta.get("session_name") or "")
        self.session_string = str(meta.get("session_string") or "")
        self.proxy_id = str(self.account.get("proxy_id") or "")
        # SSOT：persona_id 标量优先，再 persona_ids[0]（修「只写了 persona_id 工人读空」）
        _pids = list(meta.get("persona_ids") or [])
        _sing = str(meta.get("persona_id") or "").strip()
        if _sing and (not _pids or str(_pids[0]) != _sing):
            _pids = [_sing] + [p for p in _pids if str(p) != _sing]
        self.persona_ids: List[Any] = _pids
        self.client: Any = None  # A 线 TelegramClient 实例
        self.state = "stopped"
        self.detail = ""

    def _account_cfg(self) -> Dict[str, Any]:
        """组装 A 线 TelegramClient 的 account_cfg overlay（session/代理/人设）。

        凭据与指纹**不在这里定**——它们要向中央池现场解析（异步），见 ``start()``
        里的 ``_isolation_overlay``。
        """
        cfg: Dict[str, Any] = {
            "account_id": self.account_id,
            "account_label": str(self.account.get("label") or self.account_id),
            "proxy_id": self.proxy_id,
            "persona_ids": self.persona_ids,
        }
        if self.session_name:
            cfg["session_name"] = self.session_name
        if self.session_string:
            cfg["session_string"] = self.session_string
        # N4b：协议号默认把收/发镜像进统一收件箱，坐席台/收件箱可见（"有灵魂"且可托管）
        cfg["mirror_inbox"] = True
        return cfg

    async def _isolation_overlay(self) -> Dict[str, Any]:
        """三隔离在**连接这一刻**的落地：池凭据 + 设备指纹 + 池出口。

        为什么必须在这条路上做（2026-07-27 实测发现的空转）：本实例开
        ``companion_runtime``，协议号实际由本 worker → A 线 ``TelegramClient``
        拉起，而那条路原本只从全局 ``telegram.*`` 取 api_id、且完全不带设备字段。
        后果有两层：
          · 凭据：扫码时用的是池凭据，重连却用配置里的那组 —— 池里一旦有第二组
            凭据，就是「session 与 api_id 错配」（Telegram 明确的风控信号）。
            当下没出事只因为配置里那组恰好就是池里唯一那组，是巧合不是设计。
          · 指纹：扫码时向 Telegram 报的是派生机型（如 MacBook Pro），重连又变回
            pyrogram 默认值 —— **每次重连都在换设备**，比根本不做指纹更可疑。

        解析不到凭据 → 抛错让 worker 保持 stopped，绝不用配置凭据顶上（同
        credpool 契约 §4b：宁可这一个号暂时不上线，也不能带着错配的 api_id 连）。
        """
        from src.integrations.credpool_bridge import (
            aresolve_for_account,
            credpool_enabled,
            pool_key_of,
        )

        overlay: Dict[str, Any] = {}
        # 设备指纹独立于中央池（本地种子派生），先做——它自己有开关，关着就返回空。
        from src.integrations.device_fingerprint import client_kwargs_for_account

        overlay.update(client_kwargs_for_account(self.config, self.account))

        if not credpool_enabled(self.config):
            # 没开池 → **一个字都不改凭据**：让 TelegramClient 照旧从
            # config.get_telegram_config() 取（那条路可能带 env 覆盖/账号 overlay，
            # 与 resolve_credentials 的读法并不完全等价）。所有客户桌面都是这一档，
            # 不该为了池的正确性去动它们的行为。
            return overlay

        alloc = await aresolve_for_account(self.config, account=self.account)
        is_pool_account = bool(pool_key_of(self.account))
        if alloc is None:
            if is_pool_account:
                raise RuntimeError(
                    "无法解析 api 凭据（中央池不可达且该号无凭据缓存）；"
                    "为避免 session 与 api_id 错配触发风控，本号暂不上线"
                )
            return overlay  # 存量号：交给 TelegramClient 原读法，它自己会报凭据不全
        if alloc.source not in ("credpool", "credpool_cache"):
            # 凭据不是池给的（存量号回落自带那组）→ **不覆盖**。
            # 值虽然通常相同，但两条读法并不等价：resolve_credentials 直接读
            # config["telegram"]，TelegramClient 走 config.get_telegram_config()
            # （可能带 env 覆盖/账号 overlay）。不动我不拥有的东西。
            return overlay
        overlay["api_id"] = alloc.api_id
        overlay["api_hash"] = alloc.api_hash
        # 出口优先级与 B 线一致：账号上显式绑定的代理 > 池按付费档下发的独立出口
        if alloc.proxy and not self.proxy_id:
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy

            pxy = _to_pyrogram_proxy(alloc.proxy)
            if pxy:
                overlay["proxy"] = pxy
        logger.info(
            "[companion_worker] %s 连接凭据 source=%s tier=%s 独立出口=%s 指纹=%s",
            self.account_id, alloc.source, alloc.tier,
            "有" if overlay.get("proxy") else "无",
            overlay.get("device_model") or "默认")
        return overlay

    async def start(self) -> None:
        ctx = get_companion_context()
        config_manager = ctx.get("config_manager")
        skill_manager = ctx.get("skill_manager")
        if config_manager is None or skill_manager is None:
            raise RuntimeError(
                "companion runtime 上下文未就绪（缺 config_manager/skill_manager）；"
                "需 app 启动时调用 set_companion_context"
            )
        if not (self.session_name or self.session_string):
            raise RuntimeError("缺少 session（需先扫码/手机登录得到 session_name 或 session_string）")

        # 重启前先清理旧 client，避免连接泄漏
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

        from src.client.telegram_client import TelegramClient  # 惰性：避开 pyrogram 重依赖
        _cfg = self._account_cfg()
        _cfg.update(await self._isolation_overlay())
        self.client = TelegramClient(
            config=config_manager,
            skill_manager=skill_manager,
            ai_client=ctx.get("ai_client"),
            account_cfg=_cfg,
        )
        ok = await self.client.initialize()
        if not ok:
            self.client = None
            raise RuntimeError("A 线 TelegramClient 初始化失败（凭据/session 不可用）")
        # 编排器托管：非阻塞启动（不进入 idle()）
        await self.client.start(block=False)
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发文本；带 ``reply_to`` 时走 Telegram 原生引用回复（``reply_to_message_id``）。

        引用是气泡装饰，其**缺失不得阻断投递**：被引用的 id 解析不出（非数字/太旧）时
        自动退回普通发送。两层降级——① A 线壳若尚未接 ``reply_to_message_id`` 参数
        （patch 未同步）则 TypeError 回落无引用；② 编排器端本就有逐级降级探测兜住本
        worker 的签名，故老编排器调用零影响。
        """
        if self.client is None:
            raise RuntimeError("A 线 client 未连接")
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
        # P4-4：取回真实 message.id，让编排器出站回写带 id → 已读回执（双勾）可精确绑定该行。
        # A 线 TelegramClient 暴露 send_message_return_id；缺失（旧壳）时优雅回落只回 bool。
        # ``_rid`` 为 None（无引用，占绝大多数调用）时**逐字走旧调用形态**——不给底层
        # 多传 kwarg，零兼容风险；仅真要引用时才带，且 TypeError 回落容旧壳。
        _fn = getattr(self.client, "send_message_return_id", None)
        if _fn is not None:
            if _rid is not None:
                try:
                    ok, mid = await _fn(target, text, reply_to_message_id=_rid)
                except TypeError:
                    ok, mid = await _fn(target, text)
            else:
                ok, mid = await _fn(target, text)
            return {"delivered": bool(ok), "message_id": str(mid or "")}
        if _rid is not None:
            try:
                ok = await self.client.send_message(
                    target, text, reply_to_message_id=_rid)
            except TypeError:
                ok = await self.client.send_message(target, text)
        else:
            ok = await self.client.send_message(target, text)
        return {"delivered": bool(ok), "message_id": ""}

    async def ensure_peer(self, chat_key: str) -> bool:
        """群 peer 可达性体检（开演前 preflight）：True=此号能对该群发言。

        转发 A 线 ``TelegramClient.ensure_group_peer``（缓存热零 RPC，冷则
        dialogs → GetAllChats 两级预热后复核）。旧壳无此方法 → True 不拦
        （交给发送路径的 peer 自愈兜底）。
        """
        if self.client is None:
            return False
        fn = getattr(self.client, "ensure_group_peer", None)
        if fn is None:
            return True
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        return bool(await fn(target))

    async def invite_to_group(self, chat_key: str, user_ref: str) -> Dict[str, Any]:
        """把 ``user_ref``（user_id 或 username）拉进群 ``chat_key``（排班补位）。

        P3-5 灰度实锤的缺口：运营手工拉群不可靠（隐私弹窗/加错群都无回执），
        排班台账标了「已进群」实际没进 → 开演首拍才炸。这里给「群里的号拉
        候补号」一条有回执的程序化路径；username 引用可绕开「邀请方不认识
        被邀请方」的 peer 缓存死锁（ResolveUsername 是全局 RPC）。

        返回 ``{ok, kind, error}``；kind 归类平台语义，供上层决定下一步：
        ``already``＝本就在群（幂等成功）、``privacy``＝对方隐私禁止被拉
        （只能发链接让 TA 自己进）、``admin_required``＝本号无邀请权限。
        """
        if self.client is None:
            return {"ok": False, "kind": "offline", "error": "client 未连接"}
        inner = getattr(self.client, "client", None)
        if inner is None or not hasattr(inner, "add_chat_members"):
            return {"ok": False, "kind": "unsupported", "error": "无 add_chat_members 能力"}
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        user: Any = str(user_ref or "").lstrip("@")
        try:
            user = int(user)
        except (TypeError, ValueError):
            pass
        try:
            await inner.add_chat_members(target, user)
            return {"ok": True, "kind": "invited", "error": ""}
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            text = f"{name}: {exc}"
            low = (name + " " + str(exc)).lower()
            if "alreadyparticipant" in low.replace("_", ""):
                return {"ok": True, "kind": "already", "error": ""}
            if "privacy" in low:
                kind = "privacy"
            elif "adminrequired" in low.replace("_", "") or "right" in low:
                kind = "admin_required"
            elif "mutual" in low:
                kind = "not_mutual"
            elif "peer" in low or "channelinvalid" in low.replace("_", ""):
                kind = "peer"
            elif "flood" in low:
                kind = "flood"
            else:
                kind = "error"
            logger.warning("[tg-companion] 拉群失败 inviter=%s group=%s user=%s: %s",
                           self.account_id, chat_key, user_ref, text)
            return {"ok": False, "kind": kind, "error": text}

    async def mark_read(self, chat_key: str) -> bool:
        """对该会话发「已读」回执（经 A 线内层 pyrogram client；拟人「先看后回」）。"""
        inner = getattr(self.client, "client", None) if self.client is not None else None
        if inner is None or not hasattr(inner, "read_chat_history"):
            return False
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        await inner.read_chat_history(target)
        return True

    async def send_chat_action(self, chat_key: str, action: str = "typing") -> bool:
        """挂「正在输入/录音」状态（经 A 线内层 pyrogram client）。"""
        inner = getattr(self.client, "client", None) if self.client is not None else None
        if inner is None or not hasattr(inner, "send_chat_action"):
            return False
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        try:
            from pyrogram.enums import ChatAction
            act = ChatAction.RECORD_AUDIO if str(action) == "record_audio" else ChatAction.TYPING
        except Exception:
            return False
        await inner.send_chat_action(target, act)
        return True

    async def stop(self) -> None:
        try:
            if self.client is not None:
                await self.client.stop()
        except Exception:  # noqa: BLE001
            logger.debug("[tg-companion] 停止 client 失败", exc_info=True)
        self.client = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        try:
            if self.client is None or not getattr(self.client, "running", False):
                return False
            inner = getattr(self.client, "client", None)
            return bool(inner is not None and getattr(inner, "is_connected", False))
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> Dict[str, Any]:
        return {
            "type": "telegram_companion",
            "session": self.session_name,
            "account_id": self.account_id,
            "state": self.state,
            "detail": self.detail,
        }


__all__ = [
    "set_companion_context",
    "get_companion_context",
    "companion_context_ready",
    "reset_companion_context",
    "companion_runtime_enabled",
    "TelegramCompanionWorker",
]
