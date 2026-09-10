# -*- coding: utf-8 -*-
"""企业微信成员扫码登录智聊（实施97 · P1「每个用户都可以用智聊登录企业微信」）——纯逻辑层。

形态（2026-09 官方「企业微信登录」，CorpApp 模式）：
    浏览器 → https://login.work.weixin.qq.com/wwlogin/sso/login?login_type=CorpApp&appid=<CorpID>&agentid=<AgentId>
             &redirect_uri=<HTTPS 可信域名>/login/wecom/callback&state=<签名 state>
    成员扫码确认 → 企微 302 回 redirect_uri?code=…&state=… → 后端用自建应用 Secret 换 access_token →
    GET auth/getuserinfo?code=… → userid → 绑定/自动开户 → 建本地会话。

**redirect_uri 必须是该企业自建应用配置的 HTTPS 可信域名**——本机 127.0.0.1 的实例满足不了，要么部署在公网域名下，
要么经官网中继（bd2026.cc）转投（中继形态只是把 code 转到本地实例，本模块的校验/换取逻辑不变）。

无 I/O：URL 构造、state 签名/校验、本地用户名规范化、配置解析。HTTP 换取在路由层经 ``WeChatKfClient``。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlencode

LOGIN_ENDPOINT = "https://login.work.weixin.qq.com/wwlogin/sso/login"
STATE_TTL_SEC = 600.0
CALLBACK_PATH = "/login/wecom/callback"
_USERID_RE = re.compile(r"[^A-Za-z0-9_\-.]")
DEFAULT_ROLE = "agent"
ALLOWED_ROLES = ("agent", "supervisor", "viewer")


def sso_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``wecom_login`` 配置块 → 规范化字典。Secret 留空时回落 ``wechat_kf.secret``（同一自建应用最常见）；
    CorpID 同理回落 ``wechat_kf.corpid``。"""
    cfg = config or {}
    blk = cfg.get("wecom_login") if isinstance(cfg.get("wecom_login"), dict) else {}
    kf = cfg.get("wechat_kf") if isinstance(cfg.get("wechat_kf"), dict) else {}
    role = str(blk.get("default_role") or DEFAULT_ROLE).strip().lower()
    if role not in ALLOWED_ROLES:
        role = DEFAULT_ROLE
    allowed = blk.get("allowed_userids") or []
    if isinstance(allowed, str):
        allowed = [x.strip() for x in allowed.split(",") if x.strip()]
    return {
        "enabled": bool(blk.get("enabled", False)),
        "corpid": str(blk.get("corpid") or kf.get("corpid") or "").strip(),
        "agentid": str(blk.get("agentid") or "").strip(),
        "secret": str(blk.get("secret") or kf.get("secret") or "").strip(),
        "redirect_base": str(blk.get("redirect_base") or "").strip().rstrip("/"),
        "default_role": role,
        "auto_provision": bool(blk.get("auto_provision", True)),
        "allowed_userids": [str(x) for x in allowed if str(x).strip()],
        "lang": str(blk.get("lang") or "zh").strip().lower()[:2] or "zh",
    }


def sso_ready(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """能否发起登录：返回 (ok, 缺什么)。"""
    if not cfg.get("enabled"):
        return False, "disabled"
    for k in ("corpid", "agentid", "secret"):
        if not cfg.get(k):
            return False, f"missing_{k}"
    return True, ""


def build_login_url(corpid: str, agentid: str, redirect_uri: str, state: str, *, lang: str = "zh") -> str:
    """官方链接形态（``redirect_uri``/``state`` 按文档 URLEncode）。"""
    q = {"login_type": "CorpApp", "appid": corpid, "agentid": agentid,
         "redirect_uri": redirect_uri, "state": state}
    if lang in ("zh", "en"):
        q["lang"] = lang
    return LOGIN_ENDPOINT + "?" + urlencode(q, quote_via=quote, safe="")


def redirect_uri_for(redirect_base: str, request_base: str, relay_base: str = "") -> str:
    """回调地址三选一（优先级从高到低）：配置的可信域名（HTTPS 公网，如 https://katie.bd2026.cc）→ 官网中继设备前缀
    （``https://relay.bd2026.cc/d/<device_id>``，NAT 后实例）→ 当前请求 origin（只适合内网测试，企微会拒非可信域名）。"""
    base = (redirect_base or relay_base or request_base or "").rstrip("/")
    return base + CALLBACK_PATH


def _b64u(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64u(s: str) -> str:
    import base64
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
    except Exception:
        return ""


def sign_state(key: str, *, now: Optional[float] = None, nonce: Optional[str] = None, origin: str = "") -> str:
    """``<ts>.<nonce>.<hmac>[.<b64url(origin)>]``——防 CSRF：只有本实例能签，10 分钟内有效；回调时还要与会话里存的一致。

    ``origin``（浏览器访问本实例用的来源，如 ``http://192.168.0.149:18799``）签进去并明文附在第 4 段：走官网中继时，
    中继据此把浏览器 302 回本地实例（只放私网/回环来源），不必过隧道。
    """
    ts = str(int(now if now is not None else time.time()))
    n = nonce or secrets.token_urlsafe(12)
    o = _b64u(origin) if origin else ""
    sig = hmac.new(str(key or "").encode("utf-8"), f"{ts}.{n}.{o}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{n}.{sig}" + (f".{o}" if o else "")


def verify_state(key: str, state: str, *, now: Optional[float] = None, ttl: float = STATE_TTL_SEC) -> bool:
    parts = str(state or "").split(".")
    if len(parts) not in (3, 4):
        return False
    ts_s, n, sig = parts[0], parts[1], parts[2]
    o = parts[3] if len(parts) == 4 else ""
    try:
        ts = int(ts_s)
    except Exception:
        return False
    t = float(now if now is not None else time.time())
    if not (0 <= t - ts <= ttl):
        return False
    want = hmac.new(str(key or "").encode("utf-8"), f"{ts_s}.{n}.{o}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(want, sig)


def state_origin(state: str) -> str:
    """state 第 4 段携带的浏览器来源（签名已覆盖它；先 verify_state 再用）。"""
    parts = str(state or "").split(".")
    return _unb64u(parts[3]) if len(parts) == 4 and parts[3] else ""


def local_username_for(userid: str) -> str:
    """企微 userid → 智聊用户名 ``wecom_<userid>``（只留字母数字 _-. ，最长 40）。"""
    s = _USERID_RE.sub("_", str(userid or "").strip())
    return ("wecom_" + s)[:40] if s else ""


def userid_allowed(cfg: Dict[str, Any], userid: str) -> bool:
    allowed = cfg.get("allowed_userids") or []
    return (not allowed) or (str(userid) in allowed)


__all__ = ["LOGIN_ENDPOINT", "STATE_TTL_SEC", "CALLBACK_PATH", "DEFAULT_ROLE", "ALLOWED_ROLES",
           "sso_config", "sso_ready", "build_login_url", "redirect_uri_for", "sign_state", "verify_state",
           "state_origin", "local_username_for", "userid_allowed"]
