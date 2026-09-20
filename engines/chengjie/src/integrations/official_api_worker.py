"""官方 API 出站 worker（Phase G 延伸：mode=official）。

把 LINE/Messenger/WhatsApp 三端**官方 API** 接入账号池编排器，使
``orch.send(platform, account_id, chat_key, text)`` 主管道（companion / protocol_autoreply）
能直接经官方通道发——而不止于各 webhook 自身的 reply 管道。

与既有 worker 的差别：官方 API 是**无状态 HTTP**（无常驻连接），故 ``start/stop`` 是 no-op，
``healthy()`` 只校验凭证齐备。发送复用 G1/G2 的官方 send 助手（已内建 Kill-Switch 守卫）。
例外：QQ 机器人（``qqbot``）在 WebSocket 模式下有一条常驻网关长连，由子类
``QQBotOfficialWorker`` 在 ``start()`` 里拉起、``healthy()`` 反映连接态（见该类）。

凭证解析：优先账号 ``meta``（多官方账号），回退到平台级 config 块（单官方账号）：
- LINE       ：meta.channel_access_token | config.line.channel_access_token
- Messenger  ：meta.page_access_token    | config.facebook_messenger.page_access_token
- WhatsApp   ：meta.access_token+phone_number_id | config.whatsapp_cloud.{access_token,phone_number_id}
- QQ 机器人  ：meta.app_id+app_secret    | config.qqbot.{app_id,app_secret}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 编排器接管的官方平台（platform 与 RPA/Kill-Switch 作用域命名保持一致）
OFFICIAL_PLATFORMS = ("line", "messenger", "whatsapp", "instagram", "zalo", "qqbot", "wechat_kf")
#: 有**专属有状态 worker** 的官方平台（不由本模块的无状态 OfficialApiWorker 服务）：
#: 微信客服（实施97）要 sync_msg 游标轮询 + token 自管 → src/integrations/wechat_kf_worker.py 自行
#: register_worker。列在 OFFICIAL_PLATFORMS 里是为了渠道向导/一致性门禁把它当官方通道对待，
#: register_official_workers 对这些平台跳过，免得抢注一个 start() 必抛的空壳。
DEDICATED_WORKER_PLATFORMS = frozenset({"wechat_kf"})


def _meta(account: Dict[str, Any]) -> Dict[str, Any]:
    return dict((account or {}).get("meta") or {})


def dest_from_chat_key(chat_key: str) -> str:
    """从统一收件箱 chat_key 提取官方平台「裸」收件人标识（G4b 接管发送闭环关键）。

    G4 入站镜像写入的 chat_key 形如 ``line:user:<uid>`` / ``wa:user:<num>`` /
    ``fb:user:<psid>`` / ``line:group:<gid>``；而官方 send 助手（line_push / fb_send /
    wa_send_text）要的是**裸标识**。取最后一段即可——裸标识（LINE userId「U…」、PSID、
    手机号）本身不含冒号，故对「入参已是裸标识」的调用（如 companion 主管道直传）幂等。
    """
    s = str(chat_key or "").strip()
    if not s:
        return s
    return s.rsplit(":", 1)[-1]


def _cfg_block(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    return dict((config or {}).get(key) or {})


class OfficialApiWorker:
    """单账号官方 API 出站 worker（按 platform 分发到对应 send 助手）。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.platform = str(self.account.get("platform") or "").strip().lower()
        self.account_id = str(self.account.get("account_id") or "").strip() or "default"
        self.state = "stopped"
        self.detail = ""

    # ── 凭证 ─────────────────────────────────────────────────────────────────
    def _creds(self) -> Dict[str, str]:
        m = _meta(self.account)
        if self.platform == "line":
            block = _cfg_block(self.config, "line")
            return {"access_token": str(m.get("channel_access_token")
                                        or block.get("channel_access_token") or "")}
        if self.platform == "messenger":
            block = _cfg_block(self.config, "facebook_messenger")
            return {"access_token": str(m.get("page_access_token")
                                        or block.get("page_access_token") or "")}
        if self.platform == "whatsapp":
            block = _cfg_block(self.config, "whatsapp_cloud")
            return {
                "access_token": str(m.get("access_token") or block.get("access_token") or ""),
                "phone_number_id": str(m.get("phone_number_id")
                                       or block.get("phone_number_id") or ""),
            }
        if self.platform == "instagram":
            block = _cfg_block(self.config, "instagram")
            return {
                "access_token": str(m.get("page_access_token")
                                    or block.get("page_access_token") or ""),
                "ig_id": str(m.get("ig_id") or block.get("ig_id") or ""),
            }
        if self.platform == "zalo":
            block = _cfg_block(self.config, "zalo")
            return {
                "access_token": str(m.get("access_token") or block.get("access_token") or ""),
                "message_type": str(m.get("message_type")
                                    or block.get("message_type") or "cs"),
            }
        if self.platform == "qqbot":
            from src.integrations.qq_official import creds_from
            return creds_from(self.config, m)
        return {}

    def _creds_ok(self) -> bool:
        c = self._creds()
        if self.platform == "whatsapp":
            return bool(c.get("access_token") and c.get("phone_number_id"))
        if self.platform == "qqbot":
            return bool(c.get("app_id") and c.get("app_secret"))
        return bool(c.get("access_token"))

    # ── Worker 接口 ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.platform not in OFFICIAL_PLATFORMS:
            raise RuntimeError(f"不支持的官方平台: {self.platform}")
        if not self._creds_ok():
            raise RuntimeError(f"官方 {self.platform} 缺少凭证")
        self.state = "running"
        self.detail = ""

    async def stop(self) -> None:
        self.state = "stopped"

    async def healthy(self) -> bool:
        return self.state == "running" and self._creds_ok()

    def status(self) -> Dict[str, Any]:
        return {"type": f"{self.platform}_official", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """chat_key = 收件人标识（接受收件箱前缀形式 ``<plat>:user:<id>`` 或裸标识，自动归一）。

        ``reply_to``（编排器按具名形参透传）：仅 QQ 机器人消费——作 ``message_reference``
        原生引用；其余官方平台无引用语义，收下即忽略（与 LINE worker 的旧行为一致）。
        """
        c = self._creds()
        if self.platform == "qqbot":
            from src.integrations.qq_official import qqbot_send_text
            _ref = str((reply_to or {}).get("id") or (reply_to or {}).get("msg_id") or "")
            out = await qqbot_send_text(
                chat_key, text, config=self.config, meta=_meta(self.account),
                account_id=self.account_id, reply_to_msg_id=_ref)
            res = self._result(out, str(((out.get("data") or {}).get("id")) or ""))
            if out.get("blocked"):
                res["blocked"] = str(out["blocked"])
            # 引用回执如实：只有真带了 message_reference（qqbot.message_reference 开启）
            # 且发成功才让工作台画引用条——开放平台文档标该字段「暂未支持」，默认不带
            res["quote_applied"] = bool(out.get("ok") and out.get("quoted"))
            return res
        dest = dest_from_chat_key(chat_key)
        if self.platform == "line":
            from src.integrations.line_webhook import line_push
            ok = await line_push(dest, text, c["access_token"],
                                 account_id=self.account_id)
            return {"delivered": bool(ok)}
        if self.platform == "messenger":
            from src.integrations.facebook_webhook import fb_send_with_window_fallback
            out = await fb_send_with_window_fallback(
                dest, text, c["access_token"], account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "whatsapp":
            from src.integrations.whatsapp_cloud import wa_send_text
            out = await wa_send_text(dest, text, c["phone_number_id"], c["access_token"])
            data = out.get("data") or {}
            mid = ""
            try:
                mid = str(((data.get("messages") or [{}])[0]).get("id") or "")
            except Exception:
                mid = ""
            return self._result(out, mid)
        if self.platform == "instagram":
            from src.integrations.instagram_webhook import (
                ig_send_text, ig_send_with_window_fallback,
            )
            ig_cfg = _cfg_block(self.config, "instagram")
            if bool(_meta(self.account).get("human_agent_fallback")
                    or ig_cfg.get("human_agent_fallback")):
                out = await ig_send_with_window_fallback(
                    dest, text, c.get("ig_id", ""), c["access_token"],
                    account_id=self.account_id)
            else:
                out = await ig_send_text(dest, text, c.get("ig_id", ""),
                                         c["access_token"], account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "zalo":
            from src.integrations.zalo_webhook import zalo_send_text
            out = await zalo_send_text(dest, text, c["access_token"],
                                       message_type=c.get("message_type", "cs"),
                                       account_id=self.account_id)
            data = out.get("data") or {}
            mid = str((data.get("data") or {}).get("message_id") or "") if isinstance(data, dict) else ""
            return self._result(out, mid)
        raise RuntimeError(f"不支持的官方平台: {self.platform}")

    def _public_media_url(self, media_url: str) -> str:
        """把 ``/static`` 相对 URL 拼成公网绝对 https URL（LINE/Messenger 必须，FB/LINE 自取字节）。

        - 已是 http(s) 绝对 URL → 原样返回。
        - 否则取 ``config.official_media.public_base_url`` 前缀拼接；未配置 → 返回空串
          （调用方据此回 ``no_public_url``，可观测而非静默失败）。
        """
        u = str(media_url or "").strip()
        if u.lower().startswith(("http://", "https://")):
            return u
        base = str(
            (((self.config or {}).get("official_media") or {}).get("public_base_url")) or ""
        ).strip().rstrip("/")
        if not base or not u:
            return ""
        if not u.startswith("/"):
            u = "/" + u
        return base + u

    async def send_media(
        self, chat_key: str, *, media_path: str, media_type: str,
        caption: str = "", media_url: str = "",
    ) -> Dict[str, Any]:
        """官方通道媒体出站（语音/图片/…），与 ``orch.send_media`` 契约一致。

        - WhatsApp：上传本地文件 → media_id 发送（**无需公网 URL**）。
        - LINE / Messenger：按**公网 https URL** 发送（需配 ``official_media.public_base_url``，
          否则回 ``error_kind=no_public_url``）。
        - Instagram / Zalo：官方 API 媒体出站暂未接入 → ``not_supported``。
        """
        c = self._creds()
        dest = dest_from_chat_key(chat_key)
        mt = str(media_type or "").lower()
        if self.platform == "whatsapp":
            from src.integrations.whatsapp_cloud import wa_send_media
            out = await wa_send_media(
                dest, media_path, c["phone_number_id"], c["access_token"],
                media_type=mt, caption=caption)
            mid = ""
            try:
                mid = str((((out.get("data") or {}).get("messages") or [{}])[0]).get("id") or "")
            except Exception:
                mid = ""
            return self._result(out, mid)
        if self.platform == "line":
            pub = self._public_media_url(media_url)
            if not pub:
                return {"delivered": False, "error_kind": "no_public_url",
                        "error": "LINE 媒体需公网 https URL（配 official_media.public_base_url）"}
            dur = 0
            if mt in ("voice", "audio"):
                try:
                    from src.client.voice_sender import probe_audio_duration_ms
                    dur = int(probe_audio_duration_ms(media_path) or 0)
                except Exception:
                    dur = 0
            from src.integrations.line_webhook import line_push_media
            ok = await line_push_media(
                dest, pub, c["access_token"], media_type=mt,
                duration_ms=dur, account_id=self.account_id)
            return {"delivered": True} if ok else {
                "delivered": False, "error_kind": "send_failed"}
        if self.platform == "messenger":
            # 优先公网 URL；未配则 multipart 字节上传（免公网托管，与 WhatsApp 对齐）。
            pub = self._public_media_url(media_url)
            if pub:
                from src.integrations.facebook_webhook import fb_send_attachment
                out = await fb_send_attachment(
                    dest, pub, c["access_token"], media_type=mt, account_id=self.account_id)
            else:
                from src.integrations.facebook_webhook import fb_send_attachment_upload
                out = await fb_send_attachment_upload(
                    dest, media_path, c["access_token"], media_type=mt,
                    account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "instagram":
            # IG DM 附件仅支持公网 URL（不支持 filedata 上传）。
            pub = self._public_media_url(media_url)
            if not pub:
                return {"delivered": False, "error_kind": "no_public_url",
                        "error": "Instagram 媒体需公网 https URL（配 official_media.public_base_url）"}
            from src.integrations.instagram_webhook import ig_send_attachment
            out = await ig_send_attachment(
                dest, pub, c.get("ig_id", ""), c["access_token"],
                media_type=mt, account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "zalo":
            # Zalo OA API 无语音消息出站能力（图片/文件另需 upload 流程，暂未接入）。
            return {"delivered": False, "error_kind": "not_supported",
                    "error": "Zalo OA API 暂不支持语音/媒体消息出站"}
        if self.platform == "qqbot":
            # 图/视频：公网 URL → /files 换 file_info → msg_type=7 被动发送（qq_official）；
            # 语音须 silk、文件类平台暂不开放 → not_supported（official_send_caps 同口径）。
            from src.integrations.qq_official import qqbot_send_media
            out = await qqbot_send_media(
                chat_key, media_type=mt, media_path=media_path, media_url=media_url,
                caption=caption, config=self.config, meta=_meta(self.account),
                account_id=self.account_id)
            res = self._result(out, str(((out.get("data") or {}).get("id")) or ""))
            if out.get("blocked"):
                res["blocked"] = str(out["blocked"])
            return res
        return {"delivered": False, "error_kind": "not_supported",
                "error": f"{self.platform} 官方通道媒体出站暂未接入"}

    @staticmethod
    def _result(out: Dict[str, Any], message_id: str) -> Dict[str, Any]:
        """统一出站结果：delivered + message_id，失败时透出 error_kind（窗口/token/限速…）。

        让上层（pipeline/可观测）能据 ``error_kind`` 分流，而非把失败默默当"没发出"。
        """
        res: Dict[str, Any] = {"delivered": bool(out.get("ok")), "message_id": message_id}
        if not out.get("ok"):
            res["error_kind"] = str(out.get("error_kind") or "unknown")
            res["error"] = str(out.get("error") or "")
        return res


#: 官方通道里媒体出站**必须走公网 URL** 的平台（对应 send_media 的 no_public_url 分支）。
OFFICIAL_MEDIA_URL_PLATFORMS = ("line", "instagram")


def official_send_caps(platform: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """官方通道的**诚实**媒体/语音能力位（供 send-caps 端点按账号 mode 覆盖）。

    背景：``owns_media()`` 的判据是 ``hasattr(worker, "send_media")``——官方 worker
    类上方法恒存在，但运行时按平台分支：Zalo 直接 ``not_supported``、LINE/Instagram
    没配 ``official_media.public_base_url`` 就是 ``no_public_url``。按 hasattr 报能力
    会让坐席对着可点的按钮撞运行时错误（「点了才知道不行」正是能力位要消灭的）。

    本表**必须与 ``send_media`` 的运行时分支一致**，由门禁
    ``tests/test_official_channel_onboarding.py`` 双向钉住（改分支不改这里会红）。
    返回 ``{"can_media": bool, "can_voice": bool, "reason": str}``；reason ∈
    ``""`` / ``zalo_api_no_media`` / ``needs_public_url`` / ``qqbot_media_pending``。
    """
    p = str(platform or "").lower()
    if p == "zalo":
        return {"can_media": False, "can_voice": False, "reason": "zalo_api_no_media"}
    if p == "qqbot":
        # 图/视频走 /files 上传（只收公网 URL，与 LINE/IG 同一份 public_base_url）；语音须
        # silk 编码本机没有 → 永远 False。reason 沿用 ``qqbot_media_pending`` 键（前端灰态
        # 文案已按「语音待接、图/视频可发」改写；换键名要动 unified_inbox.html 的映射）。
        base = str(
            (((config or {}).get("official_media") or {}).get("public_base_url")) or ""
        ).strip()
        if not base:
            return {"can_media": False, "can_voice": False, "reason": "needs_public_url"}
        return {"can_media": True, "can_voice": False, "reason": "qqbot_media_pending"}
    if p in OFFICIAL_MEDIA_URL_PLATFORMS:
        base = str(
            (((config or {}).get("official_media") or {}).get("public_base_url")) or ""
        ).strip()
        ok = bool(base)
        return {"can_media": ok, "can_voice": ok,
                "reason": "" if ok else "needs_public_url"}
    # WhatsApp Cloud（media_id 直传）/ Messenger（URL 或 multipart 直传）无前置依赖
    return {"can_media": True, "can_voice": True, "reason": ""}


def official_pipeline_enabled(config: Dict[str, Any]) -> bool:
    """官方入站是否走 protocol_autoreply 主管道（G4c）。

    默认 **False** → 各 webhook 维持自身 SkillManager 自答（零回归）。
    开启后官方入站 → `maybe_auto_reply`：享 kill-switch 决策期早退 / canary / 陪伴记忆 /
    限速熔断 / 审计 / 转人工，且回复经 `orch.send`→官方 worker 出站（需官方账号 mode=official
    且被编排器接管；否则 run_autoreply 因 disabled/无 worker 不发——属预期，见 DEVLOG G4c）。
    """
    return bool(((config or {}).get("official_pipeline") or {}).get("enabled"))


def official_enabled(config: Dict[str, Any], platform: str) -> bool:
    """该官方平台是否在 config 中启用（platform_login.official.<platform> 或对应通道块 enabled）。"""
    p = str(platform or "").lower()
    pl = ((config or {}).get("platform_login") or {}).get("official") or {}
    if pl.get(p, {}).get("enabled"):
        return True
    # 回退：对应官方通道块自身 enabled 也视为开
    key = {"line": "line", "messenger": "facebook_messenger",
           "whatsapp": "whatsapp_cloud", "instagram": "instagram",
           "zalo": "zalo", "qqbot": "qqbot"}.get(p)
    if key and ((config or {}).get(key) or {}).get("enabled"):
        return True
    return False


class QQBotOfficialWorker(OfficialApiWorker):
    """QQ 机器人官方 worker：在 ``OfficialApiWorker`` 之上多跑一条 WS 网关长连。

    - ``connect_mode=websocket``（默认，桌面单机免公网）：``start()`` 拉起
      ``QQBotGateway.run()`` 后台任务，事件经 ``handle_qqbot_event`` 进统一收件箱；
      网关自带断线退避重连，故 ``healthy()`` 只在**致命**（4914 下架 / 4915 封禁 /
      鉴权被拒）或任务意外死亡时报 False——避免与编排器双重监督（同 WhatsApp 的取舍）。
    - ``connect_mode=webhook``：事件由 ``register_qqbot_routes`` 挂的 HTTPS 回调进来，
      本 worker 退化为无状态出站，与父类同行为。
    """

    #: 网关启动后允许「尚未 READY」的宽限（首连 token+gateway+identify 通常 <5s）
    CONNECT_GRACE_SEC = 90.0

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        super().__init__(account, config)
        self._gateway: Any = None
        self._task: Optional[asyncio.Task] = None
        self._started_at = 0.0

    def _mode(self) -> str:
        from src.integrations.qq_official import connect_mode, qqbot_cfg
        return connect_mode(qqbot_cfg(self.config))

    async def start(self) -> None:
        await super().start()
        self._started_at = time.time()
        if self._mode() != "websocket":
            self.detail = "webhook"
            return
        from src.integrations.qq_official import (
            QQBotGateway, handle_qqbot_event, intents_for, qqbot_cfg,
        )
        cfg = qqbot_cfg(self.config)
        creds = self._creds()
        account_id = self.account_id
        meta = _meta(self.account)
        config = self.config

        async def _on_event(payload: Dict[str, Any]) -> None:
            # 网关送达也算「平台事件到得了我们」——与 Webhook 同一本可达性台账
            try:
                from src.integrations.official_webhook_stats import record_event
                record_event("qqbot")
            except Exception:
                pass
            await handle_qqbot_event(payload, config=config, account_id=account_id, meta=meta)

        self._gateway = QQBotGateway(
            app_id=creds["app_id"], app_secret=creds["app_secret"],
            sandbox=bool(cfg.get("sandbox")), intents=intents_for(cfg), on_event=_on_event)
        self._task = asyncio.create_task(self._gateway.run())
        self.detail = "websocket"

    async def stop(self) -> None:
        if self._gateway is not None:
            try:
                self._gateway.stop()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await super().stop()

    async def healthy(self) -> bool:
        if not await super().healthy():
            return False
        if self._mode() != "websocket" or self._gateway is None:
            return True
        gw = self._gateway
        if getattr(gw, "fatal_code", 0):
            self.detail = str(getattr(gw, "last_error", "") or f"gateway fatal {gw.fatal_code}")
            return False
        if self._task is not None and self._task.done():
            self.detail = "gateway task exited"
            return False
        if getattr(gw, "connected", False):
            self.detail = "websocket"
            return True
        # 未连上：宽限期内算健康（首连/重连在途），超期仍未 READY 才如实报不健康
        if time.time() - self._started_at <= self.CONNECT_GRACE_SEC:
            return True
        self.detail = f"gateway not ready: {getattr(gw, 'last_error', '') or 'reconnecting'}"
        return bool(getattr(gw, "last_event_ts", 0) and
                    time.time() - float(gw.last_event_ts) < 600)

    async def delete_messages(self, chat_key: str, message_ids: List[str],
                              *, revoke: bool = True) -> Dict[str, Any]:
        """撤回机器人自己发的消息（编排器 ``delete_messages`` 统一签名；平台限 2 分钟内）。
        QQ 只有「对所有人撤回」一种语义，``revoke`` 仅为统一签名。逐条调用，单条失败不阻断。"""
        from src.integrations.qq_official import qqbot_recall_message
        ok_n, last_err = 0, ""
        for mid in (message_ids or []):
            if not str(mid or "").strip():
                continue
            res = await qqbot_recall_message(chat_key, str(mid), config=self.config,
                                             meta=_meta(self.account))
            if res.get("ok"):
                ok_n += 1
            else:
                last_err = str(res.get("error") or res.get("error_kind") or "")[:120]
        if ok_n <= 0:
            return {"ok": False, "reason": last_err or "recall_failed"}
        return {"ok": True, "deleted": ok_n}

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out["connect_mode"] = self._mode()
        if self._gateway is not None:
            try:
                out["gateway"] = self._gateway.status()
            except Exception:
                pass
        try:
            from src.integrations.qq_official import get_passive_ledger
            out["passive_ledger"] = get_passive_ledger().snapshot()
        except Exception:
            pass
        return out


def official_worker_factory(platform: str):
    """按平台选 worker 类：qqbot 走带 WS 网关的子类，其余用无状态基类。"""
    if str(platform or "").lower() == "qqbot":
        return lambda acc, cfg: QQBotOfficialWorker(acc, cfg)
    return lambda acc, cfg: OfficialApiWorker(acc, cfg)


def register_official_workers(config: Dict[str, Any]) -> None:
    """按需把官方 worker 注册进编排器（幂等、门控）。供 ensure_builtin_workers 调。"""
    from src.integrations.account_orchestrator import (
        get_worker_factory, register_worker,
    )
    for platform in OFFICIAL_PLATFORMS:
        if platform in DEDICATED_WORKER_PLATFORMS:
            continue
        try:
            if official_enabled(config, platform) and get_worker_factory(platform, "official") is None:
                register_worker(platform, "official", official_worker_factory(platform))
                logger.info("[orchestrator] 官方 worker 已注册: %s:official", platform)
        except Exception:
            logger.debug("[orchestrator] 注册 %s official worker 失败", platform, exc_info=True)


__all__ = [
    "OfficialApiWorker", "QQBotOfficialWorker", "official_worker_factory",
    "official_enabled", "official_pipeline_enabled",
    "register_official_workers", "dest_from_chat_key", "OFFICIAL_PLATFORMS",
    "DEDICATED_WORKER_PLATFORMS", "OFFICIAL_MEDIA_URL_PLATFORMS", "official_send_caps",
]
