"""Console QR-code login (集中扫码): shared checks for controller routes and the node agent.

Flow (docs/FLEET_CONSOLE_QR_LOGIN.md):
  console -> POST /api/fleet/nodes/{node}/login-qr            -> task login_qr
  agent   -> POST <instance>/api/platforms/{p}/login/start    -> ack {login_id, qr_data_url}
  console -> POST /api/fleet/nodes/{node}/login-qr/{id}/status -> task login_status (repeat)
  agent   -> GET  <instance>/api/platforms/{p}/login/{id}/status -> ack {status, qr_data_url?}
Everything that ends up in a local URL or an <img src> is checked here.
"""

from __future__ import annotations

import re
from typing import Any, Dict

PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
QR_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$")
MAX_QR_DATA_URL = 256 * 1024

LOGIN_QR_TTL_SEC = 300          # the node must pick the start task up within 5 min
LOGIN_STATUS_TTL_SEC = 90       # a status probe older than this is useless
TERMINAL_LOGIN_STATUSES = frozenset({"authorized", "failed", "expired", "cancelled"})
START_PAYLOAD_KEYS = ("account_id", "label", "group", "proxy_id", "use_fingerprint", "phone", "mode")


def valid_platform(value: Any) -> str:
    p = str(value or "").strip().lower()
    return p if PLATFORM_RE.match(p) else ""


def valid_login_id(value: Any) -> str:
    v = str(value or "").strip()
    return v if LOGIN_ID_RE.match(v) else ""


def valid_instance(value: Any) -> str:
    v = str(value or "").strip()
    return v if INSTANCE_RE.match(v) else ""


def safe_qr_data_url(value: Any) -> str:
    """Only a base64 raster ``data:image/...`` URL survives (no svg, no javascript:, no http)."""
    s = str(value or "").strip()
    if not s or len(s) > MAX_QR_DATA_URL:
        return ""
    return s if QR_DATA_URL_RE.match(s) else ""


def start_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelisted, size-capped options for ``/login/start``."""
    out: Dict[str, Any] = {}
    for k in START_PAYLOAD_KEYS:
        if k not in body or body[k] is None or body[k] == "":
            continue
        v = body[k]
        out[k] = bool(v) if k == "use_fingerprint" else str(v)[:128]
    return out


def sanitize_login_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Applied when a login_qr / login_status ack is stored on the controller."""
    if not isinstance(result, dict):
        return {}
    out = dict(result)
    if "qr_data_url" in out:
        out["qr_data_url"] = safe_qr_data_url(out.get("qr_data_url"))
    if "login_id" in out:
        out["login_id"] = valid_login_id(out.get("login_id"))
    for k in ("status", "detail", "reason_code", "instruction", "mode", "kind", "platform", "instance", "account_id"):
        if k in out and out[k] is not None:
            out[k] = str(out[k])[:500]
    return out


def strip_qr(result: Dict[str, Any]) -> Dict[str, Any]:
    """Task lists do not carry the QR image; only GET /api/fleet/tasks/{id} does."""
    if not isinstance(result, dict) or not result.get("qr_data_url"):
        return result
    out = dict(result)
    out["qr_data_url"] = ""
    out["has_qr"] = True
    return out
