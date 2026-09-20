# -*- coding: utf-8 -*-
"""微信客服（企业微信）worker：``mode=official`` 的有状态编排器成员（实施97 线 A）。

为什么不塞进 ``OfficialApiWorker``：那一族是**无状态 HTTP**（start/stop no-op、只出站，入站由各
webhook 自己镜像）；微信客服要**主动拉**（``sync_msg`` 游标）、要管 access_token 生命周期、要处理
会话状态机与 ``msg_send_fail`` 事件——是一条常驻循环，形态更像 ``TelegramProtocolWorker``。

生命周期（编排器契约：``start/stop/healthy/status/send[/send_media]``）：
- ``start``：校凭证 → 取 token（失败即抛，编排器落 error+退避，坐席在账号栏看得到原因）→
  解析 ``open_kfid``（缺则从 ``kf/account/list`` 取第一个可管理的客服账号）→ 起轮询任务。
- 轮询：``sync_once`` 按游标拉尽 ``has_more``；有货按 ``poll_interval_sec`` 紧跟，无货指数退避到
  ``idle_backoff_max_sec``（无回调 token 的拉取受平台严格频控，退避是必需品）；回调路由收到
  ``kf_msg_or_event`` 时 :meth:`kick` 带 10 分钟 token 立即唤醒一次。
- 入站：``origin=3`` 客户消息 → 记 ``record_inbound``（重开 48h/5 条窗口）→ 媒体落盘
  （``protocol_bridge.media_paths``）→ ``emit_incoming`` + ``maybe_auto_reply``（与 TG/LINE 同款）；
  ``origin=5`` 接待人员在企微客户端的回复 → 镜像为出站（人工口径，不计配额）；事件见
  :meth:`_handle_event`。
- 出站：``send``/``send_media`` → 成功即 ``record_sent``；**配额判定不在这里**——在编排器入口
  ``send_blocked``（kf_window_guard），本 worker 只负责记账，不做第二套裁决。
- 刻意**不实现** ``mark_read`` / ``send_chat_action``：微信客服 API 没有已读回执与 typing
  （协议层硬限制，能力矩阵口径），编排器 hasattr 判断自然跳过。

依赖注入（单测）：``client`` / ``state_store`` / ``emit`` / ``auto_reply`` / ``sleep`` / ``now``。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.integrations.wechat_kf import (
    FAIL_TYPE_LABELS, ORIGIN_CUSTOMER, ORIGIN_SERVICER, PLATFORM, STATE_CLOSED, STATE_HUMAN,
    WeChatKfClient, chat_key_for, external_userid_from_chat_key, media_ext_for,
    parse_sync_response,
)

logger = logging.getLogger(__name__)

_SEEN_MAX = 2000

#: 微信客服语音消息**只收 AMR**（≤2MB、≤60s）；全平台语音统一产 OGG/Opus，直传必失败。
_AMR_READY_EXTS = (".amr",)


def convert_voice_to_amr(path: str) -> Tuple[str, str]:
    """出站语音 → AMR-NB 临时文件（8kHz 单声道 12.2k）。返回 ``(临时路径或空串, 错误码)``。

    已是 .amr → ``("", "")`` 直传原文件；无 ffmpeg / 编码器不可用（随包 ffmpeg 可能没编
    libopencore_amrnb）→ ``("", "<原因>")``，调用方按 ``voice_format_unsupported`` 诚实失败——
    与 LINE 线 ``_convert_audio_for_line`` 的「失败按原格式发」不同：微信这边原格式必被拒，
    上传只是白烧一次请求。**调用方负责删除返回的临时文件。**
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in _AMR_READY_EXTS:
        return "", ""
    try:
        from src.utils.ffmpeg_resolver import ffmpeg_path
        ff = ffmpeg_path()
    except Exception:
        ff = None
    if not ff:
        return "", "ffmpeg_missing"
    import secrets
    import subprocess
    import tempfile
    dst = os.path.join(tempfile.gettempdir(), f"wxkf_voice_{secrets.token_hex(6)}.amr")
    try:
        r = subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
             "-c:a", "libopencore_amrnb", "-b:a", "12.2k", dst],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst, ""
        err = (getattr(r, "stderr", "") or "").strip()[:200]
        logger.warning("[wechat_kf] 语音转 AMR 失败 rc=%s：%s", getattr(r, "returncode", "?"), err)
        reason = "amr_encoder_unavailable" if "libopencore_amrnb" in err or "Unknown encoder" in err \
            else f"ffmpeg_failed:{err[:80]}"
    except Exception as exc:  # noqa: BLE001
        reason = f"ffmpeg_error:{type(exc).__name__}"
    try:
        os.remove(dst)
    except Exception:
        pass
    return "", reason


def wechat_kf_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        blk = (config or {}).get("wechat_kf")
        return dict(blk) if isinstance(blk, dict) else {}
    except Exception:
        return {}


def wechat_kf_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """开关：``platform_login.wechat_kf.enabled`` 或通道块 ``wechat_kf.enabled``（与 official_enabled 同构）。"""
    try:
        pl = ((config or {}).get("platform_login") or {}).get("wechat_kf") or {}
        if bool(pl.get("enabled")):
            return True
    except Exception:
        pass
    return bool(wechat_kf_cfg(config).get("enabled"))


def register_wechat_kf_worker(config: Dict[str, Any]) -> bool:
    """按需把本 worker 注册进编排器（幂等、门控）。供 ``ensure_builtin_workers`` 调。"""
    from src.integrations.account_orchestrator import get_worker_factory, register_worker
    if not wechat_kf_enabled(config):
        return False
    if get_worker_factory(PLATFORM, "official") is None:
        register_worker(PLATFORM, "official", lambda acc, cfg: WeChatKfWorker(acc, cfg))
        logger.info("[orchestrator] 微信客服 worker 已注册: %s:official", PLATFORM)
    return True


class WeChatKfWorker:
    """单客服账号（``open_kfid``）的收发 worker。"""

    def __init__(
        self,
        account: Dict[str, Any],
        config: Dict[str, Any],
        *,
        client: Optional[WeChatKfClient] = None,
        state_store: Any = None,
        emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
        auto_reply: Optional[Callable[[Dict[str, Any]], Any]] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.account = account or {}
        self.config = config or {}
        meta = dict(self.account.get("meta") or {})
        blk = wechat_kf_cfg(self.config)
        self.account_id = str(self.account.get("account_id") or "").strip() or "official"
        self.corpid = str(meta.get("corpid") or blk.get("corpid") or "").strip()
        self.secret = str(meta.get("secret") or blk.get("secret") or "").strip()
        kfid = str(meta.get("open_kfid") or blk.get("open_kfid") or "").strip()
        if not kfid and self.account_id != "official":
            kfid = self.account_id
        self.open_kfid = kfid
        self.poll_interval = self._clamp(blk.get("poll_interval_sec", 5.0), 2.0, 60.0)
        self.idle_backoff_max = self._clamp(blk.get("idle_backoff_max_sec", 30.0),
                                            self.poll_interval, 300.0)
        self.voice_format = 1 if str(blk.get("voice_format", "amr")).lower() == "silk" else 0
        self.welcome_text = str(blk.get("welcome_text") or "").strip()
        self._client = client or WeChatKfClient(self.corpid, self.secret)
        self._store = state_store
        self._emit = emit
        self._auto_reply = auto_reply
        self._sleep = sleep
        self._now = now
        self.state = "stopped"
        self.detail = ""
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._wake: Optional[asyncio.Event] = None
        self._pending_token = ""
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._customer_cache: Dict[str, Dict[str, str]] = {}
        self.consecutive_errors = 0
        self.stats: Dict[str, int] = {
            "polls": 0, "inbound": 0, "servicer_out": 0, "events": 0, "sent": 0,
            "send_fail_events": 0, "media_saved": 0, "errors": 0,
        }

    @staticmethod
    def _clamp(v: Any, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return lo

    # ── 依赖惰性解析 ──
    def _st(self):
        if self._store is None:
            from src.inbox.kf_window_guard import get_kf_state_store
            self._store = get_kf_state_store()
        return self._store

    def _do_emit(self, payload: Dict[str, Any]) -> None:
        fn = self._emit
        if fn is None:
            from src.integrations.protocol_bridge import emit_incoming
            fn = emit_incoming
        fn(payload)

    async def _do_auto_reply(self, payload: Dict[str, Any]) -> None:
        fn = self._auto_reply
        if fn is None:
            from src.integrations.protocol_bridge import maybe_auto_reply
            fn = maybe_auto_reply
        res = fn(payload)
        if hasattr(res, "__await__"):
            await res

    # ── 编排器契约 ──
    async def start(self) -> None:
        if not (self.corpid and self.secret):
            raise RuntimeError("微信客服缺少 corpid/secret（接入向导填写企微自建应用凭证）")
        tok = await self._client.get_token(force=True)
        if not tok.get("ok"):
            raise RuntimeError(f"微信客服取 token 失败: {tok.get('errmsg')}")
        if not self.open_kfid:
            await self._resolve_open_kfid()
        if not self.open_kfid:
            raise RuntimeError("微信客服未找到可管理的客服账号（open_kfid）——请在企微后台"
                               "「微信客服 → 通过 API 管理」勾选客服账号")
        self._stopping = False
        self._wake = asyncio.Event()
        self.state = "running"
        self.detail = ""
        self._task = asyncio.create_task(self._poll_loop())

    async def _resolve_open_kfid(self) -> None:
        res = await self._client.list_accounts()
        if not res.get("ok"):
            self.detail = str(res.get("errmsg") or "")
            return
        for acc in (res.get("data") or {}).get("account_list") or []:
            kfid = str((acc or {}).get("open_kfid") or "").strip()
            if kfid and int((acc or {}).get("manage_privilege", 1) or 0):
                self.open_kfid = kfid
                break
        if self.open_kfid:
            try:  # 记回注册表，下次启动不再查
                from src.integrations.account_registry import get_account_registry
                get_account_registry().upsert(PLATFORM, self.account_id,
                                              meta={"open_kfid": self.open_kfid}, merge_meta=True)
            except Exception:
                logger.debug("[wechat_kf] 回写 open_kfid 失败（忽略）", exc_info=True)

    async def stop(self) -> None:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        return self.state == "running" and self.consecutive_errors < 10

    def status(self) -> Dict[str, Any]:
        return {"type": "wechat_kf", "account_id": self.account_id, "open_kfid": self.open_kfid,
                "state": self.state, "detail": self.detail,
                "consecutive_errors": self.consecutive_errors, "stats": dict(self.stats)}

    def kick(self, token: str = "") -> None:
        """回调路由/运维：立即同步一次（带回调 token 可免频控）。"""
        if token:
            self._pending_token = str(token)
        if self._wake is not None:
            self._wake.set()

    # ── 轮询 ──
    async def _poll_loop(self) -> None:
        idle_rounds = 0
        while not self._stopping:
            token, self._pending_token = self._pending_token, ""
            try:
                got = await self.sync_once(token=token)
            except Exception:  # noqa: BLE001
                got = 0
                self.consecutive_errors += 1
                self.stats["errors"] += 1
                logger.debug("[wechat_kf] sync 异常", exc_info=True)
            if got > 0:
                idle_rounds = 0
                delay = self.poll_interval
            else:
                idle_rounds = min(idle_rounds + 1, 8)
                delay = min(self.idle_backoff_max, self.poll_interval * (1.6 ** idle_rounds))
            if self.consecutive_errors:
                delay = min(300.0, max(delay, self.poll_interval * (2 ** min(self.consecutive_errors, 6))))
            await self._wait(delay)

    async def _wait(self, delay: float) -> None:
        if self._wake is None:
            await self._sleep(delay)
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, float(delay)))
        except asyncio.TimeoutError:
            pass
        except Exception:
            await self._sleep(delay)

    async def sync_once(self, token: str = "") -> int:
        """拉一轮（拉尽 has_more）。返回处理的消息/事件条数；失败返回 0 并累计错误。"""
        st = self._st()
        cursor = st.get_cursor(self.account_id)
        handled = 0
        for _round in range(20):
            res = await self._client.sync_msg(self.open_kfid, cursor=cursor, token=token,
                                              voice_format=self.voice_format)
            self.stats["polls"] += 1
            if not res.get("ok"):
                self.consecutive_errors += 1
                self.stats["errors"] += 1
                self.detail = str(res.get("errmsg") or "sync_msg failed")[:200]
                return handled
            self.consecutive_errors = 0
            self.detail = ""
            items, next_cursor, has_more = parse_sync_response(res.get("data") or {})
            for item in items:
                try:
                    await self._handle(item)
                    handled += 1
                except Exception:  # noqa: BLE001
                    self.stats["errors"] += 1
                    logger.debug("[wechat_kf] 单条处理失败 %s", item.get("msgid"), exc_info=True)
            if next_cursor and next_cursor != cursor:
                cursor = next_cursor
                st.set_cursor(self.account_id, cursor)
            if not has_more:
                break
            token = ""  # 回调 token 只对首次拉取有意义
        return handled

    # ── 入站分发 ──
    def _seen_before(self, msgid: str) -> bool:
        if not msgid:
            return False
        if msgid in self._seen:
            return True
        self._seen[msgid] = self._now()
        while len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        return False

    async def _handle(self, item: Dict[str, Any]) -> None:
        kind = item.get("kind")
        if kind == "event":
            self.stats["events"] += 1
            await self._handle_event(item)
            return
        if kind != "message":
            return
        if self._seen_before(str(item.get("msgid") or "")):
            return
        origin = int(item.get("origin") or 0)
        uid = str(item.get("external_userid") or "")
        if not uid:
            return
        if origin == ORIGIN_CUSTOMER:
            self._st().record_inbound(self.account_id, uid, float(item.get("ts") or 0) or None)
            payload = await self._ingest_inbound(item)
            self.stats["inbound"] += 1
            self._do_emit(payload)
            await self._do_auto_reply(payload)
        elif origin == ORIGIN_SERVICER:
            payload = await self._ingest_servicer_outbound(item)
            self.stats["servicer_out"] += 1
            self._do_emit(payload)
        # origin=4（系统消息但非 event 类型）忽略

    async def _customer_profile(self, uid: str) -> Dict[str, str]:
        """客户昵称/头像（``kf/customer/batchget``，进程内缓存，best-effort）。"""
        if uid in self._customer_cache:
            return self._customer_cache[uid]
        prof: Dict[str, str] = {"name": "", "avatar_url": ""}
        try:
            res = await self._client.batch_get_customers([uid])
            if res.get("ok"):
                for c in (res.get("data") or {}).get("customer_list") or []:
                    if str((c or {}).get("external_userid") or "") == uid:
                        prof = {"name": str(c.get("nickname") or "").strip(),
                                "avatar_url": str(c.get("avatar") or "").strip()}
                        break
        except Exception:
            logger.debug("[wechat_kf] 客户资料拉取失败 uid=%s", uid, exc_info=True)
        self._customer_cache[uid] = prof
        if len(self._customer_cache) > 5000:
            self._customer_cache.pop(next(iter(self._customer_cache)))
        return prof

    async def _save_media(self, item: Dict[str, Any]) -> str:
        """下载 media_id → 落 protocol_media 根，返回 /static URL（失败空串）。"""
        media_id = str(item.get("media_id") or "")
        if not media_id:
            return ""
        res = await self._client.download_media(media_id)
        if not res.get("ok"):
            return ""
        data = res.get("data") or {}
        try:
            from src.integrations.protocol_bridge import media_paths
            ext = media_ext_for(str(data.get("content_type") or ""), str(item.get("media_type") or ""),
                                str(data.get("filename") or ""))
            dest, url = media_paths(PLATFORM, f"{self.account_id}_{item.get('msgid') or media_id}", ext)
            with open(dest, "wb") as fh:
                fh.write(bytes(data.get("bytes") or b""))
            self.stats["media_saved"] += 1
            return url
        except Exception:
            logger.debug("[wechat_kf] 媒体落盘失败", exc_info=True)
            return ""

    async def _ingest_inbound(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """客户消息 → ``emit_incoming`` 载荷（``media_type``/``media_ref`` 随消息走，AI 看得见图/语音）。"""
        from src.integrations.protocol_bridge import make_message
        uid = str(item.get("external_userid") or "")
        media_type = str(item.get("media_type") or "")
        media_ref = await self._save_media(item) if media_type else ""
        prof = await self._customer_profile(uid)
        source: Dict[str, Any] = {"message_id": str(item.get("msgid") or ""),
                                  "kf_origin": ORIGIN_CUSTOMER, "open_kfid": self.open_kfid}
        if item.get("menu_id"):
            source["menu_id"] = str(item.get("menu_id"))
        return make_message(
            platform=PLATFORM, account_id=self.account_id, chat_key=chat_key_for(uid),
            text=str(item.get("text") or ""), name=prof.get("name") or "",
            ts=float(item.get("ts") or 0), msg_id=str(item.get("msgid") or ""),
            direction="in", media_type=media_type, media_ref=media_ref,
            avatar_url=prof.get("avatar_url") or "", source=source,
        )

    async def _ingest_servicer_outbound(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """接待人员在企微客户端回的消息 → 出站镜像（人工口径；不计 API 配额）。"""
        from src.integrations.protocol_bridge import make_message
        uid = str(item.get("external_userid") or "")
        media_type = str(item.get("media_type") or "")
        media_ref = await self._save_media(item) if media_type else ""
        return make_message(
            platform=PLATFORM, account_id=self.account_id, chat_key=chat_key_for(uid),
            text=str(item.get("text") or ""), ts=float(item.get("ts") or 0),
            msg_id=str(item.get("msgid") or ""), direction="out",
            media_type=media_type, media_ref=media_ref,
            source={"message_id": str(item.get("msgid") or ""), "kf_origin": ORIGIN_SERVICER,
                    "sender_id": str(item.get("servicer_userid") or ""),
                    "sender_name": str(item.get("servicer_userid") or ""), "human_agent": True},
        )

    async def _handle_event(self, item: Dict[str, Any]) -> None:
        et = str(item.get("event_type") or "")
        ev = item.get("event") or {}
        uid = str(item.get("external_userid") or "")
        chat_key = chat_key_for(uid) if uid else ""
        if et == "msg_send_fail":
            ft = ev.get("fail_type")
            self.stats["send_fail_events"] += 1
            closed = self._st().record_fail(self.account_id, uid, ft) if uid else False
            logger.warning("[wechat_kf] 发送失败事件 acct=%s uid=%s fail_type=%s(%s) msgid=%s%s",
                           self.account_id, uid, ft, FAIL_TYPE_LABELS.get(int(ft) if str(ft).isdigit() else -1, "?"),
                           ev.get("fail_msgid"), " → 本轮窗口已关" if closed else "")
            fail_msgid = str(ev.get("fail_msgid") or "")
            if fail_msgid and chat_key:
                try:
                    from src.integrations.protocol_bridge import report_message_status
                    report_message_status(PLATFORM, self.account_id, chat_key, fail_msgid, "failed")
                except Exception:
                    pass
            return
        if et == "enter_session":
            code = str(ev.get("welcome_code") or "")
            if code and self.welcome_text and uid:
                res = await self._client.send_msg_on_event(code, self.welcome_text)
                if res.get("ok"):
                    try:
                        from src.integrations.protocol_bridge import make_message
                        self._do_emit(make_message(
                            platform=PLATFORM, account_id=self.account_id, chat_key=chat_key,
                            text=self.welcome_text, direction="out",
                            msg_id=str((res.get("data") or {}).get("msgid") or ""),
                            source={"kf_event": "welcome"}))
                    except Exception:
                        pass
            return
        if et in ("user_recall_msg", "servicer_recall_msg"):
            rid = str(ev.get("recall_msgid") or "")
            if rid and chat_key:
                try:
                    from src.integrations.protocol_bridge import report_deleted_messages
                    report_deleted_messages(PLATFORM, self.account_id, [rid], chat_key=chat_key)
                except Exception:
                    pass
            return
        if et == "session_status_change":
            logger.info("[wechat_kf] 会话状态变更 acct=%s uid=%s change_type=%s servicer=%s→%s",
                        self.account_id, uid, ev.get("change_type"),
                        ev.get("old_servicer_userid"), ev.get("new_servicer_userid"))
            return
        logger.debug("[wechat_kf] 事件 %s: %s", et, ev)

    # ── 出站 ──
    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        uid = external_userid_from_chat_key(chat_key)
        res = await self._client.send_text(uid, self.open_kfid, text)
        if not res.get("ok"):
            return {"delivered": False, "error": str(res.get("errmsg") or ""),
                    "error_kind": str(res.get("error_kind") or "api_error")}
        self._st().record_sent(self.account_id, uid)
        self.stats["sent"] += 1
        return {"delivered": True, "message_id": str((res.get("data") or {}).get("msgid") or "")}

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str,
                         caption: str = "") -> Dict[str, Any]:
        """图片/语音/视频/文件：先 ``media/upload`` 再发；配文另发一条文本（各占 1 条配额）。

        语音：全平台统一的 OGG/Opus 先转 AMR（微信客服硬要求），转不了就诚实失败
        （``error_kind=voice_format_unsupported``），上层语音链按既有口径回落文本。
        """
        uid = external_userid_from_chat_key(chat_key)
        mt = str(media_type or "").lower()
        upload_path = media_path
        tmp_amr = ""
        if mt in ("voice", "audio"):
            tmp_amr, conv_err = await asyncio.to_thread(convert_voice_to_amr, media_path)
            if conv_err:
                return {"delivered": False, "error_kind": "voice_format_unsupported",
                        "error": f"微信客服语音需 AMR，转码失败: {conv_err}"}
            upload_path = tmp_amr or media_path
        try:
            up = await self._client.upload_media(upload_path, media_type)
        finally:
            if tmp_amr:
                try:
                    os.remove(tmp_amr)
                except Exception:
                    pass
        if not up.get("ok"):
            return {"delivered": False, "error": str(up.get("errmsg") or ""),
                    "error_kind": str(up.get("error_kind") or "api_error")}
        media_id = str((up.get("data") or {}).get("media_id") or "")
        res = await self._client.send_media(uid, self.open_kfid, media_type, media_id)
        if not res.get("ok"):
            return {"delivered": False, "error": str(res.get("errmsg") or ""),
                    "error_kind": str(res.get("error_kind") or "api_error")}
        self._st().record_sent(self.account_id, uid)
        self.stats["sent"] += 1
        mid = str((res.get("data") or {}).get("msgid") or "")
        if str(caption or "").strip():
            cap = await self._client.send_text(uid, self.open_kfid, caption)
            if cap.get("ok"):
                self._st().record_sent(self.account_id, uid)
                self.stats["sent"] += 1
        return {"delivered": True, "message_id": mid}

    # ── 会话状态动作（UI 按钮 P1；先给出可调用面） ──
    async def transfer_to_human(self, chat_key: str, servicer_userid: str) -> Dict[str, Any]:
        uid = external_userid_from_chat_key(chat_key)
        return await self._client.trans_service_state(self.open_kfid, uid, STATE_HUMAN,
                                                      servicer_userid=servicer_userid)

    async def close_session(self, chat_key: str) -> Dict[str, Any]:
        uid = external_userid_from_chat_key(chat_key)
        return await self._client.trans_service_state(self.open_kfid, uid, STATE_CLOSED)

    async def session_state(self, chat_key: str) -> Dict[str, Any]:
        uid = external_userid_from_chat_key(chat_key)
        return await self._client.get_service_state(self.open_kfid, uid)


__all__ = ["WeChatKfWorker", "register_wechat_kf_worker", "wechat_kf_enabled", "wechat_kf_cfg",
           "convert_voice_to_amr"]
