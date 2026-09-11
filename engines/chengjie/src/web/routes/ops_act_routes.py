# -*- coding: utf-8 -*-
"""告警卡「已处理 / 静音」动作页 ``/ops/act``（运维群降噪 P1.3，2026-09-10）。

背景：运维群每张巡检提醒卡（待审草稿 / 案例 / 客户在等 / 语音 / LAN GPU）此前只能被动看，
没有「我知道了、别再吵」的口——同一件事每 4h（现为内容不变 24h）一遍，直到有人去工作台把
根因清掉。@tgzkw_bot 的更新流固定挂在官网 webhook（报障群按钮那套），本机拿不到
callback_query，所以这里不用 Telegram inline 按钮，而是**卡片里一条链接 → 本机一页两步确认**：

    GET  /ops/act?k=<提醒键>&t=<链接令牌>   24h 一次性令牌（ops_glance_token 同款 HMAC）；
                                            无令牌但带工作台 session 也放行。页面列当前状态 +
                                            动作按钮，每个按钮是一张表单，带 15 分钟动作令牌。
    POST /ops/act  k / action / hours / at   验动作令牌（一次性）→ 写提醒账本（RemindLedger）：
                                            ack = 记当前内容指纹，指纹不变不再提；
                                            mute = 静音 N 小时；unmute = 全清。

写的是 ``app.state.health_watchdog._remind``（巡检进程内那份账本，避免双写同一 JSON）。
不碰 session、不建会话。日志一行 ``[ops-act] <result> key=… by=…``。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import Form, Request

from src.utils import ops_glance_token

logger = logging.getLogger(__name__)

LINK_TTL_SEC = 24 * 3600      # 卡片链接：值班的人可能几小时后才看到卡
ACTION_TTL_SEC = 15 * 60      # 页面上的动作表单
_MUTE_CHOICES = (4.0, 24.0, 72.0)
_LABEL_PREFIXES = ("draft_backlog", "case_backlog", "unanswered_inbound", "avatar_voice", "lan_gpu")


def link_target(key: str) -> str:
    """链接令牌绑定的相对路径（notifier 铸令牌与本路由验令牌必须同式）。"""
    return f"/ops/act?k={quote(str(key or ''), safe='')}"


def action_target(key: str) -> str:
    return f"/ops/act/do?k={quote(str(key or ''), safe='')}"


def act_url(base_url: str, key: str) -> Optional[str]:
    """给卡片拼 ``https://<base>/ops/act?k=…&t=…``；铸不出令牌（secret 未配）→ None。"""
    tok = ops_glance_token.mint(link_target(key), ttl_sec=LINK_TTL_SEC)
    if not tok:
        return None
    return f"{str(base_url or '').rstrip('/')}/ops/act?k={quote(str(key), safe='')}&t={tok}"


def _label_key(key: str) -> str:
    for p in _LABEL_PREFIXES:
        if str(key).startswith(p):
            return f"oa.label.{p}"
    return ""


def _fmt_until(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def _hours_txt(hours: float) -> str:
    h = max(0.0, float(hours or 0.0))
    if h < 1.0:
        return f"{int(round(h * 60))} 分钟"
    if h < 48.0:
        return f"{h:.0f} 小时" if h >= 10 else f"{h:.1f} 小时"
    d = h / 24.0
    return f"{d:.1f} 天" if d < 10 else f"{d:.0f} 天"


def register_ops_act_routes(app, *, templates, page_auth=None) -> None:
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
        return templates.TemplateResponse(request, "ops_act.html", ctx, status_code=status)

    def _ledger(request: Request):
        wd = getattr(request.app.state, "health_watchdog", None)
        led = getattr(wd, "_remind", None) if wd is not None else None
        return led if led is not None and hasattr(led, "mute") else None

    def _session_user(request: Request) -> str:
        """已登录工作台的人：允许无令牌操作，并把用户名记为操作人。"""
        if page_auth is None:
            return ""
        try:
            page_auth(request)
        except Exception:
            return ""
        try:
            sess = request.session
        except Exception:
            return ""
        return str(sess.get("display_name") or sess.get("username") or "") or "session"

    def _who(request: Request, session_user: str) -> str:
        if session_user and session_user != "session":
            return session_user
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
        return f"卡片链接 {ip}".strip()

    def _state_ctx(request: Request, led, key: str, now: float) -> Dict[str, Any]:
        st = led.get(key) if key in led.keys() else None
        meta = dict((st or {}).get("meta") or {})
        muted = led.is_muted(key, now=now)
        fp_now = led._fp_of(st) if st else ""
        acked = bool(meta.get("acked_fp")) and str(meta.get("acked_fp")) == fp_now
        first = float((st or {}).get("first_seen") or 0.0)
        return {
            "key": key,
            "label_key": _label_key(key),
            "summary": str(meta.get("summary") or ""),
            "alerted": bool((st or {}).get("alerted")),
            "muted": muted,
            "muted_until": _fmt_until(meta.get("muted_until")) if muted else "",
            "acked": acked,
            "by": str(meta.get("muted_by") or meta.get("acked_by") or ""),
            "hours_txt": _hours_txt((now - first) / 3600.0) if first > 0 else "",
            "action_token": ops_glance_token.mint(action_target(key), ttl_sec=ACTION_TTL_SEC) or "",
            "mute_choices": [int(h) for h in _MUTE_CHOICES],
        }

    @app.get("/ops/act")
    async def ops_act_page(request: Request, k: str = "", t: str = ""):
        key = str(k or "").strip()[:200]
        login_url = "/login?next=" + quote(link_target(key), safe="") if key else "/login"
        session_user = _session_user(request)
        ok, reason = (True, "session") if session_user else ops_glance_token.verify(t, link_target(key))
        logger.info("[ops-act] open %s key=%s ip=%s", reason, key or "-",
                    request.headers.get("x-real-ip") or (request.client.host if request.client else "-"))
        if not ok or not key:
            status = 410 if reason in ("expired", "used") else 403
            state = "expired" if reason == "used" else (reason if key else "bad_sig")
            return _render(request, {"state": state, "login_url": login_url, "key": key}, status)
        led = _ledger(request)
        if led is None:
            return _render(request, {"state": "unavailable", "login_url": login_url, "key": key}, 503)
        ctx = _state_ctx(request, led, key, time.time())
        ctx.update({"state": "ok", "login_url": login_url, "result": ""})
        return _render(request, ctx)

    @app.post("/ops/act")
    async def ops_act_do(request: Request, k: str = Form(""), action: str = Form(""),
                         hours: str = Form(""), at: str = Form("")):
        key = str(k or "").strip()[:200]
        login_url = "/login?next=" + quote(link_target(key), safe="") if key else "/login"
        session_user = _session_user(request)
        ok, reason = ops_glance_token.verify(at, action_target(key))
        if not ok or not key:
            logger.info("[ops-act] do rejected %s key=%s", reason, key or "-")
            return _render(request, {"state": "bad_action", "login_url": login_url, "key": key}, 403)
        led = _ledger(request)
        if led is None:
            return _render(request, {"state": "unavailable", "login_url": login_url, "key": key}, 503)
        now = time.time()
        who = _who(request, session_user)
        act = str(action or "").strip().lower()
        result = ""
        if act == "ack":
            result = "acked" if led.ack(key, by=who, now=now) == "acked" else "muted"
        elif act == "mute":
            try:
                h = float(hours or 24.0)
            except (TypeError, ValueError):
                h = 24.0
            if h not in _MUTE_CHOICES:
                h = 24.0
            led.mute(key, h, by=who, now=now)
            result = "muted"
        elif act == "unmute":
            led.unmute(key, now=now)
            result = "unmuted"
        else:
            return _render(request, {"state": "bad_action", "login_url": login_url, "key": key}, 400)
        logger.info("[ops-act] %s key=%s by=%s", result, key, who)
        ctx = _state_ctx(request, led, key, now)
        ctx.update({"state": "ok", "login_url": login_url, "result": result, "who": who})
        return _render(request, ctx)


__all__ = ["register_ops_act_routes", "act_url", "link_target", "action_target",
           "LINK_TTL_SEC", "ACTION_TTL_SEC"]
