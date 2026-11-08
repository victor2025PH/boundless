"""WhatsApp 真机闭环：电脑记，手机只收发。

入站先写入收件箱，再调用 decide 拿草稿，交给 wa.me 发送口。
生成回复只走 decide，不走技能管理器那条会直接执行的入口，避免两套生成各发一次。

``phone_loop.enabled`` 默认 false。没打开时，原有 dump 轮询保持原样。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.reply_history import load_reply_history
from src.integrations.whatsapp_rpa.phone_identity import (
    conversation_id,
    extract_phone,
    header_matches,
    lookup_phone_by_name,
    normalize_e164,
    phone_from_conversation_id,
    wa_me_url,
)
from src.integrations.whatsapp_rpa.phone_ledger import PhoneLedger, resolve_caps
from src.integrations.whatsapp_rpa.phone_notify import parse_wa_notifications
from src.integrations.whatsapp_rpa.phone_send import (
    PhoneDevice,
    PhoneScreen,
    deliver_via_wa_me,
    forward_page as _forward_page,
    outgoing_matches,
    texts_close,
)

logger = logging.getLogger(__name__)

DecideFn = Callable[..., Awaitable[Dict[str, Any]]]

_LOOPS: Dict[str, "WaPhoneLoop"] = {}

_STOP_KEYWORDS = (
    "stop",
    "unsubscribe",
    "do not contact",
    "别联系",
    "停止联系",
    "不要再发",
)

_CONVERT_KEYWORDS = (
    "已下单",
    "我下单了",
    "已付款",
    "付款了",
    "成交了",
    "i paid",
    "i ordered",
    "order placed",
)


def phone_loop_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    block = (cfg or {}).get("phone_loop")
    if not isinstance(block, dict):
        return False
    return bool(block.get("enabled", False))


def send_path(cfg: Optional[Dict[str, Any]]) -> str:
    """当前允许哪一条发送路径真的出手。

    ``phone``：只走号码闭环（wa.me + 截图核对）。此时不得再调用
    ``WhatsAppRpaRunner.run_once``、旧的主动续聊入队、自动通过好友，
    也不得把文字放进旧的 dump 发送队列。
    ``legacy``：维持原来的界面层级轮询。号码闭环不注册、不发送。
    """
    return "phone" if phone_loop_enabled(cfg) else "legacy"


def register_phone_loop(loop: "WaPhoneLoop") -> None:
    if loop.enabled:
        _LOOPS[str(loop.account_id)] = loop


def unregister_phone_loop(account_id: str) -> None:
    _LOOPS.pop(str(account_id or ""), None)


def registered_phone_loop(account_id: str) -> Optional["WaPhoneLoop"]:
    loop = _LOOPS.get(str(account_id or ""))
    if loop is None or not loop.enabled:
        return None
    return loop


def client_msg_id(source: str, phone: str, text: str, anchor: str = "") -> str:
    raw = f"{source}|{phone}|{text}|{anchor}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


_REPLY_LINE_LIMIT = 80
_REPLY_PARTS = 3


def reply_chunks(text: str, limit: int = _REPLY_LINE_LIMIT, parts: int = _REPLY_PARTS) -> List[str]:
    """按换行拆成多条短消息。一行太长就截断。一轮最多 3 段。"""
    chunks: List[str] = []
    cap = max(1, int(limit))
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) > cap:
            line = line[:cap]
        chunks.append(line)
        if len(chunks) >= max(1, int(parts)):
            break
    return chunks


def sent_content_key(source: str, phone: str, text: str) -> str:
    """同一来源、号码、正文的稳定号。不带 run id，用来认出已经发出去的那条。"""
    return client_msg_id("sent", phone, text, source)


def canonical_reply_language(raw: str) -> str:
    """注册表里的标准标签。空串表示没写。未知标签返回空串，不折成 zh。"""
    from src.integrations.whatsapp_rpa.reply_language import canonical_tag

    return canonical_tag(raw)


def text_is_stop(text: str, keywords: tuple = _STOP_KEYWORDS) -> bool:
    """明确拒收才停。弱抱怨（「有点烦」）不停，避免误伤正常聊天。"""
    body = str(text or "").strip()
    if not body:
        return False
    lowered = body.casefold()
    for word in keywords:
        token = str(word or "").strip()
        if token and token.casefold() in lowered:
            return True
    try:
        from src.integrations.whatsapp_rpa.intent_detector import create_detector
        found = create_detector({
            "strong_threshold": 0.85,
            "weak_threshold": 1.01,
            "enable_negative_check": True,
        }).detect(body)
        return bool(found.get("is_stop_contact")) and found.get("level") == "strong"
    except Exception:
        return False


def text_is_conversion(text: str, keywords: tuple = _CONVERT_KEYWORDS) -> str:
    lowered = str(text or "").casefold()
    for word in keywords:
        token = str(word or "").strip()
        if token and token.casefold() in lowered:
            return token
    return ""


def persona_separation_note(self_name: str, peer_name: str) -> str:
    """两台手机各说各的。对方刚报的名字不能拿来当自己的名字。"""
    you = str(self_name or "").strip() or "你自己"
    them = str(peer_name or "").strip() or "另一个人"
    return (
        f"你是{you}。对方是{them}，是另一个人。"
        f"对方叫{them}，你叫{you}。"
        f"绝不能说喊我{them}、叫我{them}、我是{them}，也不能把{you}和{them}并列成自己的名字。"
        "不要复述对方的自我介绍，不要把对方刚告诉你的名字当成自己的名字。"
    )


_SELF_NAME_VERBS = r"(?:就喊我|喊我|称呼我|叫我|我叫|我是)"


def claims_peer_identity(text: str, peer_names: Sequence[str]) -> bool:
    """把对方的名字当成自己的称呼。喊我、叫我、我是、我叫、称呼我，以及「或者」并列，都算。"""
    body = str(text or "")
    for name in peer_names or []:
        token = str(name or "").strip()
        if len(token) < 1:
            continue
        if re.search(_SELF_NAME_VERBS + r".{0,16}" + re.escape(token), body):
            return True
        if re.search(
            _SELF_NAME_VERBS + r".{0,30}或者\s*" + re.escape(token),
            body,
        ):
            return True
    return False


def peer_name_list(peer_name: str, last_inbound: str, self_name: str = "") -> List[str]:
    """配置的对方名字，加上对方刚自称的名字。自己的名字不算。"""
    names: List[str] = []
    own = str(self_name or "").strip()
    configured = str(peer_name or "").strip()
    if configured and configured != own:
        names.append(configured)
    for found in re.findall(
        r"(?:就喊我|喊我|称呼我|我叫|叫我|我是)\s*([\u4e00-\u9fffA-Za-z·]{1,8})",
        str(last_inbound or ""),
    ):
        if found and found != own and found not in names:
            names.append(found)
    return names


async def decide_whatsapp(
    app: Any,
    *,
    account_id: str,
    phone: str,
    last_inbound: str,
    persona_id: str = "",
    history_limit: int = 30,
    agent_instruction: str = "",
    self_name: str = "",
    peer_names: Optional[List[str]] = None,
    reply_lang: str = "",
) -> Dict[str, Any]:
    """给这部手机的回复。平台固定 whatsapp，历史来自收件箱。

    ``reply_lang`` 非空时原样传给 ``generate_persona_reply`` 的显式参数，
    对方正文、空文本和号码国码都不能改写它。认不出的码直接不生成。
    """
    from src.inbox.persona_reply import generate_persona_reply

    raw_lang = str(reply_lang or "").strip()
    forced = canonical_reply_language(raw_lang) if raw_lang else ""
    if raw_lang and not forced:
        return {"ok": False, "reply": "", "reason": "bad_reply_language", "reply_lang": ""}
    e164 = normalize_e164(phone)
    cid = conversation_id(account_id, e164)
    store = getattr(getattr(app, "state", None), "inbox_store", None)
    history = load_reply_history(store, cid, last_inbound, limit=history_limit)
    peers = [str(n) for n in (peer_names or []) if str(n or "").strip()]
    for claimed in re.findall(r"叫我\s*([\u4e00-\u9fffA-Za-z·]{1,8})", str(last_inbound or "")):
        if claimed and claimed not in peers and claimed != str(self_name or "").strip():
            peers.append(claimed)
    return await generate_persona_reply(
        app=app,
        platform="whatsapp",
        chat_key=e164,
        last_inbound=str(last_inbound or ""),
        history=history,
        persona_id=str(persona_id or ""),
        conversation_id=cid,
        account_id=str(account_id or ""),
        agent_instruction=str(agent_instruction or ""),
        guard_self_names=[str(self_name or "")] if str(self_name or "").strip() else None,
        guard_peer_names=peers or None,
        reply_lang=forced,
    )


async def route_registered_send(
    account_id: str,
    chat_key: str,
    text: str,
    *,
    source: str = "care",
) -> Optional[Dict[str, Any]]:
    """关怀 / 延迟队列的 WhatsApp 投递。没登记真机闭环时返回 None，走原来的边车。"""
    loop = registered_phone_loop(account_id)
    if loop is None:
        return None
    phone = normalize_e164(chat_key) or phone_from_conversation_id(chat_key)
    if not phone:
        phone = lookup_phone_by_name(loop.inbox, account_id, chat_key)
    if not phone:
        return {
            "delivered": False, "status": "failed", "reason": "phone_unresolved",
            "retry": False,
        }
    day = time.strftime("%Y%m%d", time.gmtime())
    anchor = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]
    result = await loop.send_prepared(
        phone=phone,
        text=str(text or ""),
        source=source,
        client_msg_id=client_msg_id(source, phone, str(text or ""), f"{anchor}:{day}"),
    )
    if result.get("status") == "capped":
        result["retry"] = True
    return result


class WaPhoneLoop:
    def __init__(
        self,
        *,
        account_id: str,
        inbox: Any,
        device: PhoneDevice,
        decide_fn: DecideFn,
        cfg: Optional[Dict[str, Any]] = None,
        ledger: Optional[PhoneLedger] = None,
        ledger_path: Optional[Path] = None,
        persona_id: str = "",
        now_fn: Optional[Callable[[], float]] = None,
        run_id: str = "",
    ) -> None:
        self.account_id = str(account_id or "").strip() or "default"
        self.inbox = inbox
        self.device = device
        self.decide_fn = decide_fn
        self.cfg = dict(cfg or {})
        self.persona_id = str(persona_id or self.cfg.get("persona_id") or "")
        self.run_id = str(run_id or "").strip()
        self._now = now_fn or time.time
        if ledger is not None:
            self.ledger = ledger
        else:
            path = ledger_path or Path("wa_phone_ledger.db")
            self.ledger = PhoneLedger(path)
        self.enabled = bool(self.cfg.get("enabled", False))
        self._taps: List[str] = []

    def scoped_msg_id(self, source: str, phone: str, text: str, anchor: str = "") -> str:
        """同一次运行里的消息号。换一次运行就换号，失败没点出去的可以重试。"""
        tag = str(anchor or "")
        if self.run_id:
            tag = f"{tag}|{self.run_id}"
        return client_msg_id(source, phone, text, tag)

    def caps(self) -> Dict[str, int]:
        return resolve_caps(self.cfg)

    async def run_once(self) -> Dict[str, Any]:
        notes = await self._poll_notifications()
        handled: List[Dict[str, Any]] = []
        for note in notes:
            handled.append(await self.ingest_notification(note))
        if self.cfg.get("first_touch_enabled"):
            handled.extend(await self.drain_first_touch())
        if self.cfg.get("wake_enabled"):
            handled.extend(await self.wake_silent())
        inbound = [row for row in handled if row.get("direction") == "in"]
        return {
            "ok": True,
            "step": "phone_loop",
            "peer_text": (inbound[-1].get("text") if inbound else "") or "",
            "handled": handled,
        }

    async def _poll_notifications(self) -> List[Dict[str, str]]:
        poll = getattr(self.device, "poll_notifications", None)
        if poll is None:
            return []
        try:
            raw = await poll()
        except Exception:
            logger.debug("[wa_phone] 读通知失败", exc_info=True)
            return []
        if isinstance(raw, str):
            return parse_wa_notifications(raw)
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
        return []

    async def ingest_notification(self, note: Dict[str, str]) -> Dict[str, Any]:
        """通知给出发送者。打开该号码的会话后，顶栏对上才入库。"""
        sender = str(note.get("sender") or "").strip()
        text = str(note.get("text") or "").strip()
        phone = extract_phone(sender, str(note.get("phone") or ""))
        if not phone:
            phone = lookup_phone_by_name(self.inbox, self.account_id, sender)
        if not phone:
            return {"ok": False, "reason": "phone_unresolved", "sender": sender}
        url = wa_me_url(phone)
        await self.device.open_url(url)
        screen = await self.device.read_screen(await self.device.screencap())
        if not isinstance(screen, PhoneScreen):
            screen = PhoneScreen()
        if not header_matches(screen.header, phone=phone, sender=sender):
            try:
                await self.device.press_back()
            except Exception:
                pass
            return {
                "ok": False, "reason": "header_mismatch", "phone": phone, "sender": sender,
            }
        body = text or str(screen.last_incoming or "").strip()
        if not body:
            return {"ok": False, "reason": "empty_text", "phone": phone}
        return await self.handle_inbound(
            phone=phone, text=body, display_name=sender or screen.header,
            msg_id=str(note.get("msg_id") or ""),
        )

    async def handle_inbound(
        self,
        *,
        phone: str,
        text: str,
        display_name: str = "",
        msg_id: str = "",
    ) -> Dict[str, Any]:
        e164 = normalize_e164(phone)
        cid = conversation_id(self.account_id, e164)
        if not e164 or not cid:
            return {"ok": False, "reason": "phone_unresolved"}
        body = str(text or "").strip()
        now = float(self._now())
        self._write_message(
            cid, e164, display_name or e164, direction="in", text=body,
            platform_msg_id=msg_id or f"in-{client_msg_id('in', e164, body, str(int(now)))}",
            ts=now,
        )
        result: Dict[str, Any] = {
            "ok": True, "direction": "in", "phone": e164,
            "conversation_id": cid, "text": body,
        }
        keyword = text_is_conversion(body, self._convert_keywords())
        if keyword:
            result["conversion"] = self.flag_conversion(e164, keyword)
        if text_is_stop(body, self._stop_keywords()):
            self.ledger.stop(e164, reason="inbound_stop", sample=body, now=now)
            result["stopped"] = True
            result["reply"] = "skipped"
            return result
        if self.ledger.is_stopped(e164):
            result["stopped"] = True
            result["reply"] = "skipped"
            return result
        mode = str(self.cfg.get("reply_mode") or "auto")
        if mode == "off":
            result["reply"] = "off"
            return result
        noted = self.ledger.note_inbound(e164, body, now=now)
        if not noted.get("unreplied"):
            result["reply"] = "skipped"
            result["reason"] = "already_replied"
            result["send"] = {
                "status": "skipped", "reason": "already_replied",
                "tapped": False, "delivered": False,
            }
            return result
        decide_kwargs = {
            "account_id": self.account_id,
            "phone": e164,
            "last_inbound": body,
            "persona_id": self.persona_id,
        }
        policy = dict(self.cfg.get("reply_language_policy") or {})
        strategy = str(policy.get("strategy") or "").strip()
        if strategy:
            from src.integrations.whatsapp_rpa.reply_language import resolve_reply_policy

            decision = resolve_reply_policy(
                strategy,
                requested=str(policy.get("requested") or ""),
                peer_text=body,
                registry=policy.get("registry"),
            )
            result["reply_language"] = decision
            if not decision.get("apply"):
                result["reply"] = "silent"
                result["reason"] = str(decision.get("reason") or "reply_language_blocked")
                result["send"] = {
                    "status": "skipped",
                    "reason": result["reason"],
                    "tapped": False,
                    "delivered": False,
                    "retry": False,
                    "reply_language": decision,
                }
                return result
            decide_kwargs["reply_lang"] = str(decision.get("resolved") or "")
        try:
            draft = await self.decide_fn(**decide_kwargs)
        except Exception:
            result["reply"] = "silent"
            result["reason"] = "decide_failed"
            result["send"] = {
                "status": "skipped", "reason": "decide_failed",
                "tapped": False, "delivered": False, "retry": False,
            }
            return result
        reply = str((draft or {}).get("reply") or "").strip()
        if not (draft or {}).get("ok") or not reply:
            result["reply"] = "silent"
            return result
        if mode == "approve":
            result["reply"] = "pending"
            result["draft"] = reply
            return result
        chunks = reply_chunks(reply)
        sends = []
        anchor = msg_id or body
        for index, chunk in enumerate(chunks):
            sent = await self.send_prepared(
                phone=e164,
                text=chunk,
                source="reply",
                client_msg_id=self.scoped_msg_id("reply", e164, chunk, f"{anchor}:{index}"),
                display_name=display_name,
                stay=index > 0,
            )
            sends.append(sent)
            if sent.get("status") != "sent":
                break
        result["sends"] = sends
        result["send"] = sends[-1] if len(sends) == 1 else {
            "status": "sent" if sends and all(row.get("status") == "sent" for row in sends) else (sends[-1].get("status") if sends else "failed"),
            "parts": sends,
            "delivered": bool(sends) and all(row.get("status") == "sent" for row in sends),
            "tapped": any(row.get("tapped") for row in sends),
        }
        if len(sends) == 1:
            result["send"] = sends[0]
        if any(row.get("status") == "sent" for row in sends):
            self.ledger.mark_replied(e164, body)
        return result

    async def send_prepared(
        self,
        *,
        phone: str,
        text: str,
        source: str,
        client_msg_id: str,
        display_name: str = "",
        dry_run: Optional[bool] = None,
        stay: bool = False,
        finish_pending: bool = False,
    ) -> Dict[str, Any]:
        """回复、队列、关怀、首触、唤醒都从这里出去。"""
        e164 = normalize_e164(phone)
        cid = conversation_id(self.account_id, e164)
        body = str(text or "").strip()
        now = float(self._now())
        base = {
            "phone": e164, "conversation_id": cid, "source": source,
            "client_msg_id": client_msg_id, "delivered": False,
        }
        if not e164 or not cid or not body or not client_msg_id:
            return {**base, "status": "failed", "reason": "bad_target", "retry": False}
        if self.ledger.is_stopped(e164):
            return {**base, "status": "stopped", "reason": "stopped", "retry": False}
        if source == "first_touch" and self.ledger.has_first_touch(e164):
            self.ledger.finish_touch(e164, "skipped")
            return {**base, "status": "skipped", "reason": "already_touched", "retry": False}
        if self.ledger.pause_until() > now:
            return {**base, "status": "throttled", "reason": "device_pause", "retry": True}
        # 对方回过消息就不按条数封顶。从没回过的冷启动才吃上限。
        # 画面上出现 WhatsApp 的频繁提示时，两种会话都先停一段时间。
        cold = not self._peer_has_replied(e164)
        if cold:
            caps = self.caps()
            hour_count = self.ledger.count_sent_since(now - 3600, cold_only=True)
            day_count = self.ledger.count_sent_since(now - 86400, cold_only=True)
            if hour_count >= caps["hourly_cap"] or day_count >= caps["daily_cap"]:
                which = "hourly_cap" if hour_count >= caps["hourly_cap"] else "daily_cap"
                return {**base, "status": "capped", "reason": which, "retry": True}
        content_key = sent_content_key(source, e164, body)
        existing = self.ledger.get_send(client_msg_id)
        if existing is not None:
            previous = str(existing.get("status") or "duplicate")
            return {
                **base, "status": previous, "reason": "duplicate",
                "delivered": previous == "sent", "tapped": False, "retry": False,
            }
        try:
            fresh = await self.device.read_screen(await self.device.screencap())
        except Exception:
            fresh = None
        if fresh is not None and outgoing_matches(fresh, body):
            if content_key:
                self.ledger.mark_content_sent(content_key, now=now)
            return {
                **base, "status": "sent", "reason": "already_visible",
                "delivered": True, "tapped": False, "retry": False,
            }
        if self.ledger.has_seen_text(e164, body, content_key):
            return {
                **base, "status": "skipped", "reason": "already_recorded",
                "delivered": False, "tapped": False, "retry": False,
            }
        if fresh is not None and _forward_page(fresh):
            back = getattr(self.device, "press_back", None)
            if back is not None:
                try:
                    await back()
                except Exception:
                    pass
            return {
                **base, "status": "skipped", "reason": "forward_page",
                "delivered": False, "tapped": False, "retry": False,
            }
        pending_ok = False
        if finish_pending:
            for row in self.ledger.unsent_rows(e164):
                saved = str(row.get("body") or "")
                if saved and texts_close(saved, body):
                    pending_ok = True
                    break
                if content_key and str(row.get("content_key") or "") == content_key:
                    pending_ok = True
                    break
        if (
            source == "reply"
            and self.ledger.tracks_inbound(e164)
            and not self.ledger.has_unreplied(e164)
            and not pending_ok
        ):
            return {
                **base, "status": "skipped", "reason": "no_new_inbound",
                "tapped": False, "retry": False, "delivered": False,
            }
        slot = self.ledger.reserve(
            client_msg_id=client_msg_id, phone=e164, account_id=self.account_id,
            source=source, now=now, cold=cold, content_key=content_key, body=body,
        )
        if slot.get("duplicate"):
            previous = (slot.get("row") or {}).get("status") or "duplicate"
            return {
                **base, "status": previous, "reason": "duplicate",
                "delivered": previous == "sent", "tapped": False, "retry": False,
            }
        is_dry = self.cfg.get("dry_run") if dry_run is None else dry_run
        outcome = await deliver_via_wa_me(
            self.device, e164, body, dry_run=bool(is_dry), stay=stay,
        )
        if outcome.status == "throttled":
            self.ledger.release(client_msg_id)
            pause = self._pause_seconds()
            self.ledger.set_pause(now + pause, outcome.reason)
            return {
                **base, "status": "throttled", "reason": outcome.reason,
                "retry": True, "tapped": False, "delivered": False,
            }
        self.ledger.finish(
            client_msg_id, outcome.status, error=outcome.reason, now=now,
        )
        if outcome.status == "sent" and content_key:
            self.ledger.mark_content_sent(content_key, now=now)
        if outcome.tapped:
            self._taps.append(client_msg_id)
        if outcome.status == "sent":
            self._write_message(
                cid, e164, display_name or e164, direction="out", text=body,
                platform_msg_id=f"out-{client_msg_id}", ts=now, sent_by="ai",
            )
            self._note_outreach(cid, source, "sent", client_msg_id, now)
            if source == "first_touch":
                self.ledger.mark_first_touch(
                    e164, conversation_id=cid, client_msg_id=client_msg_id, now=now,
                )
                self.ledger.finish_touch(e164, "done")
        elif source == "first_touch" and outcome.status == "failed":
            self.ledger.finish_touch(e164, "failed")
        sent = dict(base)
        sent.update(outcome.as_dict())
        sent["retry"] = False
        return sent

    def enqueue_first_touch(self, phone: str, opener: str) -> Dict[str, Any]:
        e164 = normalize_e164(phone)
        cid = conversation_id(self.account_id, e164)
        text = str(opener or "").strip()
        if not e164 or not cid or not text:
            return {"ok": False, "reason": "bad_target"}
        if self.ledger.has_first_touch(e164):
            return {"ok": False, "reason": "already_touched", "phone": e164}
        now = float(self._now())
        self._ensure_conversation(cid, e164, e164, now)
        self.ledger.enqueue_touch(
            e164, account_id=self.account_id, opener=text, now=now,
        )
        self._note_outreach(cid, "first_touch", "planned", text[:80], now)
        return {"ok": True, "phone": e164, "conversation_id": cid}

    async def drain_first_touch(self, limit: int = 5) -> List[Dict[str, Any]]:
        self._import_planned_outreach()
        out: List[Dict[str, Any]] = []
        for row in self.ledger.pending_touches(limit):
            phone = str(row.get("phone") or "")
            opener = str(row.get("opener") or "")
            sent = await self.send_prepared(
                phone=phone,
                text=opener,
                source="first_touch",
                client_msg_id=self.scoped_msg_id("first_touch", phone, opener, "once"),
            )
            out.append(sent)
            if sent.get("status") == "capped":
                break
        return out

    async def wake_silent(self) -> List[Dict[str, Any]]:
        wait = float(self.cfg.get("wake_after_sec") or 4 * 3600)
        text = str(self.cfg.get("wake_text") or "最近还好吗").strip()
        now = float(self._now())
        out: List[Dict[str, Any]] = []
        try:
            rows = self.inbox.list_conversations(
                platform="whatsapp", account_id=self.account_id, limit=100,
            )
        except Exception:
            return out
        for row in rows or []:
            phone = normalize_e164(str(row.get("chat_key") or "")) or normalize_e164(
                str(row.get("phone") or "")
            )
            if not phone or self.ledger.is_stopped(phone):
                continue
            last_ts = float(row.get("last_ts") or 0)
            if last_ts <= 0 or now - last_ts < wait:
                continue
            day = time.strftime("%Y%m%d", time.gmtime(now))
            sent = await self.send_prepared(
                phone=phone,
                text=text,
                source="silence",
                client_msg_id=self.scoped_msg_id("silence", phone, text, day),
                display_name=str(row.get("display_name") or ""),
            )
            out.append(sent)
            if sent.get("status") == "capped":
                break
        return out

    def flag_conversion(self, phone: str, keyword: str) -> Dict[str, Any]:
        """关键词只记待确认，不写成交。坐席点确认才走 mark_deal。"""
        e164 = normalize_e164(phone)
        cid = conversation_id(self.account_id, e164)
        if not e164 or not cid:
            return {"ok": False, "reason": "phone_unresolved"}
        now = float(self._now())
        fresh = self.ledger.mark_pending_deal(
            e164, conversation_id=cid, keyword=keyword, now=now,
        )
        if fresh:
            self._note_outreach(cid, "conversion", "pending_confirm", keyword, now)
        return {
            "ok": True,
            "status": "pending_confirm",
            "keyword": keyword,
            "conversation_id": cid,
            "fresh": fresh,
        }

    def _peer_has_replied(self, phone: str) -> bool:
        cid = conversation_id(self.account_id, phone)
        if not cid or not hasattr(self.inbox, "list_recent_messages"):
            return False
        try:
            try:
                rows = self.inbox.list_recent_messages(
                    cid, limit=40, include_deleted=False,
                )
            except TypeError:
                rows = self.inbox.list_recent_messages(cid, limit=40)
        except Exception:
            return False
        for row in rows or []:
            if str(row.get("direction") or "") in {"in", "inbound"}:
                return True
        return False

    def _pause_seconds(self) -> float:
        try:
            seconds = float(self.cfg.get("rate_limit_pause_sec") or 1800)
        except (TypeError, ValueError):
            seconds = 1800
        if seconds <= 0:
            seconds = 1800
        return seconds

    def mark_deal(
        self,
        phone: str,
        *,
        source: str = "manual",
        note: str = "",
        amount: float = 0.0,
        by: str = "",
    ) -> Dict[str, Any]:
        e164 = normalize_e164(phone)
        cid = conversation_id(self.account_id, e164)
        if not cid:
            return {"ok": False, "reason": "phone_unresolved"}
        self._ensure_conversation(cid, e164, e164, float(self._now()))
        if source != "manual":
            return self.flag_conversion(e164, str(note or source))
        try:
            from src.inbox.journey_stage import record_deal
            deal = record_deal(
                self.inbox, cid, amount=float(amount or 0), note=str(note or ""),
                recorded_by=str(by or source), source="manual",
            )
        except Exception:
            logger.debug("[wa_phone] record_deal 失败", exc_info=True)
            return {"ok": False, "reason": "record_deal_failed", "conversation_id": cid}
        self.ledger.confirm_pending_deal(e164)
        self._note_outreach(
            cid, "conversion", "converted", str(note or source)[:80], float(self._now()),
        )
        deal = dict(deal or {})
        deal["conversation_id"] = cid
        return deal

    def _convert_keywords(self) -> tuple:
        raw = self.cfg.get("conversion_keywords")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(item).strip() for item in raw if str(item).strip())
        return _CONVERT_KEYWORDS

    def _stop_keywords(self) -> tuple:
        raw = self.cfg.get("stop_keywords")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(item).strip() for item in raw if str(item).strip())
        return _STOP_KEYWORDS

    def _import_planned_outreach(self) -> None:
        if not hasattr(self.inbox, "list_outreach_recent"):
            return
        try:
            rows = self.inbox.list_outreach_recent(days=36500, limit=500)
        except Exception:
            return
        for row in rows or []:
            if str(row.get("platform") or "") != "whatsapp":
                continue
            if str(row.get("status") or "") != "planned":
                continue
            account = str(row.get("account_id") or "")
            if account and account != self.account_id:
                continue
            phone = phone_from_conversation_id(str(row.get("conversation_id") or ""))
            opener = str(row.get("note") or "").strip()
            if phone and opener and not self.ledger.has_first_touch(phone):
                self.ledger.enqueue_touch(
                    phone, account_id=self.account_id, opener=opener,
                )

    def _ensure_conversation(self, cid: str, phone: str, name: str, ts: float) -> None:
        if not hasattr(self.inbox, "upsert_conversation"):
            return
        self.inbox.upsert_conversation(InboxConversation(
            conversation_id=cid,
            platform="whatsapp",
            account_id=self.account_id,
            chat_key=phone,
            display_name=name or phone,
            phone=phone,
            last_text="",
            last_ts=ts,
        ))

    def _write_message(
        self,
        cid: str,
        phone: str,
        name: str,
        *,
        direction: str,
        text: str,
        platform_msg_id: str,
        ts: float,
        sent_by: str = "",
    ) -> None:
        self._ensure_conversation(cid, phone, name, ts)
        if not hasattr(self.inbox, "ingest_message"):
            return
        self.inbox.ingest_message(InboxMessage(
            conversation_id=cid,
            platform_msg_id=platform_msg_id,
            direction=direction,
            text=text,
            ts=ts,
            sent_by=sent_by,
        ))
        if hasattr(self.inbox, "upsert_conversation"):
            self.inbox.upsert_conversation(InboxConversation(
                conversation_id=cid,
                platform="whatsapp",
                account_id=self.account_id,
                chat_key=phone,
                display_name=name or phone,
                phone=phone,
                last_text=text,
                last_ts=ts,
                unread=1 if direction == "in" else 0,
            ))

    def _note_outreach(
        self, cid: str, source: str, status: str, note: str, ts: float,
    ) -> None:
        if not hasattr(self.inbox, "record_outreach"):
            return
        try:
            self.inbox.record_outreach(
                cid,
                batch_id=f"wa_phone:{source}"[:64],
                platform="whatsapp",
                account_id=self.account_id,
                status=status,
                note=str(note or "")[:200],
                ts=ts,
            )
        except Exception:
            logger.debug("[wa_phone] outreach 写入失败", exc_info=True)
