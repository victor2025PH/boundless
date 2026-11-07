"""WhatsApp 真机 RPA 与统一收件箱的最小闭环。

会话键与收件箱一致：``whatsapp:{account}:{号码}``。显示名只做标签。
拿不到号码时用 ``whatsapp:{account}:name:{slug}``，并标 ``phone_unresolved``。

自动发送入口只有 ``prepare_outbound`` / ``finish_outbound``：
队列和自动回复都走这里。``client_msg_id`` 重复不会第二次触碰设备。
``dry_run`` 记成 ``dry_run``，不是 ``sent``。只有发出后重新 dump 能看到
己方气泡才记 ``sent``。双勾 / 蓝勾读不到时，回执停在 ``sent``，不报
``delivered`` / ``read``。

``stop_gate_enabled`` 默认开：对方明确 STOP / 已在拒收黑名单 / 静默期内，
所有发出入口都不发。关掉开关后这些拒收条件不再挡发送。

收件箱 ``automation_mode``（人工接管、审批档）故意不参与发送判断。
RPA 自己的 ``reply_mode`` 仍约束自动口（unread / 补扫 / 截屏兜底 / 补漏 /
主动续聊）：``off`` 与 ``approve`` 不触碰设备。``send_queue`` 是已经决定
要发的队列，不受 reply_mode 挡，但仍受 STOP 开关挡。
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.integrations.whatsapp_rpa import ui_hierarchy as ui

KEY_SOURCE_PHONE = "phone"
KEY_SOURCE_FALLBACK = "display_name_fallback"
FALLBACK_MARKER = "phone_unresolved"

DEFAULT_STOP_KEYWORDS: Tuple[str, ...] = (
    "stop",
    "unsubscribe",
    "do not contact",
    "别联系",
    "停止联系",
    "不要再发",
)

# 自动口：reply_mode=off/approve 时不发。发送队列不在此列。
AUTO_REPLY_SOURCES = frozenset({
    "unread",
    "open_thread",
    "screencap",
    "catchup",
    "proactive",
})

_READ_MARKERS = ("已读", "蓝勾", "blue tick", "message read")
_DELIVERED_MARKERS = ("已送达", "双勾", "double check", "delivered")
_PHONE_CANDIDATE_RE = re.compile(r"(?:\+|00)?\d[\d\s().-]{6,}\d")
_READ_WORD_RE = re.compile(r"\bread\b", re.IGNORECASE)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def stop_gate_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """缺省开启。只有显式关掉才放行 STOP / 黑名单 / 静默。"""
    if not isinstance(cfg, dict) or "stop_gate_enabled" not in cfg:
        return True
    return _as_bool(cfg.get("stop_gate_enabled"), True)


def stop_keywords(cfg: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    raw = (cfg or {}).get("stop_contact_keywords")
    if isinstance(raw, (list, tuple)) and raw:
        words = tuple(str(w).strip() for w in raw if str(w).strip())
        if words:
            return words
    return DEFAULT_STOP_KEYWORDS


def text_is_stop(text: str, keywords: Sequence[str] = DEFAULT_STOP_KEYWORDS) -> bool:
    low = (text or "").lower()
    if not low.strip():
        return False
    for pat in keywords:
        p = (pat or "").strip().lower()
        if p and p in low:
            return True
    return False


def auto_reply_allowed(source: str, reply_mode: str) -> bool:
    """发送队列不受 reply_mode 限制。其余自动口在 off/approve 时不发。"""
    if source == "send_queue":
        return True
    mode = (reply_mode or "auto").strip().lower()
    if source in AUTO_REPLY_SOURCES and mode in {"off", "approve"}:
        return False
    return True


def normalize_phone(raw: str) -> str:
    """抽出 8–15 位号码。带格式的 ``+63 917 555 0101`` 收成数字串。"""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00") and len(digits) > 8:
        digits = digits[2:]
    if 8 <= len(digits) <= 15:
        return digits
    return ""


def _phones_in_text(raw: str) -> List[str]:
    found: List[str] = []
    for match in _PHONE_CANDIDATE_RE.finditer(raw or ""):
        phone = normalize_phone(match.group(0))
        if phone and phone not in found:
            found.append(phone)
    direct = normalize_phone(raw or "")
    if direct and direct not in found:
        found.insert(0, direct)
    return found


def slugify_display_name(name: str) -> str:
    raw = re.sub(r"\s+", "_", (name or "").strip())
    raw = re.sub(r"[^\w\u4e00-\u9fff]+", "", raw, flags=re.UNICODE)
    raw = raw.strip("_")
    return (raw[:48] or "unknown")


def make_client_msg_id(
    conversation_id: str,
    peer_text: str,
    source: str,
    reply_text: str,
) -> str:
    digest = hashlib.sha256(
        f"{conversation_id}|{source}|{peer_text}|{reply_text}".encode("utf-8")
    ).hexdigest()[:20]
    return f"warpa-{digest}"


@dataclass(frozen=True)
class PeerIdentity:
    account_id: str
    display_name: str
    phone: str
    conversation_id: str
    chat_key: str
    key_source: str
    phone_unresolved: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "phone": self.phone,
            "conversation_id": self.conversation_id,
            "chat_key": self.chat_key,
            "key_source": self.key_source,
            "phone_unresolved": self.phone_unresolved,
            FALLBACK_MARKER: self.phone_unresolved,
        }


def _phone_identity(account: str, phone: str, display_name: str) -> PeerIdentity:
    label = display_name.strip() or phone
    return PeerIdentity(
        account_id=account,
        display_name=label,
        phone=phone,
        conversation_id=f"whatsapp:{account}:{phone}",
        chat_key=phone,
        key_source=KEY_SOURCE_PHONE,
        phone_unresolved=False,
    )


def _fallback_identity(account: str, display_name: str, slug: str = "") -> PeerIdentity:
    token = slug or slugify_display_name(display_name)
    return PeerIdentity(
        account_id=account,
        display_name=(display_name or "").strip(),
        phone="",
        conversation_id=f"whatsapp:{account}:name:{token}",
        chat_key=f"name:{token}",
        key_source=KEY_SOURCE_FALLBACK,
        phone_unresolved=True,
    )


def _from_conversation_id(explicit: str, account: str, display_name: str) -> Optional[PeerIdentity]:
    prefix = f"whatsapp:{account}:"
    if not explicit.startswith(prefix):
        return None
    rest = explicit[len(prefix):]
    if rest.startswith("name:"):
        slug = rest[5:] or "unknown"
        ident = _fallback_identity(account, display_name or slug, slug)
        return ident
    phone = normalize_phone(rest)
    if phone and phone == re.sub(r"\D", "", rest):
        return _phone_identity(account, phone, display_name)
    return None


def resolve_identity(
    account_id: str,
    *,
    display_name: str = "",
    phone: str = "",
    xml: Optional[bytes] = None,
    conversation_id: str = "",
) -> PeerIdentity:
    """稳定键优先用号码。已有 ``whatsapp:`` 会话 id 时沿用，不退回显示名。"""
    account = str(account_id or "default").strip() or "default"
    name = str(display_name or "").strip()
    parsed = _from_conversation_id(str(conversation_id or "").strip(), account, name)
    if parsed is not None:
        if name and parsed.display_name != name and parsed.key_source == KEY_SOURCE_PHONE:
            return replace(parsed, display_name=name)
        if name and parsed.phone_unresolved and not parsed.display_name:
            return replace(parsed, display_name=name)
        return parsed
    found = normalize_phone(phone)
    if not found and xml:
        found = extract_thread_phone(xml)
    if not found:
        found = normalize_phone(name)
    if found:
        return _phone_identity(account, found, name)
    return _fallback_identity(account, name)


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (bounds or "").strip())
    if not match:
        return None
    return tuple(int(match.group(i)) for i in range(1, 5))  # type: ignore[return-value]


def _parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    parents: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parents[child] = parent
    return parents


def infer_screen_width(xml: Optional[bytes], default: int = 1080) -> int:
    if not xml:
        return default
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return default
    max_right = 0
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if bb:
            max_right = max(max_right, bb[2])
    return max_right or default


def extract_thread_phone(xml: Optional[bytes]) -> str:
    """只看顶栏 / 联系人状态，不把聊天气泡里的数字当号码。"""
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    header_hits: List[str] = []
    for el in root.iter():
        rid = (el.get("resource-id") or "").lower()
        if "message_text" in rid:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        in_header = bool(bb) and bb[1] < 320
        interesting = any(
            token in rid
            for token in (
                "conversation_contact_status",
                "phone",
                "subtitle",
                "conversation_contact",
            )
        )
        if not in_header and not interesting:
            continue
        blob = " ".join((el.get("text") or "", el.get("content-desc") or ""))
        for phone in _phones_in_text(blob):
            if phone not in header_hits:
                header_hits.append(phone)
    return header_hits[0] if header_hits else ""


@dataclass(frozen=True)
class ListRow:
    name: str
    cx: int
    cy: int
    unread: int
    phone: str


def _row_unread(el: ET.Element) -> int:
    best = 0
    for node in el.iter():
        rid = node.get("resource-id") or ""
        if "unread" not in rid and "message_count" not in rid:
            continue
        text = (node.get("text") or "").strip()
        desc = node.get("content-desc") or ""
        if text.isdigit():
            best = max(best, int(text))
        else:
            match = re.search(r"(\d+)", desc)
            if match:
                best = max(best, int(match.group(1)))
    return best


def _row_phone(el: ET.Element, name: str) -> str:
    phones = _phones_in_text(name)
    for node in el.iter():
        rid = node.get("resource-id") or ""
        if "unread" in rid or "message_count" in rid:
            continue
        blob = " ".join((node.get("text") or "", node.get("content-desc") or ""))
        for phone in _phones_in_text(blob):
            if phone not in phones:
                phones.append(phone)
    return phones[0] if phones else ""


def scan_list_rows(xml: Optional[bytes]) -> List[ListRow]:
    """聊天列表全部会话行。号码来自行内文字 / content-desc，不是显示名本身。"""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    parents = _parent_map(root)
    rows: List[ListRow] = []
    seen = set()
    for el in root.iter():
        rid = el.get("resource-id") or ""
        if "conversations_row_contact_name" not in rid:
            continue
        name = (el.get("text") or "").strip()
        if not name:
            continue
        row_el = el
        row_bb = _parse_bounds(el.get("bounds") or "")
        cur = el
        for _ in range(6):
            parent = parents.get(cur)
            if parent is None:
                break
            cur = parent
            bb = _parse_bounds(cur.get("bounds") or "")
            if bb and (bb[2] - bb[0]) > 400:
                row_el = cur
                row_bb = bb
                break
        if not row_bb:
            continue
        key = (name, row_bb)
        if key in seen:
            continue
        seen.add(key)
        cx = (row_bb[0] + row_bb[2]) // 2
        cy = (row_bb[1] + row_bb[3]) // 2
        rows.append(ListRow(
            name=name,
            cx=cx,
            cy=cy,
            unread=_row_unread(row_el),
            phone=_row_phone(row_el, name),
        ))
    rows.sort(key=lambda row: row.cy)
    return rows


def select_chat_row(
    rows: Sequence[ListRow],
    ident: PeerIdentity,
) -> Tuple[Optional[ListRow], str]:
    """按稳定键选行。同名多行时不点第一行。"""
    if ident.phone:
        matched = [
            row for row in rows
            if row.phone == ident.phone or normalize_phone(row.name) == ident.phone
        ]
        if len(matched) == 1:
            return matched[0], "phone"
        if len(matched) > 1:
            return None, "ambiguous_phone"
    target = (ident.display_name or "").strip().casefold()
    if not target:
        return None, "not_found"
    by_name = [row for row in rows if row.name.strip().casefold() == target]
    if len(by_name) == 1:
        return by_name[0], "unique_display_name"
    if len(by_name) > 1:
        return None, "ambiguous_display_name"
    return None, "not_found"


def incoming_text(xml: Optional[bytes], screen_width: int = 1080) -> str:
    if not xml:
        return ""
    picked = ui.pick_last_incoming_text(xml, screen_width=screen_width)
    if isinstance(picked, tuple):
        return str(picked[0] or "")
    return str(picked or "")


def _peer_and_gate_text(xml: Optional[bytes], screen_width: int) -> Tuple[str, str]:
    """展示用最后一条入站，以及拿去过 STOP 闸的全文（含被短词过滤的 STOP）。"""
    bubbles = incoming_bubble_texts(xml, screen_width)
    picked = incoming_text(xml, screen_width)
    peer = picked or (bubbles[-1] if bubbles else "")
    gate = "\n".join(bubbles) if bubbles else peer
    return peer, gate


def incoming_bubble_texts(xml: Optional[bytes], screen_width: int = 1080) -> List[str]:
    """左侧气泡原文。短词 STOP 也会留下——列表解析会把它当成日期样的短英文丢掉。"""
    if not xml:
        return []
    width = screen_width or infer_screen_width(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    found: List[Tuple[int, str]] = []
    for el in root.iter():
        rid = el.get("resource-id") or ""
        if "message_text" not in rid:
            continue
        body = (el.get("text") or "").strip()
        if not body:
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        left, _top, right, bottom = bb
        cx = (left + right) // 2
        if cx > width * 0.5 or right > width * 0.85:
            continue
        found.append((bottom, body))
    found.sort(key=lambda item: item[0])
    return [body for _bottom, body in found]


def receipt_from_desc(desc: str) -> str:
    """没有明确双勾 / 蓝勾文案时只返回 sent。"""
    blob = desc or ""
    low = blob.lower()
    if any(marker in blob or marker in low for marker in _READ_MARKERS):
        return "read"
    if _READ_WORD_RE.search(low):
        return "read"
    if any(marker in blob or marker in low for marker in _DELIVERED_MARKERS):
        return "delivered"
    return "sent"


def classify_send_evidence(
    xml: Optional[bytes],
    text: str,
    *,
    screen_width: int = 0,
) -> Dict[str, Any]:
    """己方气泡可见才算 sent。回执升级只认明确的已送达 / 已读标记。"""
    width = screen_width or infer_screen_width(xml)
    empty = {"bubble": False, "receipt": "", "status": "send_unconfirmed", "screen_width": width}
    needle = (text or "").strip()
    if not xml or not needle:
        return empty
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return empty
    parents = _parent_map(root)
    best: Optional[Tuple[int, str]] = None
    for el in root.iter():
        rid = el.get("resource-id") or ""
        if "message_text" not in rid:
            continue
        body = (el.get("text") or "").strip()
        if not body or (needle not in body and body not in needle):
            continue
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        left, top, right, bottom = bb
        cx = (left + right) // 2
        outgoing = cx > width * 0.5 or right > width * 0.85
        if not outgoing:
            continue
        desc_parts = [el.get("content-desc") or ""]
        parent = parents.get(el)
        for _ in range(3):
            if parent is None:
                break
            desc_parts.append(parent.get("content-desc") or "")
            parent = parents.get(parent)
        desc = " ".join(part for part in desc_parts if part)
        if best is None or bottom >= best[0]:
            best = (bottom, desc)
    if best is None:
        return empty
    receipt = receipt_from_desc(best[1])
    return {
        "bubble": True,
        "receipt": receipt,
        "status": "sent",
        "screen_width": width,
    }


def thread_confirms_target(
    xml: Optional[bytes],
    *,
    display_name: str = "",
    phone: str = "",
) -> Dict[str, Any]:
    """打开后的顶栏必须对得上目标。对不上就不发。"""
    title = ""
    if xml:
        title = ui.find_chat_title(xml) or ""
    header_phone = extract_thread_phone(xml)
    want_phone = normalize_phone(phone) or normalize_phone(display_name)
    if want_phone:
        if header_phone == want_phone or normalize_phone(title) == want_phone:
            return {"ok": True, "reason": "phone", "title": title, "phone": header_phone}
        return {"ok": False, "reason": "phone_mismatch", "title": title, "phone": header_phone}
    if display_name and title and title.strip().casefold() == display_name.strip().casefold():
        return {"ok": True, "reason": "title", "title": title, "phone": header_phone}
    return {"ok": False, "reason": "unverified_thread", "title": title, "phone": header_phone}


def rejection_reason(
    cfg: Optional[Dict[str, Any]],
    chat_state: Optional[Dict[str, Any]],
    peer_text: str,
    *,
    legacy_state: Optional[Dict[str, Any]] = None,
) -> str:
    """STOP / 黑名单 / 静默。开关关掉时返回空串，不挡发送。"""
    if not stop_gate_enabled(cfg):
        return ""
    if text_is_stop(peer_text, stop_keywords(cfg)):
        return "stop_contact"
    for state in (chat_state or {}, legacy_state or {}):
        if int(state.get("blacklist") or 0):
            return "blacklist"
        quiet = float(state.get("quiet_until") or 0)
        if quiet > time.time():
            return "quiet"
    return ""


class WaMinLoop:
    """可注入假设备的闭环。生产 runner 复用这里的键、闸门和回执。"""

    def __init__(
        self,
        *,
        account_id: str,
        state_store: Any = None,
        inbox_store: Any = None,
        cfg: Optional[Dict[str, Any]] = None,
        device: Any = None,
        cfg_getter: Any = None,
        inbox_getter: Any = None,
        device_getter: Any = None,
    ) -> None:
        self.account_id = str(account_id or "default")
        self.state = state_store
        self._inbox = inbox_store
        self._cfg = dict(cfg or {})
        self._device = device
        self._cfg_getter = cfg_getter
        self._inbox_getter = inbox_getter
        self._device_getter = device_getter

    @property
    def cfg(self) -> Dict[str, Any]:
        if self._cfg_getter is not None:
            loaded = self._cfg_getter() or {}
            return loaded if isinstance(loaded, dict) else {}
        return self._cfg

    @property
    def inbox(self) -> Any:
        if self._inbox_getter is not None:
            return self._inbox_getter()
        return self._inbox

    @property
    def device(self) -> Any:
        if self._device_getter is not None:
            return self._device_getter()
        return self._device

    def identify(
        self,
        *,
        display_name: str = "",
        phone: str = "",
        xml: Optional[bytes] = None,
        conversation_id: str = "",
    ) -> PeerIdentity:
        return resolve_identity(
            self.account_id,
            display_name=display_name,
            phone=phone,
            xml=xml,
            conversation_id=conversation_id,
        )

    def _legacy_state(self, ident: PeerIdentity) -> Dict[str, Any]:
        if self.state is None or not ident.display_name:
            return {}
        legacy_key = f"wa:{self.account_id}:{ident.display_name}"
        try:
            return self.state.get_chat_state(legacy_key) or {}
        except Exception:
            return {}

    def _state_of(self, ident: PeerIdentity) -> Dict[str, Any]:
        if self.state is None:
            return {}
        try:
            return self.state.get_chat_state(ident.conversation_id) or {}
        except Exception:
            return {}

    def remember(self, ident: PeerIdentity, *, peer_text: str = "", reply_text: str = "") -> None:
        if self.state is None:
            return
        fields: Dict[str, Any] = {
            "display_name": ident.display_name,
            "phone": ident.phone,
            "key_source": ident.key_source,
            "phone_unresolved": 1 if ident.phone_unresolved else 0,
            "conversation_id": ident.conversation_id,
        }
        if peer_text:
            fields["last_peer_text"] = peer_text
            fields["last_peer_ts"] = time.time()
        if reply_text:
            fields["last_reply"] = reply_text
            fields["last_reply_ts"] = time.time()
        self.state.upsert_chat_state(ident.conversation_id, **fields)

    def note_stop(self, ident: PeerIdentity, peer_text: str) -> None:
        if self.state is None:
            return
        quiet_min = float(self.cfg.get("stop_contact_quiet_minutes") or 1440)
        self.state.upsert_chat_state(
            ident.conversation_id,
            blacklist=1,
            quiet_until=time.time() + quiet_min * 60,
            display_name=ident.display_name,
            phone=ident.phone,
            key_source=ident.key_source,
            phone_unresolved=1 if ident.phone_unresolved else 0,
            conversation_id=ident.conversation_id,
            last_peer_text=peer_text,
            last_peer_ts=time.time(),
        )

    def rejection_block(self, ident: PeerIdentity, peer_text: str) -> str:
        reason = rejection_reason(
            self.cfg,
            self._state_of(ident),
            peer_text,
            legacy_state=self._legacy_state(ident),
        )
        if reason == "stop_contact":
            self.note_stop(ident, peer_text)
        return reason

    def record_inbound(self, ident: PeerIdentity, text: str, *, ts: Optional[float] = None) -> bool:
        store = self.inbox
        body = (text or "").strip()
        if store is None or not body:
            return False
        from src.inbox.models import InboxConversation, InboxMessage

        now = float(ts if ts is not None else time.time())
        store.upsert_conversation(InboxConversation(
            conversation_id=ident.conversation_id,
            platform="whatsapp",
            account_id=ident.account_id,
            chat_key=ident.chat_key,
            display_name=ident.display_name,
            phone=ident.phone,
            last_text=body,
            last_ts=now,
            unread=1,
        ))
        mid = hashlib.sha256(
            f"in|{ident.conversation_id}|{body}".encode("utf-8")
        ).hexdigest()[:24]
        return bool(store.ingest_message(InboxMessage(
            conversation_id=ident.conversation_id,
            platform_msg_id=f"warpa-in-{mid}",
            direction="in",
            text=body,
            original_text=body,
            ts=now,
        )))

    def record_outbound(
        self,
        ident: PeerIdentity,
        text: str,
        client_msg_id: str,
        *,
        ts: Optional[float] = None,
    ) -> bool:
        store = self.inbox
        body = (text or "").strip()
        if store is None or not body:
            return False
        from src.inbox.models import InboxMessage

        now = float(ts if ts is not None else time.time())
        return bool(store.ingest_message(InboxMessage(
            conversation_id=ident.conversation_id,
            platform_msg_id=client_msg_id or "",
            direction="out",
            text=body,
            original_text=body,
            ts=now,
            sent_by="ai",
        )))

    def prepare_outbound(
        self,
        *,
        source: str,
        identity: PeerIdentity,
        text: str,
        peer_text: str = "",
        dry_run: bool = False,
        client_msg_id: str = "",
        existing_item_id: int = 0,
    ) -> Dict[str, Any]:
        mode = str(self.cfg.get("reply_mode") or "auto")
        cmid = (client_msg_id or "").strip() or make_client_msg_id(
            identity.conversation_id, peer_text, source, text,
        )
        base: Dict[str, Any] = {
            "ok": True,
            "touch_device": False,
            "duplicate": False,
            "item_id": int(existing_item_id or 0),
            "client_msg_id": cmid,
            "conversation_id": identity.conversation_id,
            "key_source": identity.key_source,
            "phone_unresolved": identity.phone_unresolved,
            "source": source,
            "status": "",
            "step": "",
            "receipt": "",
        }
        block = self.rejection_block(identity, peer_text)
        if block:
            if base["item_id"] and self.state is not None:
                self.state.mark_send_queue_item(base["item_id"], "blocked", block)
            base.update(status="blocked", step=block)
            return base
        if not auto_reply_allowed(source, mode):
            step = "reply_mode_off" if mode.strip().lower() == "off" else "reply_mode_approve"
            base.update(status=step, step=step)
            return base
        if self.state is None:
            base.update(touch_device=not dry_run, status="dry_run" if dry_run else "processing",
                        step="dry_run_done" if dry_run else "ready")
            return base
        if dry_run:
            if base["item_id"]:
                self.state.mark_send_queue_item(base["item_id"], "dry_run")
            else:
                rec = self.state.enqueue_send_ex(
                    identity.conversation_id,
                    identity.display_name,
                    text,
                    client_msg_id=cmid,
                    conversation_id=identity.conversation_id,
                    status="dry_run",
                )
                base["item_id"] = rec["id"]
                base["duplicate"] = bool(rec.get("duplicate"))
            base.update(status="dry_run", step="dry_run_done", receipt="dry_run")
            return base
        if base["item_id"]:
            item = self.state.get_send_queue_item(base["item_id"]) or {}
            status = str(item.get("status") or "")
            if status in {"sent", "dry_run", "blocked", "send_unconfirmed", "failed", "cancelled"}:
                base.update(duplicate=True, status=status, step="duplicate")
                return base
            if status == "queued" and not self.state.claim_send_queue_item(base["item_id"]):
                base.update(duplicate=True, status="processing", step="duplicate")
                return base
            base.update(touch_device=True, status="processing", step="ready")
            return base
        rec = self.state.enqueue_send_ex(
            identity.conversation_id,
            identity.display_name,
            text,
            client_msg_id=cmid,
            conversation_id=identity.conversation_id,
            status="queued",
        )
        base["item_id"] = rec["id"]
        if rec.get("duplicate"):
            base.update(duplicate=True, status=rec.get("status") or "", step="duplicate")
            return base
        if not self.state.claim_send_queue_item(rec["id"]):
            base.update(duplicate=True, status="processing", step="duplicate")
            return base
        base.update(touch_device=True, status="processing", step="ready")
        return base

    def mark_failed(self, item_id: int, error: str) -> None:
        if self.state is None or not item_id:
            return
        self.state.mark_send_queue_item(int(item_id), "failed", error)

    def finish_outbound(
        self,
        prep: Dict[str, Any],
        xml: Optional[bytes],
        text: str,
        identity: PeerIdentity,
    ) -> Dict[str, Any]:
        evidence = classify_send_evidence(xml, text)
        out = dict(prep)
        out["touch_device"] = False
        if evidence["status"] == "sent":
            receipt = evidence["receipt"] if evidence["receipt"] in {"sent", "delivered", "read"} else "sent"
            if self.state is not None and prep.get("item_id"):
                self.state.mark_send_queue_item(int(prep["item_id"]), "sent", receipt=receipt)
            self.record_outbound(identity, text, str(prep.get("client_msg_id") or ""))
            self.remember(identity, reply_text=text)
            out.update(ok=True, status="sent", step="sent", receipt=receipt)
            return out
        if self.state is not None and prep.get("item_id"):
            self.state.mark_send_queue_item(
                int(prep["item_id"]), "send_unconfirmed", "bubble_not_seen",
            )
        out.update(ok=False, status="send_unconfirmed", step="send_unconfirmed", receipt="")
        return out

    def deliver(
        self,
        *,
        identity: PeerIdentity,
        text: str,
        peer_text: str = "",
        source: str = "unread",
        dry_run: bool = False,
        client_msg_id: str = "",
        existing_item_id: int = 0,
    ) -> Dict[str, Any]:
        prep = self.prepare_outbound(
            source=source,
            identity=identity,
            text=text,
            peer_text=peer_text,
            dry_run=dry_run,
            client_msg_id=client_msg_id,
            existing_item_id=existing_item_id,
        )
        if not prep.get("touch_device"):
            return prep
        device = self.device
        if device is None:
            self.mark_failed(int(prep.get("item_id") or 0), "no_device")
            prep.update(ok=False, status="failed", step="no_device")
            return prep
        device.send_text(text)
        xml = device.dump()
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        return self.finish_outbound(prep, xml, text, identity)

    def open_for_reply(self, ident: PeerIdentity) -> Dict[str, Any]:
        device = self.device
        if device is None:
            return {"ok": False, "reason": "no_device"}
        xml = device.dump()
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        row, why = select_chat_row(scan_list_rows(xml), ident)
        if row is None:
            return {"ok": False, "reason": why, "conversation_id": ident.conversation_id}
        device.tap(row.cx, row.cy)
        return {
            "ok": True,
            "reason": why,
            "cx": row.cx,
            "cy": row.cy,
            "conversation_id": ident.conversation_id,
        }

    def process_unread(
        self,
        *,
        reply_text: str,
        dry_run: bool = False,
        client_msg_id: str = "",
    ) -> Dict[str, Any]:
        device = self.device
        if device is None:
            return {"ok": False, "step": "no_device"}
        xml = device.dump()
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        unread = [row for row in scan_list_rows(xml) if row.unread > 0]
        if not unread:
            return {"ok": True, "step": "no_unread"}
        row = unread[0]
        device.tap(row.cx, row.cy)
        thread = device.dump()
        if isinstance(thread, str):
            thread = thread.encode("utf-8")
        width = int(getattr(device, "screen_width", 0) or 0) or infer_screen_width(thread)
        ident = self.identify(display_name=row.name, phone=row.phone, xml=thread)
        peer, gate_text = _peer_and_gate_text(thread, width)
        self.remember(ident, peer_text=peer)
        self.record_inbound(ident, peer)
        block = self.rejection_block(ident, gate_text)
        common = ident.as_dict()
        common["peer_text"] = peer
        if block:
            return {**common, "ok": True, "step": block, "status": "blocked", "touch_device": False}
        mode = str(self.cfg.get("reply_mode") or "auto")
        if not auto_reply_allowed("unread", mode):
            step = "reply_mode_off" if mode.strip().lower() == "off" else "reply_mode_approve"
            if step == "reply_mode_approve" and self.state is not None and reply_text:
                self.state.insert_pending(
                    ident.conversation_id, peer, reply_text, ident.display_name,
                )
            return {**common, "ok": True, "step": step, "status": step, "touch_device": False}
        sent = self.deliver(
            identity=ident,
            text=reply_text,
            peer_text=peer,
            source="unread",
            dry_run=dry_run,
            client_msg_id=client_msg_id,
        )
        sent.update(common)
        return sent

    def process_open_thread(
        self,
        *,
        reply_text: str,
        dry_run: bool = False,
        client_msg_id: str = "",
    ) -> Dict[str, Any]:
        device = self.device
        if device is None:
            return {"ok": False, "step": "no_device"}
        thread = device.dump()
        if isinstance(thread, str):
            thread = thread.encode("utf-8")
        if b"entry" not in thread:
            return {"ok": True, "step": "not_in_thread"}
        width = int(getattr(device, "screen_width", 0) or 0) or infer_screen_width(thread)
        title = ui.find_chat_title(thread) or ""
        ident = self.identify(display_name=title, xml=thread)
        peer, gate_text = _peer_and_gate_text(thread, width)
        self.remember(ident, peer_text=peer)
        self.record_inbound(ident, peer)
        common = ident.as_dict()
        common["peer_text"] = peer
        block = self.rejection_block(ident, gate_text)
        if block:
            return {**common, "ok": True, "step": block, "status": "blocked", "touch_device": False}
        mode = str(self.cfg.get("reply_mode") or "auto")
        if not auto_reply_allowed("open_thread", mode):
            step = "reply_mode_off" if mode.strip().lower() == "off" else "reply_mode_approve"
            return {**common, "ok": True, "step": step, "status": step, "touch_device": False}
        sent = self.deliver(
            identity=ident,
            text=reply_text,
            peer_text=peer,
            source="open_thread",
            dry_run=dry_run,
            client_msg_id=client_msg_id,
        )
        sent.update(common)
        return sent
