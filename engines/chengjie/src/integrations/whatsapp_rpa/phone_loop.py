"""WhatsApp 真机闭环：电脑记，手机只收发。

入站先写入收件箱，再调用 decide 拿草稿，交给 wa.me 发送口。
生成回复只走 decide，不走技能管理器那条会直接执行的入口，避免两套生成各发一次。

``phone_loop.enabled`` 默认 false。没打开时，原有 dump 轮询保持原样。
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

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


async def decide_whatsapp(
    app: Any,
    *,
    account_id: str,
    phone: str,
    last_inbound: str,
    persona_id: str = "",
    history_limit: int = 30,
) -> Dict[str, Any]:
    """给这部手机的回复。平台固定 whatsapp，历史来自收件箱。"""
    from src.inbox.persona_reply import generate_persona_reply

    e164 = normalize_e164(phone)
    cid = conversation_id(account_id, e164)
    store = getattr(getattr(app, "state", None), "inbox_store", None)
    history = load_reply_history(store, cid, last_inbound, limit=history_limit)
    return await generate_persona_reply(
        app=app,
        platform="whatsapp",
        chat_key=e164,
        last_inbound=str(last_inbound or ""),
        history=history,
        persona_id=str(persona_id or ""),
        conversation_id=cid,
        account_id=str(account_id or ""),
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
    ) -> None:
        self.account_id = str(account_id or "").strip() or "default"
        self.inbox = inbox
        self.device = device
        self.decide_fn = decide_fn
        self.cfg = dict(cfg or {})
        self.persona_id = str(persona_id or self.cfg.get("persona_id") or "")
        self._now = now_fn or time.time
        if ledger is not None:
            self.ledger = ledger
        else:
            path = ledger_path or Path("wa_phone_ledger.db")
            self.ledger = PhoneLedger(path)
        self.enabled = bool(self.cfg.get("enabled", False))
        self._taps: List[str] = []

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
            deal = self.mark_deal(e164, source="keyword", note=keyword)
            result["deal"] = deal
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
        draft = await self.decide_fn(
            account_id=self.account_id,
            phone=e164,
            last_inbound=body,
            persona_id=self.persona_id,
        )
        reply = str((draft or {}).get("reply") or "").strip()
        if not (draft or {}).get("ok") or not reply:
            result["reply"] = "silent"
            return result
        if mode == "approve":
            result["reply"] = "pending"
            result["draft"] = reply
            return result
        sent = await self.send_prepared(
            phone=e164,
            text=reply,
            source="reply",
            client_msg_id=client_msg_id("reply", e164, reply, msg_id or body),
            display_name=display_name,
        )
        result["send"] = sent
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
        caps = self.caps()
        hour_count = self.ledger.count_sent_since(now - 3600)
        day_count = self.ledger.count_sent_since(now - 86400)
        if hour_count >= caps["hourly_cap"] or day_count >= caps["daily_cap"]:
            which = "hourly_cap" if hour_count >= caps["hourly_cap"] else "daily_cap"
            return {**base, "status": "capped", "reason": which, "retry": True}
        slot = self.ledger.reserve(
            client_msg_id=client_msg_id, phone=e164, account_id=self.account_id,
            source=source, now=now,
        )
        if slot.get("duplicate"):
            previous = (slot.get("row") or {}).get("status") or "duplicate"
            return {
                **base, "status": previous, "reason": "duplicate",
                "delivered": previous == "sent", "tapped": False, "retry": False,
            }
        is_dry = self.cfg.get("dry_run") if dry_run is None else dry_run
        outcome = await deliver_via_wa_me(
            self.device, e164, body, dry_run=bool(is_dry),
        )
        self.ledger.finish(
            client_msg_id, outcome.status, error=outcome.reason, now=now,
        )
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
                client_msg_id=client_msg_id("first_touch", phone, opener, "once"),
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
                client_msg_id=client_msg_id("silence", phone, text, day),
                display_name=str(row.get("display_name") or ""),
            )
            out.append(sent)
            if sent.get("status") == "capped":
                break
        return out

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
            try:
                if int(self.inbox.count_active_deals(cid)) > 0:
                    return {"ok": True, "reason": "already_recorded", "conversation_id": cid}
            except Exception:
                pass
        try:
            from src.inbox.journey_stage import record_deal
            deal = record_deal(
                self.inbox, cid, amount=float(amount or 0), note=str(note or ""),
                recorded_by=str(by or source), source=source,
            )
        except Exception:
            logger.debug("[wa_phone] record_deal 失败", exc_info=True)
            return {"ok": False, "reason": "record_deal_failed", "conversation_id": cid}
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
