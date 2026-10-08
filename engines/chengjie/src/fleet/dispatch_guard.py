"""One fail-closed gate for controller auto-dispatch and phone operations.

Live-stream machines, seat 173, and protected phones must never receive a
site todo, a phone operation, or any other scheduled / batch phone task.
Every automatic path calls ``auto_dispatch_block``. A match on any rule
excludes the node. An identity we cannot read excludes it too.

The check does not log serials. Callers keep using wallpaper numbers in
audit lines.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

from .detect import is_seat_173
from .phones import is_protected

logger = logging.getLogger("fleet.dispatch_guard")

# Whole tokens that still mean a live-stream machine. ``aory`` is not one:
# room hosts AORY-A / AORY-B must keep receiving work.
_LIVE_TOKENS = frozenset({"mnlwin", "livestream"})
_LIVE_GROUPS = frozenset({"live", "live-stream", "live_stream", "livestream"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def is_guarded_kind(kind: str) -> bool:
    """Tasks that must pass the gate: site todos and every phone operation."""
    from .protocol import (
        PHONE_APP_KINDS, PHONE_FLOW_KINDS, PHONE_SESSION_KINDS, PHONE_TASK_KINDS, TASK_SITE_TODO,
    )

    name = str(kind or "").strip().lower()
    if name == TASK_SITE_TODO:
        return True
    return name in PHONE_TASK_KINDS or name in PHONE_FLOW_KINDS or name in PHONE_SESSION_KINDS or name in PHONE_APP_KINDS


def auto_dispatch_block(node: Any, *, serial: Optional[str] = None, exclude: Any = None) -> str:
    """``live_stream`` / ``node_173`` / ``protected_phone`` / ``excluded`` / ``uncertain`` / ``""``.

    Empty means the node may receive the task. Any other string means it must not.
    ``exclude`` is an explicit list of node ids or hostnames (exact, case-insensitive).
    ``None`` means no extra names. A present value that is not a list of strings
    is ``uncertain`` (fail closed).
    """
    try:
        return _block(node, serial=serial, exclude=exclude)
    except Exception:
        logger.warning("[dispatch_guard] check failed; excluding the node")
        return "uncertain"


def agent_host_block(host: str, *, live_flag: bool = False, serial: Optional[str] = None,
                     node_id: str = "") -> str:
    """Agent-side reason, using this PC's hostname.

    Maps the controller reasons onto the strings the agent already returns:
    ``live_stream_host`` and ``seat_173``. Protected phones stay ``protected_phone``.
    """
    node: dict = {"host_name": host or "", "node_id": node_id or ""}
    if live_flag:
        node["meta"] = {"live_stream": True}
    reason = auto_dispatch_block(node, serial=serial)
    if reason == "live_stream":
        return "live_stream_host"
    if reason == "node_173":
        return "seat_173"
    return reason


def _block(node: Any, *, serial: Optional[str], exclude: Any) -> str:
    if serial is not None and str(serial).strip() and is_protected(str(serial)):
        return "protected_phone"
    names = _coerce_exclude(exclude)
    if names is None:
        return "uncertain"
    if not isinstance(node, dict):
        return "uncertain"
    meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
    tags = meta.get("tags") if isinstance(meta.get("tags"), (list, tuple)) else ()
    extra_tags = node.get("tags") if isinstance(node.get("tags"), (list, tuple)) else ()
    host = str(node.get("host_name") or "")
    label = str(node.get("label") or "")
    display = str(node.get("display_name") or node.get("name") or "")
    group = str(node.get("group_name") or "")
    node_id = str(node.get("node_id") or "")
    fields = (host, label, display, group, node_id)
    tag_text = tuple(str(tag) for tag in tuple(tags) + tuple(extra_tags))
    if _live_meta(meta.get("live_stream")):
        return "live_stream"
    for text in fields + tag_text:
        if "直播" in text:
            return "live_stream"
    blob = " ".join(fields + tag_text).casefold()
    if "live-stream" in blob or "live_stream" in blob or "livestream" in blob:
        return "live_stream"
    if _live_group(group) or any(_live_group(tag) for tag in tag_text):
        return "live_stream"
    for text in fields + tag_text:
        if _tokens(text) & _LIVE_TOKENS:
            return "live_stream"
    host_cf = host.casefold()
    if "ganzhi-176" in host_cf or "176" in host_cf:
        return "live_stream"
    if is_seat_173(host, label, display, group, node_id):
        return "node_173"
    for text in (host, label, display, node_id):
        if _node_173(text):
            return "node_173"
    host_key = host.casefold()
    id_key = node_id.casefold()
    for item in names:
        key = item.casefold()
        if key and (key == host_key or key == id_key):
            return "excluded"
    if not host.strip() and not label.strip() and not display.strip() and not node_id.strip():
        return "uncertain"
    return ""


def _live_meta(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) == 1:
        return True
    return str(value).strip().casefold() in _TRUE


def _live_group(value: Any) -> bool:
    return str(value or "").strip().casefold() in _LIVE_GROUPS


def _tokens(value: str) -> set:
    return {part for part in re.split(r"[^a-z0-9]+", str(value or "").lower()) if part}


def _node_173(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text == "173" or text.endswith("-173") or text.endswith("_173")


def _coerce_exclude(exclude: Any) -> Optional[Tuple[str, ...]]:
    """``None`` -> no extra names. A bad shape -> ``None`` (caller fail-closes)."""
    if exclude is None:
        return ()
    if isinstance(exclude, str):
        text = exclude.strip()
        return (text,) if text else ()
    if isinstance(exclude, (list, tuple)):
        out = []
        for item in exclude:
            if isinstance(item, bool) or not isinstance(item, str):
                return None
            text = item.strip()
            if text:
                out.append(text)
        return tuple(out)
    return None
