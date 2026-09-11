"""登录后回跳（``?next=``）安全解析 — 防开放重定向。

托管租户交付串会带 ``/login?next=/workspace/dash``，让客户首登直达上线自检看板。
规则刻意收紧：只接受同站相对路径，拒绝 ``//evil``、``https://…``、反斜杠与控制字符。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

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


def _strip_lang_query(query: str) -> str:
    """去掉 query 里的 ``lang``（含嵌套在 ``next`` 里的），其余键值原样保序。"""
    if not query:
        return ""
    kept = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k == "lang":
            continue
        if k == "next":
            nxt = safe_next_path(v)
            if nxt:
                p, _, q = nxt.partition("?")
                q2 = _strip_lang_query(q)
                v = f"{p}?{q2}" if q2 else p
        kept.append((k, v))
    return urlencode(kept, doseq=False)


def _safe_fragment(fragment: str) -> str:
    """回跳地址可携带的 ``#fragment``（同页锚点/会话 hash 路由）；控制字符/超长即丢弃。"""
    f = str(fragment or "")
    if not f or len(f) > _MAX_NEXT_LEN or any(ord(ch) < 32 for ch in f):
        return ""
    return f"#{f}"


def set_lang_redirect_target(referer: Optional[str], next_raw: Optional[str] = None) -> str:
    """``/set_lang`` 切完语言后的回跳地址（2026-09-12 切语言不生效事故）。

    此前直接 303 回 Referer 原文。桌面壳把工作台加载成 ``/workspace?lang=zh_hant&theme=dark``
    （首帧对齐用），中间件又是 ``?lang=`` > cookie——回跳后 query 再次压过刚写的 cookie，
    页面纹丝不动；壳侧却同步了配置改了五个菜单，形成「菜单变了正文没变」的分裂。

    规则：优先页面显式传的 ``next``（同站路径校验；**保留 #fragment**——Referer 不带
    hash，工作台的会话/标签 hash 路由切完语言会丢位），其次 Referer 的站内 path+query
    （防开放重定向）；两者都删掉 ``lang``（含 ``next`` 里嵌套的），其余参数
    （theme/conv…）原样保留；都没有 / 解析失败 → ``/``。
    """
    nxt_s = str(next_raw or "").strip()
    if nxt_s:
        frag = ""
        if "#" in nxt_s:
            nxt_s, _, frag_raw = nxt_s.partition("#")
            frag = _safe_fragment(frag_raw)
        nxt = safe_next_path(nxt_s)
        if nxt and nxt.split("?", 1)[0].rstrip("/") != "/set_lang":
            p, _, q = nxt.partition("?")
            q2 = _strip_lang_query(q)
            return (f"{p}?{q2}" if q2 else p) + frag
    raw = str(referer or "").strip()
    if not raw or len(raw) > 4 * _MAX_NEXT_LEN:
        return "/"
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return "/"
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    if any(ord(ch) < 32 for ch in path):
        return "/"
    # 回跳到 /set_lang 自身会成环（理论上不会有这种 Referer，防御一下）
    if path.rstrip("/") == "/set_lang":
        return "/"
    q = _strip_lang_query(parsed.query or "")
    return f"{path}?{q}" if q else path


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
