# -*- coding: utf-8 -*-
"""认证与用户管理路由。"""
import hashlib
import json
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse

from src.host.device_registry import config_file

router = APIRouter(tags=["auth"])

_users_path = config_file("users.json")

# ── API Key 鉴权 ──
_API_KEY = os.environ.get("OPENCLAW_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_SESSION_TTL = int(os.environ.get("OPENCLAW_SESSION_TTL", "28800"))
_active_sessions: dict = {}
_login_failures: dict = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300
_SESSION_TOKEN = os.environ.get("OPENCLAW_SESSION_TOKEN", "")


# ── 鉴权验证 ──

async def verify_api_key(request: Request, key: Optional[str] = Security(_api_key_header)):
    """允许 X-API-Key，或与登录页相同的会话 Token（Cookie / Bearer），避免仪表盘在开启 API_KEY 时全部 401。"""
    if not _API_KEY:
        return
    if key == _API_KEY:
        return
    token = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if not token:
        token = (request.cookies.get("oc_token") or "").strip()
    try:
        _cleanup_expired_sessions()
    except Exception:
        pass
    sess = _active_sessions.get(token)
    if sess and sess.get("expires", 0) > time.time():
        return
    raise HTTPException(status_code=401, detail="无效的 API Key 或会话已过期")


def _cleanup_expired_sessions():
    now = time.time()
    expired = [k for k, v in _active_sessions.items() if v["expires"] < now]
    for k in expired:
        _active_sessions.pop(k, None)


# ── Phase-2: Role ACL ────────────────────────────────────────────────
def _get_session_user(request: Request) -> Optional[dict]:
    """从 cookie / Authorization 拿当前 session, 返 user dict 或 None."""
    token = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if not token:
        token = (request.cookies.get("oc_token") or "").strip()
    if not token:
        return None
    sess = _active_sessions.get(token)
    if not sess or sess.get("expires", 0) <= time.time():
        return None
    return sess


def get_current_user_role(request: Request) -> str:
    """返当前用户的 role, 未登录或 internal-mode (api key 空) 返 ''."""
    sess = _get_session_user(request)
    return (sess or {}).get("role", "")


def get_current_username(request: Request) -> str:
    sess = _get_session_user(request)
    return (sess or {}).get("user", "")


def requires_role(*allowed_roles: str):
    """FastAPI dependency factory: 限制 endpoint 仅给指定 role 用户.

    使用:
        @router.post("/admin/something",
                     dependencies=[Depends(requires_role("admin"))])

    内网兼容模式 (_API_KEY 空) 时仍要 role 检查 (因为我们不希望客服能调).
    用 X-API-Key 调时算 admin (机器对机器场景).
    """
    allowed = {r.lower() for r in allowed_roles}

    async def _check(request: Request,
                      key: Optional[str] = Security(_api_key_header)):
        # 1) X-API-Key 通行 (机器调用)
        if _API_KEY and key == _API_KEY:
            return
        # 2) session: 看 role 在白名单内
        sess = _get_session_user(request)
        if not sess:
            raise HTTPException(status_code=401, detail="未登录")
        role = (sess.get("role") or "").lower()
        if role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足: 需要 {', '.join(allowed)}, 当前 role={role}",
            )

    return _check


# ── 密码哈希（PBKDF2 升级） ──

def _hash_password(password: str, salt: str = None) -> str:
    """PBKDF2-SHA256 密码哈希 (100000次迭代)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"pbkdf2:{salt}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """验证密码，兼容旧 SHA256 格式。"""
    if stored.startswith("pbkdf2:"):
        _, salt, hash_hex = stored.split(":", 2)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return dk.hex() == hash_hex
    # 兼容旧格式
    if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
        old_hash = hashlib.sha256(("openclaw_salt_" + password).encode()).hexdigest()
        return old_hash == stored
    return password == stored


# ── 用户存储 ──

def _load_users() -> list:
    if _users_path.exists():
        with open(_users_path, "r", encoding="utf-8") as f:
            return json.load(f)
    default_hash = _hash_password("admin")
    default_users = [{"username": "admin", "password": default_hash, "role": "admin", "display": "管理员"}]
    _save_users(default_users)
    return default_users


def _save_users(data: list):
    _users_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_users_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 登录锁定 ──

def _check_login_lockout(client_ip: str):
    info = _login_failures.get(client_ip)
    if not info:
        return
    if info.get("locked_until", 0) > time.time():
        remaining = int(info["locked_until"] - time.time())
        raise HTTPException(429, f"登录已锁定，请 {remaining} 秒后重试")


def _record_login_failure(client_ip: str):
    info = _login_failures.setdefault(client_ip, {"count": 0, "locked_until": 0})
    info["count"] += 1
    if info["count"] >= _LOGIN_MAX_ATTEMPTS:
        info["locked_until"] = time.time() + _LOGIN_LOCKOUT_SECONDS
        info["count"] = 0


def _clear_login_failure(client_ip: str):
    _login_failures.pop(client_ip, None)


# ── 认证端点 ──

@router.post("/auth/login")
async def login(body: dict, request: Request):
    """Browser login with password hash + lockout."""
    client_ip = request.client.host if request.client else "unknown"
    _check_login_lockout(client_ip)

    username = body.get("username", "")
    password = body.get("password", "")

    if not _SESSION_TOKEN and not _API_KEY and not username:
        token = secrets.token_urlsafe(32)
        _active_sessions[token] = {"expires": time.time() + _SESSION_TTL, "role": "admin", "user": "guest"}
        _clear_login_failure(client_ip)
        return {"token": token, "expires_in": _SESSION_TTL, "role": "admin", "user": "guest"}

    if username:
        users = _load_users()
        user = next((u for u in users if u["username"] == username), None)
        if user and _verify_password(password, user["password"]):
            token = secrets.token_urlsafe(32)
            _active_sessions[token] = {
                "expires": time.time() + _SESSION_TTL,
                "role": user.get("role", "operator"),
                "user": username,
            }
            _clear_login_failure(client_ip)
            _cleanup_expired_sessions()
            return {"token": token, "expires_in": _SESSION_TTL, "role": user["role"],
                    "user": username, "display": user.get("display", username)}

    if password and (password == _SESSION_TOKEN or password == _API_KEY):
        token = secrets.token_urlsafe(32)
        _active_sessions[token] = {"expires": time.time() + _SESSION_TTL, "role": "admin", "user": "api"}
        _clear_login_failure(client_ip)
        return {"token": token, "expires_in": _SESSION_TTL, "role": "admin", "user": "api"}

    _record_login_failure(client_ip)
    fail_info = _login_failures.get(client_ip, {})
    remaining = _LOGIN_MAX_ATTEMPTS - fail_info.get("count", 0)
    raise HTTPException(status_code=401, detail=f"用户名或密码错误 (剩余 {remaining} 次尝试)")


@router.post("/auth/logout")
async def logout(body: dict):
    token = body.get("token", "")
    _active_sessions.pop(token, None)
    return {"ok": True}


@router.get("/auth/me")
async def auth_me(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("oc_token", "")
    session = _active_sessions.get(token)
    if not session or session["expires"] < time.time():
        _active_sessions.pop(token, None)
        return {"authenticated": False}
    session["expires"] = time.time() + _SESSION_TTL
    return {"authenticated": True, "user": session["user"], "role": session["role"]}


# ── 用户管理端点 ──

@router.get("/auth/users", dependencies=[Depends(verify_api_key)])
def list_users():
    users = _load_users()
    return [{"username": u["username"], "role": u["role"], "display": u.get("display", "")} for u in users]


@router.post("/auth/users", dependencies=[Depends(requires_role("admin"))])
def create_user(body: dict):
    users = _load_users()
    if any(u["username"] == body.get("username") for u in users):
        raise HTTPException(400, "用户已存在")
    users.append({
        "username": body["username"],
        "password": _hash_password(body.get("password", "123456")),
        "role": body.get("role", "operator"),
        "display": body.get("display", body["username"]),
    })
    _save_users(users)
    return {"ok": True}


@router.put("/auth/users/{username}", dependencies=[Depends(verify_api_key)])
def update_user(username: str, body: dict):
    users = _load_users()
    for u in users:
        if u["username"] == username:
            if "password" in body:
                u["password"] = _hash_password(body["password"])
            if "role" in body:
                u["role"] = body["role"]
            if "display" in body:
                u["display"] = body["display"]
            _save_users(users)
            return {"ok": True}
    raise HTTPException(404, "用户不存在")


@router.delete("/auth/users/{username}", dependencies=[Depends(verify_api_key)])
def delete_user(username: str):
    users = _load_users()
    users = [u for u in users if u["username"] != username]
    _save_users(users)
    return {"ok": True}


# ── 登录页 ──

@router.get("/login", response_class=HTMLResponse)
def login_page():
    """Serve login page."""
    return _LOGIN_HTML


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OpenClaw 登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#0b1120;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:40px;width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.login-logo{text-align:center;margin-bottom:24px}
.login-logo h1{font-size:28px;font-weight:700;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.login-logo p{font-size:12px;color:#64748b;margin-top:4px}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px}
.form-group input{width:100%;padding:12px 14px;background:#0f172a;border:1px solid #334155;border-radius:10px;color:#e2e8f0;font-size:14px;outline:none;transition:border .2s}
.form-group input:focus{border-color:#3b82f6}
.login-btn{width:100%;padding:12px;background:#3b82f6;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:all .2s;margin-top:8px}
.login-btn:hover{background:#2563eb;transform:translateY(-1px)}
.login-btn:disabled{background:#475569;cursor:wait}
.login-error{color:#f87171;font-size:12px;margin-top:8px;text-align:center;display:none}
.login-footer{text-align:center;margin-top:20px;font-size:11px;color:#475569}
</style></head><body>
<div class="login-card">
  <div class="login-logo"><h1>OpenClaw</h1><p>群控自动化管理系统</p></div>
  <form onsubmit="doLogin(event)">
    <div class="form-group"><label>用户名</label><input id="login-user" type="text" placeholder="admin" autocomplete="username"/></div>
    <div class="form-group"><label>密码</label><input id="login-pass" type="password" placeholder="请输入密码" autocomplete="current-password"/></div>
    <button type="submit" class="login-btn" id="login-btn">登 录</button>
    <div class="login-error" id="login-error"></div>
  </form>
  <div class="login-footer">OpenClaw v1.1.0 &copy; 2024-2026</div>
</div>
<script>
async function doLogin(e){
  e.preventDefault();
  const btn=document.getElementById('login-btn');
  const err=document.getElementById('login-error');
  btn.disabled=true; err.style.display='none';
  const username=document.getElementById('login-user').value.trim();
  const password=document.getElementById('login-pass').value;
  try{
    const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
    if(!r.ok){const d=await r.json();throw new Error(d.detail||'登录失败');}
    const data=await r.json();
    document.cookie='oc_token='+data.token+';path=/;max-age=86400';
    localStorage.setItem('oc_token',data.token);
    localStorage.setItem('oc_user',data.user||username);
    localStorage.setItem('oc_role',data.role||'operator');
    window.location.href='/dashboard';
  }catch(ex){
    err.textContent=ex.message;err.style.display='block';
  }finally{btn.disabled=false;}
}
if(localStorage.getItem('oc_token')){
  fetch('/auth/me',{headers:{'Authorization':'Bearer '+localStorage.getItem('oc_token')}})
    .then(r=>r.json()).then(d=>{if(d.authenticated)window.location.href='/dashboard';});
}
</script></body></html>"""


# 供其他模块导入
def get_active_sessions():
    return _active_sessions
