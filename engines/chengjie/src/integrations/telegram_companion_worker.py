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


# ── TG 整段历史双向删除（2026-08-17「清空对方设备」）─────────────────────────

async def tg_delete_full_history(client: Any, chat_key: str,
                                 *, revoke: bool = True) -> Dict[str, Any]:
    """整段会话历史双向删除：raw ``messages.DeleteHistory(revoke=True)``。

    这是 Telegram 私聊独有的官方能力（连对方设备一起清空整段对话）；
    pyrogram 2.0.x 没有面向私聊的高层封装（``delete_user_history`` 是超级群
    踢人清言语义），故走 raw API。两个 TG worker（协议号 / companion）共用
    本函数——契约漂移只需改一处。语义边界：

    - 仅私聊/普通群（InputPeerUser/InputPeerChat）；超级群/频道是
      ``channels.deleteHistory`` 语义、对他人无 revoke → 如实
      ``unsupported_chat_type``（绝不静默降级成只删自己）。
    - 服务端按 ``offset`` 分页，大会话一次调用删不完 → 循环直到清完
      （带保险上限防协议异常死循环）。
    - pyrogram 惰性导入，随本模块「导入零重依赖」约定。
    """
    from pyrogram.raw import functions as _raw_fns
    from pyrogram.raw.types import InputPeerChannel

    target: Any = chat_key
    try:
        target = int(chat_key)
    except (TypeError, ValueError):
        target = chat_key
    peer = await client.resolve_peer(target)
    if isinstance(peer, InputPeerChannel):
        return {"ok": False, "reason": "unsupported_chat_type"}
    total = 0
    for _ in range(200):
        r = await client.invoke(_raw_fns.messages.DeleteHistory(
            peer=peer, max_id=0, revoke=bool(revoke)))
        total += int(getattr(r, "pts_count", 0) or 0)
        if not int(getattr(r, "offset", 0) or 0):
            break
    return {"ok": True, "deleted": total}


# ── feature flag ─────────────────────────────────────────────────────────────

def companion_runtime_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """是否让协议号走 A 线丰富运行时（默认关）。"""
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool(tg.get("companion_runtime", False))


def companion_media_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """companion 号是否对编排器暴露发媒体能力（``companion_media``，默认关）。

    **缺陷背景**（2026-07-31 实测）：开着 ``companion_runtime`` 的部署里，
    ``owns_media("telegram", *)`` 恒 False——因为本 worker 从没实现 ``send_media``
    （类 docstring 里却一直写着「可选 send_media(...)」，即本就是设计意图）。
    直接后果是生产接口自己报出来的：

        telegram  can_media=False can_voice=False voice_mode=none
        whatsapp  can_media=True  can_voice=True  voice_mode=composer
        line      can_media=True  can_voice=True  voice_mode=composer

    即**流量最大的 Telegram 是唯一一个坐席不能手动发图/发语音的平台**（按钮置灰、
    硬点 501）。AI 反而能发——它走 A 线 pyrogram 原生路径，绕开了编排器。

    **默认关而不是直接开**：``owns_media`` 是单一布尔，同时管着三条消费链，其中
    「主动触达语音」那条会把 Telegram 的沉默回访按 ``voice.probability`` 变成语音条
    ——属运营决策，不该由一次缺陷修复顺带决定。开＝一行 overlay + 重启。
    """
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool(tg.get("companion_media", False))


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
        # 出站媒体能力**按开关条件绑定**（与 LineProtocolWorker 同款做法，理由同源）：
        # 编排器 `owns_media()` 的判据就是 `hasattr(worker,"send_media")`，而这一个
        # 布尔同时管着三条消费链——坐席手动发图/发语音（`/api/unified-inbox/send-media`
        # 与 send-voice，不支持就 501 且按钮置灰）、B 线 System Z 自动发媒体、以及
        # 主动触达的语音/照片投递。写成普通方法＝三条一次性全开，其中主动触达那条
        # 会让 Telegram 的沉默回访按 `voice.probability` 变成语音条，属运营决策。
        # ⚠ 别顺手「简化」成 `async def send_media`，那会静默改变生产行为。
        try:
            if companion_media_enabled(config):
                self.send_media = self._send_media_impl  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.debug("[tg-companion] 出站媒体开关解析失败（按关处理）", exc_info=True)

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
        # A 线 TelegramClient.start 内部吞异常（只记日志不 raise）——这里必须
        # 复核 running 并把原始根因（如 [401 SESSION_REVOKED]）重新抛给编排器：
        # 编排器据此分类 session_revoked → 注册表落 offline + 健康表 logged_out +
        # 告警，坐席账号抽屉才能看见「该号已在手机端退出，需重新扫码」。
        if not getattr(self.client, "running", False):
            reason = str(getattr(self.client, "last_start_error", "") or "")
            self.client = None
            raise RuntimeError(
                f"A 线 TelegramClient 启动失败：{reason or '未知原因（见日志）'}")
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("A 线 client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        # P4-4：取回真实 message.id，让编排器出站回写带 id → 已读回执（双勾）可精确绑定该行。
        # A 线 TelegramClient 暴露 send_message_return_id；缺失（旧壳）时优雅回落只回 bool。
        _fn = getattr(self.client, "send_message_return_id", None)
        if _fn is not None:
            ok, mid = await _fn(target, text)
            return {"delivered": bool(ok), "message_id": str(mid or "")}
        ok = await self.client.send_message(target, text)
        return {"delivered": bool(ok), "message_id": ""}

    async def _send_media_impl(self, chat_key: str, *, media_path: str,
                               media_type: str = "",
                               caption: str = "") -> Dict[str, Any]:
        """发媒体（图/语音/视频/文件）。只在 ``companion_media`` 开时才被绑成
        ``send_media`` —— 见 ``__init__`` 里那段说明，别改成普通方法。

        走**内层 pyrogram**（``self.client.client``）而不是 A 线包装
        ``TelegramClient.send_photo``：后者自带收件箱出站镜像，而
        ``AccountOrchestrator.send_media`` 也会镜像一次 —— 委派过去会让一次发送在
        坐席台出现**两行**。护栏面与兄弟 worker ``TelegramProtocolWorker.send_media``
        一致（Kill-Switch / 反封号闸门 / 去重微扰都在编排器那一层统一做）。
        """
        inner = getattr(self.client, "client", None) if self.client is not None else None
        if inner is None:
            raise RuntimeError("A 线 client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        kind = str(media_type or "").lower()
        if kind == "image":
            msg = await inner.send_photo(target, media_path, caption=caption)
        elif kind == "voice":
            msg = await inner.send_voice(target, media_path, caption=caption)
        elif kind == "video":
            msg = await inner.send_video(target, media_path, caption=caption)
        else:
            msg = await inner.send_document(target, media_path, caption=caption)
        return {"delivered": True, "message_id": str(getattr(msg, "id", "") or "")}

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

    async def delete_messages(self, chat_key: str, message_ids: List[str],
                              *, revoke: bool = True) -> Dict[str, Any]:
        """删除若干条消息（2026-08-17 双端撤回，经 A 线内层 pyrogram client）。

        与兄弟 worker ``TelegramProtocolWorker.delete_messages`` 同契约：
        ``revoke=True``＝对所有人删除；platform_msg_id 即 pyrogram 消息 id。
        worker 客户端活在 web 线程 loop → 收件箱路由内直接 await 即可（同 loop）。
        """
        inner = getattr(self.client, "client", None) if self.client is not None else None
        if inner is None or not hasattr(inner, "delete_messages"):
            raise RuntimeError("A 线 client 未连接")
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
        n = await inner.delete_messages(target, ids, revoke=bool(revoke))
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = len(ids)
        return {"ok": n > 0, "deleted": n}

    async def delete_history(self, chat_key: str,
                             *, revoke: bool = True) -> Dict[str, Any]:
        """整段历史双向删除（2026-08-17「清空对方设备」，经 A 线内层 pyrogram）。

        与兄弟 worker ``TelegramProtocolWorker.delete_history`` 同契约，
        实现共用 ``tg_delete_full_history``（私聊限定，超级群/频道如实拒绝）。
        """
        inner = getattr(self.client, "client", None) if self.client is not None else None
        if inner is None or not hasattr(inner, "resolve_peer"):
            raise RuntimeError("A 线 client 未连接")
        return await tg_delete_full_history(inner, chat_key, revoke=revoke)

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
