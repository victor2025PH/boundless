"""智聊读取无界玩家网关：从对话里抽出手机号/UID，注入只读后台资料。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urljoin

_HAS_DIGITS = re.compile(r"\d{6,}")


def player_gateway_cfg(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return dict(config.get("player_gateway") or {})
    raw = getattr(config, "config", None)
    if isinstance(raw, dict):
        return dict(raw.get("player_gateway") or {})
    return {}


def should_lookup(text: str, cfg: Dict[str, Any]) -> bool:
    if not cfg.get("enabled"):
        return False
    if not (cfg.get("url") and cfg.get("key")):
        return False
    return bool(_HAS_DIGITS.search(text or ""))


def fetch_lookup(cfg: Dict[str, Any], *, q: str = "", phone: str = "", uid: str = "") -> Optional[Dict[str, Any]]:
    base = str(cfg.get("url") or "").rstrip("/") + "/"
    url = urljoin(base, "lookup")
    payload = json.dumps({"q": q, "phone": phone, "uid": uid, "bind": True}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gateway-Key": str(cfg.get("key") or ""),
            "Accept": "application/json",
            "User-Agent": "chatx-player-gateway/1.0",
        },
    )
    timeout = float(cfg.get("timeout_sec") or 8)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return None


def inject_player_block(user_context: Dict[str, Any], text: str, config: Any) -> None:
    """有键即消费：写入 ``_player_data_block``；失败或缺数字则清残留。不抛。"""
    user_context.pop("_player_data_block", None)
    try:
        cfg = player_gateway_cfg(config)
        blob = "\n".join(
            x for x in (
                text,
                str(user_context.get("_current_user_message_for_lang") or ""),
                str(user_context.get("_player_lookup_hint") or ""),
            ) if x
        )
        if not should_lookup(blob, cfg):
            return
        data = fetch_lookup(cfg, q=blob)
        chatx = (data or {}).get("chatx_text") if isinstance(data, dict) else ""
        if chatx:
            user_context["_player_data_block"] = str(chatx)[:4000]
    except Exception:
        return
