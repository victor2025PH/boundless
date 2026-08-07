"""登录后回跳（``?next=``）安全解析 — 防开放重定向。

托管租户交付串会带 ``/login?next=/workspace/dash``，让客户首登直达上线自检看板。
规则刻意收紧：只接受同站相对路径，拒绝 ``//evil``、``https://…``、反斜杠与控制字符。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote, unquote, urlparse

# 回跳目标长度上限（防畸形 URL / 日志撑爆）
_MAX_NEXT_LEN = 512

# 登录失败重渲染时仍保留的表单字段名
NEXT_FORM_FIELD = "next"

# 托管交付默认首登落地（今日概览 = 上线自检红灯入口）
DEFAULT_ONBOARD_NEXT = "/workspace/dash"


def safe_next_path(raw: Optional[str]) -> str:
    """把用户可控的 next 收成安全的站内路径，否则返回空串。

    合法例：``/workspace/dash``、``/workspace/golive?tab=ai``。
    非法例：``https://evil.com``、``//evil.com``、``/\\evil``、空、相对无前导斜杠。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or len(s) > _MAX_NEXT_LEN:
        return ""
    try:
        s = unquote(s)
    except Exception:  # noqa: BLE001
        return ""
    if any(ord(ch) < 32 for ch in s):
        return ""
    if "\\" in s or "://" in s:
        return ""
    if not s.startswith("/"):
        return ""
    # protocol-relative //host 与 "////" 一律拒
    if s.startswith("//"):
        return ""
    parsed = urlparse(s)
    if parsed.scheme or parsed.netloc:
        return ""
    path = parsed.path or ""
    if not path.startswith("/") or path.startswith("//"):
        return ""
    out = path
    if parsed.query:
        out = f"{out}?{parsed.query}"
    if len(out) > _MAX_NEXT_LEN:
        return ""
    return out


def login_url_with_next(base: str, next_path: str = DEFAULT_ONBOARD_NEXT) -> str:
    """拼交付/中间件用的登录深链。

    ``base`` 可以是站点根（``https://host``）或已含 ``/login`` 的地址。
    """
    b = (base or "").rstrip("/")
    if not b:
        b = "/login"
    elif not b.endswith("/login"):
        b = f"{b}/login"
    nxt = safe_next_path(next_path) or DEFAULT_ONBOARD_NEXT
    return f"{b}?next={quote(nxt, safe='')}"


def resolve_post_login_dest(*, next_raw: Optional[str], role_default: str) -> str:
    """登录成功落地：合法 next 优先，否则角色默认。挡回 ``/login`` 自身防环。"""
    nxt = safe_next_path(next_raw)
    if not nxt:
        return role_default
    path_only = nxt.split("?", 1)[0].rstrip("/") or "/"
    if path_only == "/login":
        return role_default
    return nxt


def login_redirect_location(request_path: str, request_query: str = "") -> str:
    """未登录页鉴权失败时的 Location：保留原路径为 next。"""
    raw = request_path or "/"
    if request_query:
        raw = f"{raw}?{request_query}"
    # 已在登录相关页 → 不套娃
    path_only = (request_path or "/").split("?", 1)[0].rstrip("/") or "/"
    if path_only in ("/login", "/logout", "/setup"):
        return "/login"
    nxt = safe_next_path(raw)
    if not nxt:
        return "/login"
    return login_url_with_next("/login", nxt)
