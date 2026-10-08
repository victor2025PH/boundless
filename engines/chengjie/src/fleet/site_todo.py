"""Per-room site todos (agent 0.3.22).

The controller builds a classified list from the latest heartbeat phones, the
latest ``net_health`` rows, and the wallpaper ledger last pushed to that node.
It enqueues one ``site_todo`` task for that node only. The agent writes the
list into the local operator-alert snapshot; the panel groups it by category.

Items carry a category and a wallpaper number or an unnumbered slot. They do
not carry adb serials or free text. Live-stream hosts, node 173, an explicit
exclude list, and any node the guard cannot identify are not queued. The
protected phone is dropped before a category is chosen. Offline nodes are not
queued; the console says they need the agent reinstalled.

Facebook logged-out is reported only when ``fb_screen`` and ``fb_confirm`` are
both ``login``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from .phones import is_protected
from .phone_rules import scrub_phone_error_text
from .protocol import STATUS_DONE, STATUS_QUEUED, TASK_NET_HEALTH, TASK_PUSH_CONFIG, TASK_SITE_TODO

logger = logging.getLogger("fleet.site_todo")

DEBOUNCE_SEC = 25
CONSOLE_REINSTALL = "需重装 agent"
_MAX_ITEMS = 64
_WALL_RE = re.compile(r"^\d{1,4}$")
_CATEGORIES = (
    "no_network",
    "no_signal",
    "unauthorized",
    "missing_wallpaper",
    "usb_unplugged",
    "ledger_conflict",
    "fb_logged_out",
)
_CATEGORY_SET = frozenset(_CATEGORIES)
_ORDER = {name: index for index, name in enumerate(_CATEGORIES)}


def site_skip_reason(node: Any, *, exclude: Any = None) -> str:
    """``live_stream`` / ``node_173`` / ``excluded`` / ``uncertain`` / ``""``.

    Delegates to ``auto_dispatch_block``. A live-stream display name, group, or
    hostname, node 173, and the explicit exclude list all skip the node. Room
    hosts CHINAMI, CHINAMI-B, AORY-A, and AORY-B are not live. A serial that
    merely contains those characters is not a node identity.
    """
    from .dispatch_guard import auto_dispatch_block

    reason = auto_dispatch_block(node, exclude=exclude)
    if reason == "protected_phone":
        return ""
    return reason


def phone_signature(phones: Any) -> str:
    """Stable signature of serial+state. Stays in controller memory; never enqueue it."""
    rows: List[List[str]] = []
    if isinstance(phones, list):
        for item in phones:
            if not isinstance(item, dict):
                continue
            rows.append([
                str(item.get("serial") or "").strip().upper(),
                str(item.get("state") or "").strip().lower(),
            ])
    rows.sort()
    return json.dumps(rows, separators=(",", ":"))


def should_dispatch(prev_sig: Optional[str], new_sig: str, last_at: Optional[float], now: float,
                    debounce_sec: float = DEBOUNCE_SEC) -> bool:
    """False when the list is unchanged, or the last send is still inside the window."""
    if new_sig == prev_sig:
        return False
    if last_at is not None and (float(now) - float(last_at)) < float(debounce_sec):
        return False
    return True


def site_todo_signature(todos: Any) -> str:
    clean = sanitize_site_todos(todos)
    return json.dumps(clean, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _wall_number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    if _WALL_RE.fullmatch(text):
        try:
            if int(text) >= 1:
                return text
        except ValueError:
            return ""
    return ""


def _item(category: str, number: str, slot: Optional[int] = None) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "category": category,
        "wallpaper_no": number,
        "unnumbered": not bool(number),
    }
    if not number and isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= 9999:
        row["slot"] = slot
    return row


def _index_phones(phones: Any) -> Dict[str, str]:
    current: Dict[str, str] = {}
    if not isinstance(phones, list):
        return current
    for raw in phones:
        if not isinstance(raw, dict):
            continue
        serial = str(raw.get("serial") or "").strip().upper()
        if not serial or is_protected(serial):
            continue
        current[serial] = str(raw.get("state") or "").strip().lower()
    return current


def _row_target(row: Dict[str, Any]) -> Optional[tuple]:
    number = _wall_number(row.get("wallpaper_no"))
    if number:
        return number, None
    key = str(row.get("key") or "")
    match = re.fullmatch(r"unnumbered:(\d+)", key.split("#", 1)[0])
    if not match:
        return None
    return "", int(match.group(1))


def build_site_todos(*, phones: Any, net_rows: Any, wallpaper_map: Any,
                     phones_error: str = "") -> List[Dict[str, Any]]:
    """Classify one node's own phones. Output is already whitelist-shaped."""
    from .operator_alert import parse_wallpaper_map

    current = _index_phones(phones)
    wall = parse_wallpaper_map(wallpaper_map)
    by_number: Dict[str, List[str]] = {}
    for serial, number in wall.items():
        by_number.setdefault(number, []).append(serial)
    present = set(current)
    found: List[Dict[str, Any]] = []
    seen = set()

    def add(category: str, number: str, slot: Optional[int] = None) -> None:
        if category not in _CATEGORY_SET:
            return
        number = _wall_number(number)
        key = (category, number, int(slot or 0))
        if key in seen:
            return
        seen.add(key)
        found.append(_item(category, number, slot))

    for number, serials in by_number.items():
        live = [serial for serial in serials if serial in present]
        if len(serials) >= 2 and len(live) >= 2:
            add("ledger_conflict", number)

    slot_of: Dict[str, int] = {}
    next_slot = 1

    def claim_slot(serial: str) -> int:
        nonlocal next_slot
        if serial not in slot_of:
            slot_of[serial] = next_slot
            next_slot += 1
        return slot_of[serial]

    for serial in sorted(current):
        state = current[serial]
        number = wall.get(serial, "")
        slot = None if number else claim_slot(serial)
        if state in {"unauthorized", "no_permissions"}:
            add("unauthorized", number, slot)
        elif state == "offline":
            add("usb_unplugged", number, slot)
        elif state == "device" and not number:
            add("missing_wallpaper", "", slot)

    if not str(phones_error or "").strip():
        for number, serials in by_number.items():
            if serials and not any(serial in present for serial in serials):
                add("usb_unplugged", number)

    for raw in net_rows or []:
        if not isinstance(raw, dict):
            continue
        target = _row_target(raw)
        if target is None:
            continue
        number, slot = target
        airplane = raw.get("airplane") is True
        reachable = raw.get("reachable")
        transport = raw.get("transport")
        signal = raw.get("signal")
        sim = raw.get("sim")
        if airplane or reachable is False or transport == "none":
            add("no_network", number, slot)
        wifi_up = transport == "wifi" and reachable is True
        if (signal == "none" and not airplane and not wifi_up and transport != "wifi"
                and sim not in {"absent", "unavailable"}):
            add("no_signal", number, slot)
        if raw.get("fb_screen") == "login" and raw.get("fb_confirm") == "login":
            add("fb_logged_out", number, slot)
        if len(found) >= _MAX_ITEMS:
            break

    found.sort(key=lambda row: (
        _ORDER.get(str(row.get("category")), 99),
        int(row["wallpaper_no"]) if str(row.get("wallpaper_no") or "").isdigit() else 10_000,
        int(row.get("slot") or 0),
    ))
    return sanitize_site_todos(found)[:_MAX_ITEMS]


def sanitize_site_todo_item(item: Any) -> Optional[Dict[str, Any]]:
    """Keep category, wallpaper number, unnumbered, and an integer slot. Drop the rest."""
    if not isinstance(item, dict):
        return None
    category = item.get("category")
    if category not in _CATEGORY_SET:
        return None
    number = _wall_number(item.get("wallpaper_no"))
    out: Dict[str, Any] = {
        "category": category,
        "wallpaper_no": number,
        "unnumbered": bool(item.get("unnumbered")) or not number,
    }
    if number:
        out["unnumbered"] = False
    slot = item.get("slot")
    if not number and isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= 9999:
        out["slot"] = slot
        out["unnumbered"] = True
    return out


def sanitize_site_todos(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    seen = set()
    for raw in items:
        clean = sanitize_site_todo_item(raw)
        if clean is None:
            continue
        key = (clean["category"], clean["wallpaper_no"], clean.get("slot") or 0)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
        if len(out) >= _MAX_ITEMS:
            break
    return out


def sanitize_site_todo_payload(payload: Any) -> Dict[str, Any]:
    src = payload if isinstance(payload, dict) else {}
    return {"todos": sanitize_site_todos(src.get("todos"))}


_QUOTED_DEVICE_RE = re.compile(
    r"(device\s+['\"])([A-Za-z0-9][A-Za-z0-9._:\-]{3,63})(['\"])",
    re.IGNORECASE,
)


def _ack_serials(result: Any) -> List[tuple]:
    """(serial, wallpaper) pairs carried on a raw ack, before the whitelist drops them."""
    pairs: List[tuple] = []
    if not isinstance(result, dict):
        return pairs
    serial = result.get("serial")
    wall = result.get("wallpaper_no") or result.get("wallpaper") or ""
    if isinstance(serial, str) and serial.strip():
        pairs.append((serial.strip(), wall if isinstance(wall, str) else ""))
    phones = result.get("phones")
    if isinstance(phones, list):
        for row in phones:
            if not isinstance(row, dict):
                continue
            serial = row.get("serial")
            if not isinstance(serial, str) or not serial.strip():
                continue
            wall = row.get("wallpaper_no") or row.get("wallpaper") or ""
            pairs.append((serial.strip(), wall if isinstance(wall, str) else ""))
    return pairs


def scrub_ack_detail(text: Any, result: Any = None) -> str:
    """Redact serials in a site_todo or net_health ack detail.

    A known serial becomes its wallpaper number, or ``[redacted]`` when the
    ledger number is absent. ``device '…' not found`` is already covered by
    ``scrub_phone_error_text``. Quoted device tokens of four or more characters
    are redacted for any trailing word, including ``offline``. A bare
    ``device offline`` is left as-is.
    """
    out = text if isinstance(text, str) else ""
    for serial, wallpaper in _ack_serials(result):
        out = scrub_phone_error_text(out, serial=serial, wallpaper=wallpaper)
    out = scrub_phone_error_text(out)

    def _swap(match: re.Match) -> str:
        token = match.group(2)
        if len(token) < 4 or _WALL_RE.fullmatch(token):
            return match.group(0)
        return match.group(1) + "[redacted]" + match.group(3)

    return _QUOTED_DEVICE_RE.sub(_swap, out)


def sanitize_site_todo_result(result: Any) -> Dict[str, Any]:
    """Ack whitelist: ``ok`` and a count. No serials and no free text."""
    src = result if isinstance(result, dict) else {}
    out: Dict[str, Any] = {}
    if src.get("ok") is True:
        out["ok"] = True
    count = src.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        todos = src.get("todos")
        if isinstance(todos, list):
            count = len(sanitize_site_todos(todos))
        elif isinstance(todos, int) and not isinstance(todos, bool):
            count = todos
        else:
            count = None
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= _MAX_ITEMS:
        out["count"] = count
    return out


def _latest_wallpaper(store: Any, node_id: str) -> Any:
    try:
        tasks = store.list_tasks(node_id=node_id, kind=TASK_PUSH_CONFIG, limit=40)
    except Exception:
        logger.debug("[site_todo] wallpaper lookup failed node=%s", node_id, exc_info=True)
        return None
    for rec in tasks or []:
        payload = rec.get("payload") if isinstance(rec, dict) else None
        patch = payload.get("patch") if isinstance(payload, dict) else None
        if isinstance(patch, dict) and "wallpaper_map" in patch:
            return patch.get("wallpaper_map")
    return None


def _latest_net_rows(store: Any, node_id: str) -> List[Dict[str, Any]]:
    from .net_health import public_row

    try:
        tasks = store.list_tasks(node_id=node_id, kind=TASK_NET_HEALTH, limit=20)
    except Exception:
        logger.debug("[site_todo] net_health lookup failed node=%s", node_id, exc_info=True)
        return []
    for rec in tasks or []:
        if not isinstance(rec, dict) or rec.get("status") != STATUS_DONE:
            continue
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        phones = result.get("phones")
        if not isinstance(phones, list):
            continue
        rows = []
        for row in phones:
            if isinstance(row, dict):
                rows.append(public_row(row))
        return rows
    return []


def todos_for_node(store: Any, node: Dict[str, Any]) -> List[Dict[str, Any]]:
    if site_skip_reason(node, exclude=getattr(store, "dispatch_exclude", None)):
        return []
    return build_site_todos(
        phones=node.get("phones"),
        net_rows=_latest_net_rows(store, str(node.get("node_id") or "")),
        wallpaper_map=_latest_wallpaper(store, str(node.get("node_id") or "")),
        phones_error=str(node.get("phones_error") or ""),
    )


def _scrub_name(text: Any, node: Dict[str, Any], wall: Dict[str, str]) -> str:
    out = str(text or "")
    for phone in node.get("phones") or []:
        if not isinstance(phone, dict):
            continue
        serial = str(phone.get("serial") or "")
        if len(serial) < 4:
            continue
        out = scrub_phone_error_text(out, serial=serial, wallpaper=wall.get(serial.strip().upper(), ""))
    return out


def console_site_todos(store: Any, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Read-only rows for the console. Offline nodes say the agent must be reinstalled."""
    from .operator_alert import parse_wallpaper_map

    rows: List[Dict[str, Any]] = []
    for node in store.list_nodes(include_revoked=False, now=now) or []:
        if not isinstance(node, dict):
            continue
        skip = site_skip_reason(node, exclude=getattr(store, "dispatch_exclude", None))
        wall = parse_wallpaper_map(_latest_wallpaper(store, str(node.get("node_id") or "")))
        offline = node.get("state") != "online"
        rows.append({
            "node_id": str(node.get("node_id") or ""),
            "host_name": _scrub_name(node.get("host_name"), node, wall),
            "label": _scrub_name(node.get("label"), node, wall),
            "skipped": skip,
            "console": CONSOLE_REINSTALL if (not skip and offline) else "",
            "todos": [] if skip else todos_for_node(store, node),
        })
    return rows


def dispatch_site_todo(store: Any, node_id: str, state: Dict[str, Dict[str, Any]],
                       *, now: Optional[float] = None) -> Optional[str]:
    """Enqueue at most one ``site_todo`` for an online node. Never raises.

    ``state`` remembers the last signature and time per node (process memory).
    An unchanged list is not sent again. A change inside ``DEBOUNCE_SEC`` waits.
    A node that already has a queued ``site_todo`` is not given a second one
    until the debounce window has passed, and then the stale queued task is
    cancelled so only one remains.
    """
    try:
        return _dispatch(store, node_id, state, now=now)
    except Exception:
        logger.warning("[site_todo] dispatch skipped node=%s", node_id, exc_info=True)
        return None


def _dispatch(store: Any, node_id: str, state: Dict[str, Dict[str, Any]],
              *, now: Optional[float]) -> Optional[str]:
    node = store.get_node(node_id)
    if not isinstance(node, dict) or node.get("state") != "online":
        return None
    if site_skip_reason(node, exclude=getattr(store, "dispatch_exclude", None)):
        return None
    todos = todos_for_node(store, node)
    sig = site_todo_signature(todos)
    stamp = float(now if now is not None else time.time())
    prev = state.get(node_id) if isinstance(state.get(node_id), dict) else {}
    prev_sig = prev.get("sig") if isinstance(prev, dict) else None
    if prev_sig is None and not todos:
        state[node_id] = {"sig": sig, "at": stamp}
        return None
    last_at = prev.get("at") if isinstance(prev, dict) else None
    if not isinstance(last_at, (int, float)) or isinstance(last_at, bool):
        last_at = None
    if not should_dispatch(prev_sig if isinstance(prev_sig, str) else None, sig, last_at, stamp):
        return None
    queued = store.list_tasks(node_id=node_id, status=STATUS_QUEUED, kind=TASK_SITE_TODO, limit=20)
    for rec in queued or []:
        task_id = rec.get("task_id") if isinstance(rec, dict) else None
        if task_id:
            store.cancel(task_id)
    payload = sanitize_site_todo_payload({"todos": todos})
    rec = store.enqueue(node_id, TASK_SITE_TODO, payload=payload, created_by="site_todo", now=stamp)
    if not rec:
        return None
    state[node_id] = {"sig": sig, "at": stamp}
    logger.info("[site_todo] queued node=%s count=%s", node_id, len(payload.get("todos") or []))
    return str(rec.get("task_id") or "")
