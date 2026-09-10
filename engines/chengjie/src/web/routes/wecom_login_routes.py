# -*- coding: utf-8 -*-
"""企业微信成员扫码登录智聊（实施97 · P1）——路由层。

- ``GET /login/wecom``            生成签名 state（存会话）→ 302 到企微登录页
- ``GET /login/wecom/callback``   校验 state → 用自建应用凭证换 ``auth/getuserinfo`` → userid →
                                  绑定表 ``<config_dir>/wecom_bindings.json`` 找本地用户；没有则按配置自动开户
                                  （``wecom_<userid>``，随机不可用密码，默认坐席角色）→ 建会话（与账号密码登录同一套键）
- ``GET /api/auth/wecom/status``  登录页/设置页探测：是否启用、缺什么、回调地址是什么（不回显 Secret）

配置 ``wecom_login``：enabled / corpid / agentid / secret（留空回落 wechat_kf 同一应用）/ redirect_base（HTTPS 可信域名）/
default_role / auto_provision / allowed_userids。纯逻辑在 ``src/integrations/wecom_sso.py``。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from src.integrations.wecom_sso import (
    build_login_url, local_username_for, redirect_uri_for, sign_state, sso_config, sso_ready,
    userid_allowed, verify_state,
)

logger = logging.getLogger(__name__)


class WecomBindingStore:
    """企微 userid ↔ 智聊用户名（JSON 小表，锁内读写；坏文件当空）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, str]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in (d or {}).items()} if isinstance(d, dict) else {}
        except Exception:
            return {}

    def username_for(self, userid: str) -> str:
        with self._lock:
            return self._load().get(str(userid), "")

    def bind(self, userid: str, username: str) -> None:
        with self._lock:
            d = self._load()
            d[str(userid)] = str(username)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def all(self) -> Dict[str, str]:
        with self._lock:
            return self._load()


async def exchange_code_for_userid(corpid: str, secret: str, code: str) -> Dict[str, Any]:
    """``auth/getuserinfo``：返回 ``{ok, userid, errcode, errmsg}``（非成员回 openid，不算登录成功）。"""
    from src.integrations.wechat_kf import WeChatKfClient
    client = WeChatKfClient(corpid, secret)
    res = await client.api("auth/getuserinfo", params={"code": str(code or "")}, method="GET")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    userid = str(data.get("userid") or "").strip()
    return {"ok": bool(res.get("ok")) and bool(userid), "userid": userid,
            "errcode": int(res.get("errcode") or 0), "errmsg": str(res.get("errmsg") or "")}


def register_wecom_login_routes(app: FastAPI, *, user_store: Any, config_manager: Any, templates: Any,
                                exchange: Any = None) -> None:
    from src.utils.web_user_store import ROLE_LABELS
    _exchange = exchange or exchange_code_for_userid

    def _cfg() -> Dict[str, Any]:
        return sso_config(getattr(config_manager, "config", None) or {})

    def _state_key() -> str:
        cfg = getattr(config_manager, "config", None) or {}
        return str((cfg.get("web_admin") or {}).get("auth_token") or "") + "|" + _cfg().get("secret", "")

    def _bindings() -> WecomBindingStore:
        cfg_path = getattr(config_manager, "config_path", None)
        base = Path(cfg_path).resolve().parent if cfg_path else Path.cwd() / "config"
        return WecomBindingStore(base / "wecom_bindings.json")

    def _request_base(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}"

    def _relay_base(request: Request) -> str:
        """官网中继设备前缀（relay.enabled 时恒有；连不连上不影响 redirect_uri 的确定性）。"""
        rc = getattr(request.app.state, "relay_client", None)
        base = str(getattr(rc, "public_base", "") or "") if rc is not None else ""
        if not base:
            try:
                from src.integrations.relay_client import public_base_for, relay_config
                r = relay_config(getattr(config_manager, "config", None) or {})
                if r["enabled"] and r["device_id"]:
                    base = public_base_for(r["url"], r["device_id"])
            except Exception:
                base = ""
        return base

    def _redirect_uri(request: Request) -> str:
        cfg = _cfg()
        return redirect_uri_for(cfg["redirect_base"], _request_base(request), _relay_base(request))

    def _verify_dir() -> Path:
        cfg_path = getattr(config_manager, "config_path", None)
        base = Path(cfg_path).resolve().parent if cfg_path else Path.cwd() / "config"
        return base / "wecom_verify"

    def _login_error(request: Request, key: str):
        from src.web.web_i18n import tr
        return templates.TemplateResponse(request, "login.html", {
            "error": tr(request, key), "has_users": user_store.user_count() > 0, "next": "",
            "wecom_login": _cfg().get("enabled", False)})

    @app.get("/api/auth/wecom/status")
    async def api_wecom_status(request: Request):
        cfg = _cfg()
        ok, why = sso_ready(cfg)
        rc = getattr(request.app.state, "relay_client", None)
        relay_base = _relay_base(request)
        source = "configured" if cfg["redirect_base"] else ("relay" if relay_base else "request")
        return {"ok": True, "enabled": cfg["enabled"], "ready": ok, "reason": why,
                "redirect_uri": _redirect_uri(request), "redirect_source": source,
                "public_base": (cfg["redirect_base"] or relay_base or _request_base(request)).rstrip("/"),
                "relay": (rc.status() if rc is not None and hasattr(rc, "status") else
                          ({"enabled": True, "connected": False, "public_base": relay_base} if relay_base else {"enabled": False})),
                "corpid": cfg["corpid"], "agentid": cfg["agentid"], "default_role": cfg["default_role"],
                "auto_provision": cfg["auto_provision"]}

    @app.get("/WW_verify_{code}.txt")
    async def wecom_domain_verify_file(code: str):
        """企微「可信域名」归属验证文件（后台下载的 WW_verify_xxx.txt 放到 <config_dir>/wecom_verify/）。"""
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9]{4,64}", code or ""):
            return PlainTextResponse("not found", status_code=404)
        p = _verify_dir() / f"WW_verify_{code}.txt"
        if not p.is_file():
            return PlainTextResponse("not found", status_code=404)
        return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))

    @app.post("/api/auth/wecom/verify-file")
    async def api_wecom_verify_file(request: Request):
        """主管把企微后台给的验证文件名与内容贴进来 → 落到 wecom_verify/，随后 https://<域名>/WW_verify_xxx.txt 即可访问。"""
        from src.web.routes.unified_inbox_auth import _require_supervisor
        api_auth = getattr(request.app.state, "api_auth", None)
        if callable(api_auth):
            api_auth(request)
        _require_supervisor(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        import re as _re
        name = str((body or {}).get("filename") or "").strip()
        content = str((body or {}).get("content") or "").strip()
        if not _re.fullmatch(r"WW_verify_[A-Za-z0-9]{4,64}\.txt", name) or not content or len(content) > 256:
            return JSONResponse({"ok": False, "error": "bad_file"}, status_code=400)
        d = _verify_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")
        cfg = _cfg()
        relay_base = _relay_base(request)
        base = (cfg["redirect_base"] or relay_base or _request_base(request)).rstrip("/")
        # 走中继的实例：文件还要发布到中继根目录（企微校验的是 https://relay.bd2026.cc/WW_verify_xxx.txt）
        relay_published = False
        rc = getattr(request.app.state, "relay_client", None)
        if not cfg["redirect_base"] and relay_base and rc is not None and hasattr(rc, "publish_verify_file"):
            try:
                relay_published = bool(await rc.publish_verify_file(name, content))
            except Exception:
                relay_published = False
        # 中继根目录（不带 /d/<id>）才是企微看的域名根
        url_base = base.split("/d/", 1)[0] if (not cfg["redirect_base"] and relay_base) else base
        return {"ok": True, "url": f"{url_base}/{name}", "relay_published": relay_published}

    @app.get("/login/wecom")
    async def wecom_login_start(request: Request):
        cfg = _cfg()
        ok, why = sso_ready(cfg)
        if not ok:
            return _login_error(request, "err.auth.wecom_not_ready")
        # 浏览器来源签进 state：经中继回跳时中继据此把浏览器直接送回本实例（只放私网/回环）
        state = sign_state(_state_key(), origin=_request_base(request))
        request.session["wecom_state"] = state
        url = build_login_url(cfg["corpid"], cfg["agentid"], _redirect_uri(request), state, lang=cfg["lang"])
        return RedirectResponse(url, status_code=302)

    @app.get("/login/wecom/callback")
    async def wecom_login_callback(request: Request, code: str = "", state: str = ""):
        cfg = _cfg()
        ok, _why = sso_ready(cfg)
        if not ok:
            return _login_error(request, "err.auth.wecom_not_ready")
        expected = str(request.session.get("wecom_state") or "")
        if not (state and verify_state(_state_key(), state) and secrets.compare_digest(state, expected)):
            return _login_error(request, "err.auth.wecom_bad_state")
        request.session.pop("wecom_state", None)
        if not code:
            return _login_error(request, "err.auth.wecom_denied")
        try:
            ex = await _exchange(cfg["corpid"], cfg["secret"], code)
        except Exception:
            logger.debug("[wecom_sso] 换取用户身份异常", exc_info=True)
            ex = {"ok": False, "userid": ""}
        if not ex.get("ok"):
            logger.warning("[wecom_sso] getuserinfo 失败 errcode=%s %s", ex.get("errcode"), ex.get("errmsg"))
            return _login_error(request, "err.auth.wecom_exchange_failed")
        userid = str(ex["userid"])
        if not userid_allowed(cfg, userid):
            return _login_error(request, "err.auth.wecom_not_allowed")
        bindings = _bindings()
        username = bindings.username_for(userid)
        user = user_store.get_user(username) if username else None
        if user is None:
            if not cfg["auto_provision"]:
                return _login_error(request, "err.auth.wecom_not_allowed")
            username = local_username_for(userid)
            user = user_store.get_user(username) or user_store.create_user(
                username, secrets.token_urlsafe(24), role=cfg["default_role"], display_name=userid)
            if user is None:
                return _login_error(request, "err.auth.wecom_exchange_failed")
            bindings.bind(userid, username)
        if not int(user.get("enabled", 1) or 0):
            return _login_error(request, "err.auth.wecom_not_allowed")
        role = str(user.get("role") or cfg["default_role"])
        if role not in ROLE_LABELS:
            role = cfg["default_role"]
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")[:200]
        jti = user_store.create_session(user["username"], role, ip, ua)
        try:
            user_store.mark_login(user["username"], fallback_role=role)
        except Exception:
            pass
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        request.session["role"] = role
        request.session["display_name"] = user.get("display_name") or userid
        request.session["jti"] = jti
        request.session["wecom_userid"] = userid
        dest = "/workspace" if role == "agent" else "/workspace/dash"
        return RedirectResponse(dest, status_code=303)


__all__ = ["register_wecom_login_routes", "WecomBindingStore", "exchange_code_for_userid"]
