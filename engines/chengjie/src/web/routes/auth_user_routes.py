"""认证 / 用户 / 会话管理路由 — 从 admin.py 抽出（Phase E1）。

端点（与抽出前逐行一致）：
  GET  /login                       POST /login        GET /logout
  GET  /setup                       POST /api/setup    POST /api/setup/test-ai
  POST /api/change-password
  GET  /users   POST /users/create  POST /users/update/{user_id}  POST /users/delete/{user_id}
  GET  /api/sessions   POST /api/sessions/{jti}/revoke   POST /api/sessions/revoke-all

团队角色分层 + 坐席字符额度（2026-08-16 增）：
  POST /users/quota/{user_id}       —— 设置坐席月度字符额度（master/admin，层级守卫）
  GET  /api/users/char-usage        —— 团队字符用量总览（master/admin/supervisor）
  GET  /api/workspace/my-usage      —— 当前登录坐席「我的用量」（任意登录角色）

L3 按人权限覆写（2026-08-16 第二批增）：
  GET  /api/users/{user_id}/perms   —— 读取目标用户能力权限三态（同层级守卫）
  POST /users/perms/{user_id}       —— 写入覆写 {allow:[],deny:[]}（同层级守卫 + 审计）

依赖通过 register 传入（闭包 + 单例）；模块级常量（templates / ROLE_*）直接 import，
减少参数穿线。
"""

from __future__ import annotations

import hmac
import time

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.utils.agent_char_usage import (
    agent_chars_enabled,
    agent_quota_status,
    ensure_store_for_read,
)
from src.utils.web_user_store import (
    PERM_REGISTRY,
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_LABELS,
    ROLE_MASTER,
    ROLE_SUPERVISOR,
    assignable_roles,
    can_manage_target,
    default_perm_allowed,
    parse_perms,
)
from src.web.i18n_packs import UI_LANGS
from src.web.login_redirect import resolve_post_login_dest, safe_next_path
from src.web.web_i18n import tr


def register_auth_user_routes(
    app,
    *,
    user_store,
    token,
    config_manager,
    audit_store=None,
    require_auth,
    require_role,
):
    # 复用 admin.py 模块级 templates（与其余页面同一 Jinja 环境）
    from src.web.admin import templates

    def _role_default_dest(role: str) -> str:
        # 角色化落地：坐席→收件箱；管理员→今日概览（上线自检入口）；
        # 系统主（含托管 owner）→今日概览（旧默认 `/`=/cases 会让首登看不见自检红灯）。
        # 运维令牌直登仍走 `/`（见 token 分支），肌肉记忆与应急排查不改。
        if role == ROLE_AGENT:
            return "/workspace"
        # WP-2 首启向导直达：onboarding.enabled 且未完成 → 非坐席首登落 /welcome
        # （显式 ?next= 深链仍最优先——resolve_post_login_dest 只在无 next 时用本默认；
        #  flag 关 / 已完成 / 任何异常 → welcome_pending 恒 False，登录流程零变化）。
        try:
            from src.utils.onboarding_state import welcome_pending
            if welcome_pending(getattr(config_manager, "config", None) or {}):
                return "/welcome"
        except Exception:
            pass
        if role in (ROLE_ADMIN, ROLE_MASTER):
            return "/workspace/dash"
        return "/"

    def _login_ctx(request: Request, *, error: str = "", next_raw: str = ""):
        nxt = safe_next_path(next_raw) or safe_next_path(request.query_params.get("next"))
        return {
            "error": error,
            "has_users": user_store.user_count() > 0,
            "next": nxt,
        }

    # ── 角色分层守卫（谁能管谁 / 谁能发什么角色，判定在 web_user_store 单点）──
    def _actor_role(request: Request) -> str:
        return str(request.session.get("role", "") or "")

    def _guard_target(request: Request, target_role: str) -> None:
        """操作者层级不足以管理目标账号 → 403（角色变更/禁用/删除/设额度共用）。"""
        if not can_manage_target(_actor_role(request), str(target_role or "")):
            raise HTTPException(403, tr(request, "err.team.cannot_manage"))

    def _users_page_ctx(request: Request, *, msg: str = "", msg_ok: bool = True) -> dict:
        actor = _actor_role(request)
        return {
            "users": user_store.list_users(),
            "role_labels": ROLE_LABELS,
            "msg": msg,
            "msg_ok": msg_ok,
            "actor_role": actor,
            "assignable_roles_ctx": [
                (r, ROLE_LABELS.get(r, r)) for r in assignable_roles(actor)
            ],
        }

    def _runtime_config():
        """闭包 config_manager 的实时配置（防 None / 非 dict）。"""
        cfg = getattr(config_manager, "config", None)
        return cfg if isinstance(cfg, dict) else None

    # ── 登录 / 登出 ───────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", _login_ctx(request))

    @app.post("/login")
    async def login_submit(request: Request, auth_token: str = Form(None),
                           username: str = Form(None), password: str = Form(None),
                           next: str = Form("")):
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")[:200]
        # multi-user login
        if username and password:
            user = user_store.verify(username, password)
            if user:
                jti = user_store.create_session(user["username"], user["role"], ip, ua)
                request.session["user_id"] = user["id"]
                request.session["username"] = user["username"]
                request.session["role"] = user["role"]
                request.session["display_name"] = user.get("display_name", username)
                request.session["jti"] = jti
                _dest = resolve_post_login_dest(
                    next_raw=next, role_default=_role_default_dest(user["role"]))
                resp = RedirectResponse(_dest, status_code=303)
                # 语言跟人走：登录即套用该用户保存的 UI 语言（无偏好则不动，沿用 cookie/默认）。
                # 白名单消费 UI_LANGS 单一事实源——此前硬编码 ("zh","en")，vi/th/id 用户
                # 每次登录偏好丢失（set_lang 落库五语、回填只认两语的不对称 bug）。
                _lang = (user.get("lang") or "").strip().lower()
                if _lang in UI_LANGS:
                    resp.set_cookie("ui_lang", _lang, max_age=365 * 86400)
                return resp
            return templates.TemplateResponse(request, "login.html", _login_ctx(
                request, error=tr(request, "err.auth.bad_credentials"), next_raw=next))
        # legacy token login（S6：恒定时间比较，防时序侧信道）
        if auth_token and token and hmac.compare_digest(str(auth_token), str(token)):
            jti = user_store.create_session("admin", ROLE_MASTER, ip, ua)
            request.session["auth"] = token
            request.session["role"] = ROLE_MASTER
            request.session["username"] = "admin"
            request.session["jti"] = jti
            # 令牌直登：默认仍 `/`；若显式带合法 next（交付深链）则尊重
            _dest = resolve_post_login_dest(next_raw=next, role_default="/")
            return RedirectResponse(_dest, status_code=303)
        return templates.TemplateResponse(request, "login.html", _login_ctx(
            request, error=tr(request, "token_error"), next_raw=next))

    @app.get("/logout")
    async def logout(request: Request):
        jti = request.session.get("jti")
        if jti:
            user_store.revoke_session(jti)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ── 首次使用配置向导 ──────────────────────────────────────
    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if user_store.user_count() > 0:
            return RedirectResponse("/", status_code=303)
        ai_cfg = config_manager.config.get("ai", {}) if hasattr(config_manager, "config") else {}
        return templates.TemplateResponse(request, "setup.html", {
            "ai_cfg": ai_cfg,
        })

    @app.post("/api/setup")
    async def setup_submit(request: Request):
        """初始化第一个管理员账户（仅在无用户时可调用）"""
        if user_store.user_count() > 0:
            raise HTTPException(400, tr(request, "err.auth.already_initialized"))
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        confirm  = body.get("confirm", "")
        api_key  = (body.get("api_key") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        model    = (body.get("model") or "").strip()

        if not username or not password:
            raise HTTPException(400, tr(request, "err.auth.user_pass_required"))
        if len(password) < 6:
            raise HTTPException(400, tr(request, "su_js_003"))
        if password != confirm:
            raise HTTPException(400, tr(request, "err.auth.pwd_mismatch_signup"))

        # 创建 master 账户
        result = user_store.create_user(username, password, ROLE_MASTER,
                                        display_name=username)
        if not result:
            raise HTTPException(500, tr(request, "err.auth.create_failed"))

        # 保存 AI 配置（如果提供了）——P0-1：写 overlay（config.local.yaml）而非改写主
        # config.yaml（保住注释/结构，密钥不进 git 跟踪文件），并热重建 AI 运行时免重启。
        if api_key:
            try:
                if hasattr(config_manager, "save_ai_credentials"):
                    config_manager.save_ai_credentials({
                        "api_key": api_key, "base_url": base_url, "model": model,
                    })
                    from src.web.routes.unified_inbox_setup_routes import reload_ai_runtime
                    await reload_ai_runtime(request.app, config_manager)
                else:  # 极端回退（老 ConfigManager stub）：仅进程内生效
                    if not hasattr(config_manager, "config") or config_manager.config is None:
                        config_manager.config = {}
                    config_manager.config.setdefault("ai", {})["api_key"] = api_key
                    if base_url:
                        config_manager.config["ai"]["base_url"] = base_url
                    if model:
                        config_manager.config["ai"]["model"] = model
            except Exception:
                pass  # AI 配置保存失败不影响主流程

        if audit_store:
            audit_store.log("setup", "create_master_user", username)
        return {"ok": True, "username": result["username"]}

    @app.post("/api/setup/test-ai")
    async def setup_test_ai(request: Request):
        """测试 AI API Key 有效性（仅首次安装向导期或已登录可用）。

        S6：原实现「无需登录」→ 任何人可让服务器代发外部 LLM 请求（探测/刷量）。
        收敛为：已存在用户时必须已登录；全新安装（尚无用户）阶段供向导使用。
        """
        if user_store.user_count() > 0 and not request.session.get("user_id"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        body = await request.json()
        api_key  = (body.get("api_key") or "").strip()
        # 从当前配置读取默认值，不再硬编码 DeepSeek
        _ai_cfg = config_manager.get_ai_config() if config_manager else {}
        base_url = (body.get("base_url") or _ai_cfg.get("base_url", "")).rstrip("/")
        model    = (body.get("model") or _ai_cfg.get("model", "gemini-2.5-flash"))
        if api_key == "_keep_":
            api_key = _ai_cfg.get("api_key", "")
        if not api_key:
            raise HTTPException(400, tr(request, "err.auth.api_key_required"))
        if not base_url:
            raise HTTPException(400, tr(request, "err.auth.base_url_required"))
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 5},
                )
            if resp.status_code == 200:
                return {"ok": True, "msg": "连接成功"}
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = resp.text[:200]
            return {"ok": False, "msg": f"API 返回 HTTP {resp.status_code}: {detail}"}
        except Exception as e:
            return {"ok": False, "msg": f"连接失败: {e}"}

    # ── 修改密码 ──────────────────────────────────────────────
    @app.post("/api/change-password")
    async def api_change_password(request: Request):
        require_auth(request)
        body = await request.json()
        old_pw = body.get("old_password", "")
        new_pw = body.get("new_password", "")
        if not old_pw or not new_pw:
            raise HTTPException(400, tr(request, "err.auth.old_new_pwd_required"))
        if len(new_pw) < 6:
            raise HTTPException(400, tr(request, "base.shell.pwd_min_len"))
        uname = request.session.get("username", "")
        if not uname:
            raise HTTPException(400, tr(request, "err.auth.unknown_user"))
        user = user_store.verify(uname, old_pw)
        if not user:
            raise HTTPException(400, tr(request, "err.auth.wrong_current_pwd"))
        user_store.update_user(user["id"], password=new_pw)
        if audit_store:
            audit_store.log(uname, "change_password", uname)
        return {"ok": True}

    # ── 用户管理 ──────────────────────────────────────────────
    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        require_role(request, "users")
        return templates.TemplateResponse(
            request, "users.html", _users_page_ctx(request))

    @app.post("/users/create")
    async def users_create(request: Request, username: str = Form(...),
                           password: str = Form(...), role: str = Form("agent"),
                           display_name: str = Form("")):
        require_role(request, "users")
        # 分层：只能发自己层级以下的角色（master 也不得再造 master）
        if role not in assignable_roles(_actor_role(request)):
            raise HTTPException(403, tr(request, "err.team.role_not_allowed"))
        ajax = "application/json" in request.headers.get("accept", "")
        if len(password) < 6:
            if ajax:
                return {"ok": False, "detail": tr(request, "su_js_003")}
            return templates.TemplateResponse(request, "users.html", _users_page_ctx(
                request, msg="密码至少 6 位", msg_ok=False))
        result = user_store.create_user(username, password, role, display_name)
        if not result:
            if ajax:
                return {"ok": False, "detail": tr(request, "err.auth.user_exists_or_bad_role", username=username)}
            return templates.TemplateResponse(request, "users.html", _users_page_ctx(
                request, msg=f"创建失败：用户名 '{username}' 已存在或角色无效", msg_ok=False))
        if audit_store:
            audit_store.log(request.session.get("username", ""), "create_user", username)
        if ajax:
            return {"ok": True, "id": result["id"], "username": result["username"], "role": result["role"]}
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/update/{user_id}")
    async def users_update(user_id: int, request: Request, role: str = Form(None),
                           password: str = Form(None), enabled: str = Form(None),
                           monthly_char_quota: int = Form(None),
                           quota_alert_pct: int = Form(None)):
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        if str(target.get("role") or "") == ROLE_MASTER:
            # master 行受保护：任何人（含 master 自己）不得经此端点改动
            raise HTTPException(403, tr(request, "err.team.master_protected"))
        _guard_target(request, str(target.get("role") or ""))
        kw = {}
        if role:
            if role not in assignable_roles(_actor_role(request)):
                raise HTTPException(403, tr(request, "err.team.role_not_allowed"))
            kw["role"] = role
        if password:
            kw["password"] = password
        if enabled is not None:
            kw["enabled"] = enabled == "1"
        if monthly_char_quota is not None:
            kw["monthly_char_quota"] = monthly_char_quota
        if quota_alert_pct is not None:
            kw["quota_alert_pct"] = quota_alert_pct
        user_store.update_user(user_id, **kw)
        if audit_store:
            audit_store.log(request.session.get("username", ""), "update_user", str(user_id))
        if "application/json" in request.headers.get("accept", ""):
            updated = user_store.get_user_by_id(user_id)
            return {
                "ok": True,
                "role": updated.get("role") if updated else None,
                "enabled": updated.get("enabled") if updated else None,
            }
        return RedirectResponse("/users", status_code=303)

    @app.post("/users/delete/{user_id}")
    async def users_delete(user_id: int, request: Request):
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        _guard_target(request, str(target.get("role") or ""))
        ok = user_store.delete_user(user_id)
        if audit_store and ok:
            audit_store.log(request.session.get("username", ""), "delete_user", str(user_id))
        if "application/json" in request.headers.get("accept", ""):
            return {"ok": ok, "detail": "" if ok else "无法删除（主帐号不可删除）"}
        return RedirectResponse("/users", status_code=303)

    # ── 坐席月度字符额度（0 = 不限；层级守卫与角色变更同一判定）─────────
    @app.post("/users/quota/{user_id}")
    async def users_set_quota(user_id: int, request: Request,
                              monthly_quota: int = Form(...),
                              alert_pct: int = Form(None)):
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        _guard_target(request, str(target.get("role") or ""))
        kw = {"monthly_char_quota": max(0, int(monthly_quota or 0))}
        if alert_pct is not None:
            kw["quota_alert_pct"] = alert_pct
        user_store.update_user(user_id, **kw)
        updated = user_store.get_user_by_id(user_id) or {}
        if audit_store:
            audit_store.log(
                request.session.get("username", ""), "set_quota",
                f"{target.get('username')}={kw['monthly_char_quota']}")
        return {
            "ok": True,
            "quota": int(updated.get("monthly_char_quota") or 0),
            "alert_pct": int(updated.get("quota_alert_pct") or 80),
        }

    # ── 坐席 Telegram 通知号绑定（P2 2026-08-18：目标达成等业务事件的定向推送
    #    收件地址；空串=解绑=只推管理员渠道。层级守卫与额度同款）─────────────
    @app.post("/users/notify-binding/{user_id}")
    async def users_set_notify_binding(user_id: int, request: Request,
                                       tg_chat_id: str = Form("")):
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        _guard_target(request, str(target.get("role") or ""))
        cleaned = str(tg_chat_id or "").strip()
        digits = cleaned.lstrip("-")
        if cleaned and not (digits.isdigit() and len(digits) <= 20
                            and cleaned.count("-") <= (1 if cleaned.startswith("-") else 0)):
            raise HTTPException(400, tr(request, "err.team.bad_chat_id"))
        user_store.update_user(user_id, notify_tg_chat_id=cleaned)
        updated = user_store.get_user_by_id(user_id) or {}
        if audit_store:
            audit_store.log(
                request.session.get("username", ""), "set_notify_binding",
                f"{target.get('username')}={'bound' if cleaned else 'unbound'}")
        return {
            "ok": True,
            "tg_chat_id": str(updated.get("notify_tg_chat_id") or ""),
        }

    # ── 坐席自助绑定 + 测试推送（P3 2026-08-18）────────────────────────────
    # 管理员代绑（上方端点）之外的自助口：任意登录角色读/写**自己**的通知号——
    # 走 api_auth choke point（agent 白名单放行 /api/workspace 前缀，与 my-usage
    # 同款；页面闸 require_auth 会把 agent 303 走，不适用 JSON API）。
    # 「测试推送」借告警渠道首个启用 telegram 通道的 bot 真发一条：Telegram bot
    # 无法私聊从没跟它说过话的人——这是绑定后收不到推送的最高发故障，绑定当场
    # 就能测出来，而不是等第一单成交才发现静默丢失。

    def _self_username(request: Request) -> str:
        return str(request.session.get("username")
                   or request.session.get("user") or "").strip()

    def _api_choke(request: Request) -> None:
        _api_auth = getattr(request.app.state, "api_auth", None)
        if callable(_api_auth):
            _api_auth(request)
        else:  # 极端回落（测试 stub app 无 choke point）
            require_auth(request)

    def _clean_chat_id(request: Request, raw: str) -> str:
        cleaned = str(raw or "").strip()
        digits = cleaned.lstrip("-")
        if cleaned and not (digits.isdigit() and len(digits) <= 20
                            and cleaned.count("-") <= (1 if cleaned.startswith("-") else 0)):
            raise HTTPException(400, tr(request, "err.team.bad_chat_id"))
        return cleaned

    @app.get("/api/workspace/my-notify-binding")
    async def api_my_notify_binding_get(request: Request):
        _api_choke(request)
        me = _self_username(request)
        u = user_store.get_user(me) if me else None
        chat = str((u or {}).get("notify_tg_chat_id") or "").strip()
        # 只回尾 4 位：页面显示「已绑定 …7810」足够，全量号没必要往前端传
        return {"ok": True, "username": me, "bound": bool(chat),
                "chat_tail": chat[-4:] if chat else ""}

    @app.post("/api/workspace/my-notify-binding")
    async def api_my_notify_binding_set(request: Request,
                                        tg_chat_id: str = Form("")):
        _api_choke(request)
        me = _self_username(request)
        u = user_store.get_user(me) if me else None
        if not u:
            # 令牌直登（admin 不在 web_users 表）没有「自己的行」可写
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        cleaned = _clean_chat_id(request, tg_chat_id)
        user_store.update_user(int(u["id"]), notify_tg_chat_id=cleaned)
        if audit_store:
            audit_store.log(me, "set_notify_binding",
                            f"self={'bound' if cleaned else 'unbound'}")
        return {"ok": True, "bound": bool(cleaned)}

    _notify_test_last: dict = {}   # chat_id → ts（30s 防抖，防拿告警 bot 刷屏）

    @app.post("/api/workspace/my-notify-binding/test")
    async def api_my_notify_binding_test(request: Request,
                                         user_id: int = Form(None)):
        """给绑定号真发一条测试推送。缺省=自己；带 user_id=用户管理页的管理员
        代测（require_role users + 层级守卫，与代绑同权限面）。"""
        _api_choke(request)
        if user_id is not None:
            require_role(request, "users")
            target_u = user_store.get_user_by_id(int(user_id))
            if not target_u:
                raise HTTPException(404, tr(request, "err.team.user_not_found"))
            _guard_target(request, str(target_u.get("role") or ""))
        else:
            me = _self_username(request)
            target_u = user_store.get_user(me) if me else None
            if not target_u:
                raise HTTPException(404, tr(request, "err.team.user_not_found"))
        chat = str(target_u.get("notify_tg_chat_id") or "").strip()
        if not chat:
            raise HTTPException(400, tr(request, "err.team.not_bound"))
        now = time.time()
        if now - _notify_test_last.get(chat, 0.0) < 30.0:
            raise HTTPException(429, tr(request, "err.team.test_too_frequent"))
        from src.integrations.notify_webhooks_store import effective_webhooks
        cfg = _runtime_config() or {}
        chan = next(
            (w for w in effective_webhooks(cfg)
             if w.get("enabled") is not False
             and str(w.get("format") or "").lower() == "telegram"
             and str(w.get("token") or "").strip()),
            None)
        if chan is None:
            # 告警渠道没接通：测试无从发起——指路接通面板而不是装成功
            raise HTTPException(503, tr(request, "err.team.no_alert_channel"))
        from src.inbox.webhook_notifier import (
            WebhookNotifier,
            _build_chat_body,
            _resolve_chat_endpoint,
        )
        url = _resolve_chat_endpoint(
            "telegram", str(chan.get("url") or ""),
            str(chan.get("token") or ""))
        body, headers = _build_chat_body(
            "telegram", tr(request, "tq_notify_test_msg"), chat,
            str(chan.get("token") or ""))
        import asyncio as _asyncio
        try:
            await _asyncio.get_event_loop().run_in_executor(
                None, WebhookNotifier._http_post, url, body, headers)
        except Exception as exc:
            # 最常见=chat not found / bot blocked：把原因透传给绑定人自查
            raise HTTPException(502, tr(
                request, "err.team.test_send_fail",
                reason=str(exc)[:120]))
        _notify_test_last[chat] = now
        if audit_store:
            audit_store.log(
                request.session.get("username", ""), "notify_test_push",
                str(target_u.get("username") or ""))
        return {"ok": True}

    # ── L3 按人权限覆写（读/写同一层级守卫；判定单点在 web_user_store）─────
    def _perm_rows(role: str, perms_json) -> list:
        """把目标用户的覆写状态展开成编辑器行（顺序=PERM_REGISTRY 定义序）。

        契约（users.html perm-modal 消费，勿改字段名）：
        [{key, domain, default: bool（角色默认允不允许）, state: inherit|allow|deny}]
        """
        overrides = parse_perms(perms_json)
        rows = []
        for key, meta in PERM_REGISTRY.items():
            if key in overrides["deny"]:
                state = "deny"
            elif key in overrides["allow"]:
                state = "allow"
            else:
                state = "inherit"
            rows.append({
                "key": key,
                "domain": str(meta.get("domain") or ""),
                "default": default_perm_allowed(role, key),
                "state": state,
            })
        return rows

    @app.get("/api/users/{user_id}/perms")
    async def api_user_perms(user_id: int, request: Request):
        """读取目标用户能力权限三态（master/admin，层级守卫与角色变更同一判定）。"""
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        _guard_target(request, str(target.get("role") or ""))
        role = str(target.get("role") or "")
        return {"ok": True, "role": role,
                "perms": _perm_rows(role, target.get("perms_json"))}

    @app.post("/users/perms/{user_id}")
    async def users_set_perms(user_id: int, request: Request):
        """写入 L3 覆写（JSON {"allow": [], "deny": []}；双空=清除回纯继承）。"""
        require_role(request, "users")
        target = user_store.get_user_by_id(user_id)
        if not target:
            raise HTTPException(404, tr(request, "err.team.user_not_found"))
        _guard_target(request, str(target.get("role") or ""))
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, tr(request, "err.team.bad_perms"))
        allow = body.get("allow") if body.get("allow") is not None else []
        deny = body.get("deny") if body.get("deny") is not None else []
        if not isinstance(allow, list) or not isinstance(deny, list):
            raise HTTPException(400, tr(request, "err.team.bad_perms"))
        if not user_store.set_user_perms(user_id, allow, deny):
            # 未注册键 / allow∩deny 冲突 / 行不存在——store 整单拒绝零写入
            raise HTTPException(400, tr(request, "err.team.bad_perms"))
        updated = user_store.get_user_by_id(user_id) or {}
        overrides = parse_perms(updated.get("perms_json"))
        if audit_store:
            audit_store.log(
                request.session.get("username", ""), "set_perms",
                f"{target.get('username')} allow={sorted(overrides['allow'])} "
                f"deny={sorted(overrides['deny'])}")
        return {"ok": True,
                "perms": _perm_rows(str(updated.get("role") or ""),
                                    updated.get("perms_json"))}

    # ── 会话管理 API ─────────────────────────────────────────
    @app.get("/api/sessions")
    async def api_sessions_list(request: Request):
        """列出所有活跃 session（仅 master）"""
        require_role(request, "users")
        sessions = user_store.list_sessions(include_revoked=False)
        current_jti = request.session.get("jti", "")
        for s in sessions:
            s["is_current"] = (s["jti"] == current_jti)
            # 脱敏 jti（仅传前8位用于展示，保留完整用于操作）
            s["jti_display"] = s["jti"][:8] + "…"
        return {"sessions": sessions, "total": len(sessions)}

    @app.post("/api/sessions/{jti}/revoke")
    async def api_session_revoke(jti: str, request: Request):
        """撤销指定 session（强制该设备下线）"""
        require_role(request, "users")
        current_jti = request.session.get("jti", "")
        if jti == current_jti:
            raise HTTPException(400, tr(request, "err.auth.cannot_kick_self"))
        if _actor_role(request) != ROLE_MASTER:
            # admin 不得踢 master/admin 的会话（与 can_manage_target 同层级语义）
            target = next(
                (s for s in user_store.list_sessions() if s.get("jti") == jti), None)
            if target and str(target.get("role") or "") in (ROLE_MASTER, ROLE_ADMIN):
                raise HTTPException(403, tr(request, "err.team.cannot_manage"))
        user_store.revoke_session(jti)
        actor = request.session.get("username", "")
        if audit_store:
            audit_store.log(actor, "revoke_session", jti[:8])
        return {"ok": True}

    @app.post("/api/sessions/revoke-all")
    async def api_sessions_revoke_all(request: Request):
        """撤销除自己外的所有 session"""
        require_role(request, "users")
        current_jti = request.session.get("jti", "")
        actor_is_master = _actor_role(request) == ROLE_MASTER
        sessions = user_store.list_sessions()
        revoked = 0
        for s in sessions:
            if s["jti"] == current_jti:
                continue
            if not actor_is_master and str(s.get("role") or "") in (ROLE_MASTER, ROLE_ADMIN):
                continue  # admin 批量踢时静默跳过 master/admin 会话，只计真正踢掉的
            user_store.revoke_session(s["jti"])
            revoked += 1
        actor = request.session.get("username", "")
        if audit_store and revoked:
            audit_store.log(actor, "revoke_all_sessions", f"revoked={revoked}")
        return {"ok": True, "revoked": revoked}

    # ── 团队字符用量（用户管理页统计条 + 每卡用量行的唯一数据源）─────────
    _USAGE_VIEW_ROLES = {ROLE_MASTER, ROLE_ADMIN, ROLE_SUPERVISOR}

    @app.get("/api/users/char-usage")
    async def api_users_char_usage(request: Request):
        """团队字符用量总览（master/admin/supervisor）。

        契约（并行 agent 消费，勿改字段名）：
        {ok, enabled, month, license{available,included,used,remaining,topup_chars,
        enforce,source}, totals{month_total,today_total,by_category},
        agents[{id,username,display_name,role,enabled,quota,alert_pct,used_month,
        used_today,by_category,status,has_overrides}]}，agents 按 used_month 降序、
        含零用量用户。

        第二批扩展（workspace_usage.html 消费，字段名钉死勿改）：
        - enforce: bool —— usage.agent_chars.enforce（缺省 False；enabled 关时也
          如实回显配置值，是否联动由消费方决定）；
        - license_month: {available, total, by_category} —— 授权池**当月**口径
          （区别于 license.used 的历史累计）；quota store 未建 → available=False 全零；
        - daily: 坐席账本近 7 天逐日序列（store.daily_series(7)；store 缺 → []）。
        """
        # 角色闸先于 require_auth：agent/viewer 拿明确 403 而非被页面闸 303 回工作台
        role = _actor_role(request)
        if role and role not in _USAGE_VIEW_ROLES:
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        require_auth(request)
        cfg = _runtime_config()
        store = ensure_store_for_read(cfg)
        month_map = store.month_totals() if store is not None else {}
        day_map = store.day_totals() if store is not None else {}
        try:
            from src.licensing.quota_store import check_license_quota
            q = check_license_quota()
            lic_id = str(q.get("lic_id") or "default")
            license_info = {
                "available": True,
                "included": int(q.get("included") or 0),
                "used": int(q.get("used") or 0),
                "remaining": (int(q["remaining"]) if q.get("remaining") is not None else None),
                "topup_chars": int(q.get("topup_chars") or 0),
                "enforce": bool(q.get("enforce")),
                "source": str(q.get("source") or ""),
            }
        except Exception:
            lic_id = ""
            license_info = {
                "available": False, "included": 0, "used": 0, "remaining": None,
                "topup_chars": 0, "enforce": False, "source": "",
            }
        # 授权池「当月」口径（license.used 是历史累计，月报表要的是本月增量）。
        # 诚实局限：LicenseQuotaStore 只有 usage()（历史累计+today）与
        # usage_history(months)（按月合计、无分桶）——当月总数取 usage_history(1)
        # 尾项（month==当月才算，否则 0）；**当月分桶拿不到**（usage().by_category
        # 是历史累计口径，混进来会虚高），by_category 恒空 dict，消费方只用 total。
        license_month = {"available": False, "total": 0, "by_category": {}}
        if lic_id:
            try:
                from src.licensing.quota_store import get_license_quota_store
                lic_store = get_license_quota_store()
                if lic_store is not None:
                    hist = lic_store.usage_history(lic_id, 1)
                    cur_month = time.strftime("%Y-%m", time.gmtime())
                    n = 0
                    if hist and str(hist[-1].get("month") or "") == cur_month:
                        n = int(hist[-1].get("chars") or 0)
                    license_month = {"available": True, "total": n, "by_category": {}}
            except Exception:
                license_month = {"available": False, "total": 0, "by_category": {}}
        month_total = 0
        today_total = 0
        by_category: dict = {}
        for slot in month_map.values():
            month_total += int(slot.get("total") or 0)
            for cat, n in (slot.get("by_category") or {}).items():
                by_category[cat] = by_category.get(cat, 0) + int(n or 0)
        for n in day_map.values():
            today_total += int(n or 0)
        agents = []
        for u in user_store.list_users():
            uname = str(u.get("username") or "")
            slot = month_map.get(uname) or {}
            used_month = int(slot.get("total") or 0)
            quota = int(u.get("monthly_char_quota") or 0)
            agents.append({
                "id": u.get("id"),
                "username": uname,
                "display_name": u.get("display_name") or uname,
                "role": u.get("role"),
                "enabled": bool(u.get("enabled")),
                "quota": quota,
                "alert_pct": int(u.get("quota_alert_pct") or 80),
                "used_month": used_month,
                "used_today": int(day_map.get(uname) or 0),
                "by_category": dict(slot.get("by_category") or {}),
                "status": agent_quota_status(used_month, quota),
                # L3 覆写在身（users.html「已覆写」徽章 / 用量页标记共用）
                "has_overrides": bool(u.get("perms_json")),
            })
        agents.sort(key=lambda a: -a["used_month"])
        # usage.agent_chars.enforce（缺省 False）：额度「超了拦不拦」的部署级开关，
        # 执法在 translate/voice/send 路由（另一条线），此处只如实回显供面板判色。
        _usage_cfg = (cfg or {}).get("usage") if isinstance(cfg, dict) else None
        _ac_cfg = (_usage_cfg or {}).get("agent_chars") if isinstance(_usage_cfg, dict) else None
        enforce = bool(_ac_cfg.get("enforce", False)) if isinstance(_ac_cfg, dict) else False
        return {
            "ok": True,
            "enabled": agent_chars_enabled(cfg),
            "enforce": enforce,
            "month": time.strftime("%Y-%m", time.gmtime()),
            "license": license_info,
            "license_month": license_month,
            "totals": {
                "month_total": month_total,
                "today_total": today_total,
                "by_category": by_category,
            },
            "daily": (store.daily_series(7) if store is not None else []),
            "agents": agents,
        }

    @app.get("/api/workspace/my-usage")
    async def api_workspace_my_usage(request: Request):
        """当前登录坐席「我的用量」（任意登录角色；坐席 API 白名单含本前缀）。

        契约（并行 agent 消费，勿改字段名）：{ok, enabled, username, quota,
        alert_pct, month_total, today_total, by_category, status, month}。
        """
        # 走 API choke point（_api_auth）：agent 白名单放行 /api/workspace 前缀；
        # 页面闸 require_auth 会把 agent 303 回 /workspace，不适用于 JSON API。
        _api_auth = getattr(request.app.state, "api_auth", None)
        if callable(_api_auth):
            _api_auth(request)
        else:  # 极端回落（测试 stub app 无 choke point）
            require_auth(request)
        username = str(request.session.get("username") or "")
        cfg = _runtime_config()
        store = ensure_store_for_read(cfg)
        usage = {"month_total": 0, "today_total": 0, "by_category": {}}
        if store is not None and username:
            usage = store.usage_for(username)
        user = user_store.get_user(username) if username else None
        # 令牌直登（admin 不在 web_users 表）→ quota=0 不限
        quota = int((user or {}).get("monthly_char_quota") or 0)
        alert_pct = int((user or {}).get("quota_alert_pct") or 80)
        month_total = int(usage.get("month_total") or 0)
        return {
            "ok": True,
            "enabled": agent_chars_enabled(cfg),
            "username": username,
            "quota": quota,
            "alert_pct": alert_pct,
            "month_total": month_total,
            "today_total": int(usage.get("today_total") or 0),
            "by_category": dict(usage.get("by_category") or {}),
            "status": agent_quota_status(month_total, quota),
            "month": time.strftime("%Y-%m", time.gmtime()),
        }
