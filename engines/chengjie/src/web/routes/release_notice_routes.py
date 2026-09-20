"""「这版改变了什么」升级首开弹窗 API（Q-4 #267 F）。

- ``GET  /api/release-notice``          → {ok, pending, version, rows[], labels{}}（labels 已按请求语言本地化）
- ``POST /api/release-notice/apply``    → body {version, choices:{key: keep|flip}, timezone?}
- ``POST /api/release-notice/dismiss``  → 关闭 ＝ 全部 keep（写显式新默认）＋ mark_seen

只给 master / operator（api_auth 由 admin 注入；角色再在这里核一次）。所有异常 → ok:false，
永不 500（弹窗是锦上添花，不许拖垮工作台）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_LABEL_KEYS = (
    "rn_title", "rn_sub", "rn_keep_off", "rn_keep_on", "rn_turn_on", "rn_turn_off",
    "rn_was_on", "rn_was_off", "rn_now_on", "rn_now_off", "rn_value_change",
    "rn_tz_label", "rn_tz_ph", "rn_tz_hint", "rn_tz_required", "rn_tz_bad",
    "rn_apply", "rn_applying", "rn_later", "rn_fail", "rn_done",
)


def register_release_notice_routes(app, *, api_auth, config_manager=None) -> None:
    def _cm(request: Request):
        return getattr(request.app.state, "config_manager", None) or config_manager

    def _role_ok(request: Request) -> bool:
        """会话角色 master / operator 才弹（坐席看不到、也改不了默认）；无会话（token 鉴权 /
        测试）＝api_auth 已放行即视为可见。"""
        role = ""
        try:
            role = str(request.session.get("role", "") or "")
        except Exception:
            role = ""
        return (not role) or role in ("master", "operator")

    def _version(request: Request) -> str:
        from src.utils.app_identity import app_version
        override = str(request.query_params.get("v") or "").strip()
        return override or app_version()

    @app.get("/api/release-notice")
    async def api_release_notice(request: Request, _=Depends(api_auth)):
        try:
            from src.utils import release_defaults as rd
            cm = _cm(request)
            if cm is None or not _role_ok(request):
                return {"ok": True, "pending": False, "why": "unavailable"}
            v = _version(request)
            info = rd.pending_notice(cm, v)
            labels = {k: tr(request, k, k) for k in _LABEL_KEYS}
            labels["rn_title"] = tr(request, "rn_title", "{v}", v=v)
            rows = []
            for r in info.get("rows") or []:
                base = str(r.get("i18n") or "")
                rows.append({
                    "key": r["key"], "kind": r["kind"], "old": r.get("old"), "new": r.get("new"),
                    "current": r.get("current"), "needs_timezone": bool(r.get("needs_timezone")),
                    "timezone_key": r.get("timezone_key", ""),
                    "current_timezone": r.get("current_timezone", ""),
                    "title": tr(request, base + "_title", r["key"]),
                    "desc": tr(request, base + "_desc", ""),
                })
            return {"ok": True, "pending": bool(info.get("pending")), "version": v,
                    "why": info.get("why", ""), "rows": rows, "labels": labels}
        except Exception:
            logger.debug("release-notice 读取失败", exc_info=True)
            return {"ok": False, "pending": False, "why": "error"}

    @app.post("/api/release-notice/apply")
    async def api_release_notice_apply(request: Request, _=Depends(api_auth)):
        try:
            from src.utils import release_defaults as rd
            cm = _cm(request)
            if cm is None or not _role_ok(request):
                return {"ok": False, "reason": "unavailable"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            v = str(body.get("version") or "").strip() or _version(request)
            choices = body.get("choices") if isinstance(body.get("choices"), dict) else {}
            tz = str(body.get("timezone") or "").strip()
            res = rd.apply_choices(cm, v, {str(k): str(c) for k, c in choices.items()}, timezone=tz)
            if not res["ok"]:
                for f in res["failed"]:
                    if f.get("reason") == "tz_required":
                        f["message"] = tr(request, "rn_tz_required", "")
                    elif f.get("reason") == "bad_choice":
                        f["message"] = tr(request, "rn_fail", "") + str(f.get("key"))
            res["version"] = v
            return res
        except Exception:
            logger.debug("release-notice apply 失败", exc_info=True)
            return {"ok": False, "reason": "error"}

    @app.post("/api/release-notice/dismiss")
    async def api_release_notice_dismiss(request: Request, _=Depends(api_auth)):
        try:
            from src.utils import release_defaults as rd
            cm = _cm(request)
            if cm is None:
                return {"ok": False, "reason": "unavailable"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            v = str(body.get("version") or "").strip() or _version(request)
            res = rd.apply_choices(cm, v, {})
            res["version"] = v
            return res
        except Exception:
            logger.debug("release-notice dismiss 失败", exc_info=True)
            return {"ok": False, "reason": "error"}


__all__ = ["register_release_notice_routes"]
