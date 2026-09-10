# -*- coding: utf-8 -*-
"""告警「一眼看」只读页 ``GET /ops/glance?t=<token>&to=<path>``（Q-14 #262 E，2026-09-10）。

Telegram 告警卡里 ``links=magic`` 的链接落点：令牌由 ``src.utils.ops_glance_token`` 铸/验
（十分钟、一次性、绑定 ``to``）。页面只读——会话号 / 账号 / 平台 / 最近 5 条 / 当前档 +
「在工作台打开」按钮（跳 ``to`` 本身，由那页的正常鉴权要求登录）。

不建会话、不写任何东西、不碰 session；令牌坏了只给一句话 + 「去登录」。
日志一行 ``[ops-glance] ok|expired|bad_sig|used|unconfigured to=… conv=…``。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Request

from src.utils import ops_glance_token

logger = logging.getLogger(__name__)

_CONV_KEYS = ("conv", "cid", "conversation_id")
_RECENT_LIMIT = 5
_TEXT_CAP = 300


def _conv_id_from_path(to: str) -> str:
    try:
        qs = parse_qs(urlsplit(to).query, keep_blank_values=False)
    except Exception:
        return ""
    for k in _CONV_KEYS:
        v = qs.get(k)
        if v and str(v[0]).strip():
            return str(v[0]).strip()[:200]
    return ""


def _safe_to(to: str) -> str:
    """只接受站内相对路径；其余归零（页面仍可渲染，只是没有目标）。"""
    to = str(to or "")
    if not to.startswith("/") or to.startswith("//") or "\\" in to:
        return ""
    return to[:2000]


def _fmt_when(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0)))
    except Exception:
        return ""


def _recent(store: Any, cid: str) -> List[Dict[str, Any]]:
    try:
        rows = store.list_recent_messages(cid, limit=_RECENT_LIMIT, include_deleted=False)
    except TypeError:
        rows = store.list_recent_messages(cid, limit=_RECENT_LIMIT)
    except Exception:
        logger.warning("[ops-glance] list_recent_messages failed conv=%s", cid, exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        text = str(r.get("text") or r.get("original_text") or "")
        if len(text) > _TEXT_CAP:
            text = text[:_TEXT_CAP] + "…"
        out.append({
            "direction": str(r.get("direction") or "in"),
            "when": _fmt_when(r.get("ts")),
            "media_type": str(r.get("media_type") or ""),
            "text": text,
        })
    return out


def register_ops_glance_routes(app, *, templates) -> None:
    def _i18n(request: Request) -> Dict[str, str]:
        d = getattr(getattr(request, "state", None), "i18n", None)
        if isinstance(d, dict):
            return d
        try:
            from src.web.web_i18n import get_translations
            return get_translations()
        except Exception:
            return {}

    def _render(request: Request, ctx: Dict[str, Any], status: int = 200):
        ctx.setdefault("i18n", _i18n(request))
        return templates.TemplateResponse(request, "ops_glance.html", ctx, status_code=status)

    @app.get("/ops/glance")
    async def ops_glance(request: Request, t: str = "", to: str = ""):
        to = _safe_to(to)
        cid = _conv_id_from_path(to)
        login_url = "/login" + (f"?next={quote(to, safe='')}" if to else "")
        ok, reason = ops_glance_token.verify(t, to)
        logger.info("[ops-glance] %s to=%s conv=%s ip=%s", reason, to or "-", cid or "-",
                    request.headers.get("x-real-ip") or (request.client.host if request.client else "-"))
        if not ok:
            status = 410 if reason in ("expired", "used") else 403
            state = "expired" if reason == "used" else reason
            return _render(request, {"state": state, "login_url": login_url, "to": to}, status)

        store = getattr(request.app.state, "inbox_store", None)
        conv: Optional[Dict[str, Any]] = None
        messages: List[Dict[str, Any]] = []
        tier = ""
        if cid and store is None:
            return _render(request, {"state": "store_unavailable", "login_url": login_url, "to": to}, 503)
        if cid and store is not None:
            try:
                conv = store.get_conversation(cid)
            except Exception:
                logger.warning("[ops-glance] get_conversation failed conv=%s", cid, exc_info=True)
                conv = None
            if conv:
                messages = _recent(store, cid)
                try:
                    tier = str(store.get_automation_mode(cid) or "")
                except Exception:
                    tier = ""
        return _render(request, {
            "state": "ok",
            "to": to,
            "conv_id": cid,
            "conv": conv,
            "messages": messages,
            "tier": tier,
            "open_url": to or "/workspace/inbox",
            "login_url": login_url,
        })


__all__ = ["register_ops_glance_routes"]
