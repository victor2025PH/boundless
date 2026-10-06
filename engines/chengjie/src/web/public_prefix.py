"""公网前缀模式（web_admin.public_prefix）。

场景：公网 nginx 只反代 ``/fleet/``（``/fleet/`` → 应用 ``/fleet/``，``/fleet/api/`` → 应用 ``/api/``），
站点根 ``/login``、``/static`` 等落到官网。管理台壳要从 ``/fleet/ui/*`` 访问。

开启方式（默认空 = 关闭，行为与历史逐字节一致）::

    web_admin:
      public_prefix: "/fleet/ui"        # 对外站内根路径
      # public_login_path: "/fleet/login"  # 可选；默认 = 前缀上一级 + /login

开启后：

1. ``PublicPrefixStripMiddleware``（最外层）：``/fleet/ui/x`` 进路由前剥成 ``/x``，复用原页面与 /api。
2. ``PublicPrefixRewriteMiddleware``（最内层，压缩之前）：
   - HTML/CSS/JS 正文里的站点根路径补前缀：``href/src/action/poster/formaction`` 属性、CSS ``url()``、
     以及**首段是已注册路由**的引号字面量；
   - 拼接片段（``'/api/x/'+id+'/revoke'`` 里的 ``'/revoke'``）不补——历史 VPS 手补的 ``_PAGE`` 正则
     会把它改成 ``.../fleet/ui/revoke``，写操作全坏（2026-10-05/06 控制台事故）；
   - 3xx ``Location`` 的站点根路径补前缀；``/login``（正文与 Location）落到 ``public_login_path``；
   - JSON、附件、已压缩响应、已带挂载根（``/fleet``）或 ``/downloads`` 的地址一律不动。
3. ``<mount>/static`` 静态挂载、``public_login_path`` 登录别名与 CSRF 豁免由 admin.create_app 按需注册。
4. base.html 在前缀模式下注入运行时兜底（fetch/XHR/sendBeacon/点击/提交补前缀），默认不渲染。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, Mapping, Optional
from urllib.parse import quote

_SEG_RE = re.compile(r"^(/[A-Za-z0-9._\-]+)+$")

# 拼接片段里仍按「完整地址开头」处理的首段（host + '/api/...'、'//'+host+'/ws/...'）
_PATH_START_SEGS = frozenset({"api", "static", "ws", "i18n", "copilot", "assistant-shared"})
_MAX_REWRITE_BYTES = 6_000_000


@dataclass(frozen=True)
class PublicPrefix:
    prefix: str = ""
    login_path: str = ""
    static_prefix: str = ""
    exempt: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def enabled(self) -> bool:
        return bool(self.prefix)

    @property
    def mount_root(self) -> str:
        """前缀首段（/fleet/ui → /fleet）；该段下的地址视为已在公网命名空间内。"""
        if not self.prefix:
            return ""
        return "/" + self.prefix.strip("/").split("/", 1)[0]


DISABLED = PublicPrefix()


def _clean_path(value: Any) -> str:
    s = str(value or "").strip().rstrip("/")
    if not _SEG_RE.match(s) or any(seg in (".", "..") for seg in s.split("/")):
        return ""
    return s


def resolve_public_prefix(config: Optional[Mapping[str, Any]]) -> PublicPrefix:
    """读 web_admin.public_prefix；非法值（非 /a/b 形式）按关闭处理。"""
    web = {}
    if isinstance(config, Mapping):
        web = config.get("web_admin") or {}
    if not isinstance(web, Mapping):
        return DISABLED
    prefix = _clean_path(web.get("public_prefix"))
    if not prefix:
        return DISABLED
    parent = prefix.rsplit("/", 1)[0]
    login = _clean_path(web.get("public_login_path")) or ((parent or prefix) + "/login")
    if login == "/login":
        login = prefix + "/login"
    static = (parent + "/static") if parent else (prefix + "/static")
    mount_seg = prefix.strip("/").split("/", 1)[0]
    exempt = frozenset({mount_seg, "downloads", "sw.js"})
    return PublicPrefix(prefix=prefix, login_path=login, static_prefix=static, exempt=exempt)


def _first_seg(path: str) -> str:
    rest = path[1:]
    for i, ch in enumerate(rest):
        if ch in "/?#":
            return rest[:i]
    return rest


def route_roots(routes: Iterable[Any]) -> FrozenSet[str]:
    """已注册路由/挂载的首段集合（/api/x/{id} → api）。"""
    out = set()
    for r in routes or ():
        p = getattr(r, "path", None)
        if not isinstance(p, str) or not p.startswith("/"):
            continue
        seg = _first_seg(p)
        if seg and "{" not in seg:
            out.add(seg)
    return frozenset(out)


class PublicPrefixStripMiddleware:
    """最外层：``<prefix>/x`` → ``/x``（http 与 websocket）。"""

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path") or ""
            p = self.prefix
            if p and (path == p or path.startswith(p + "/")):
                rest = path[len(p):] or "/"
                raw = scope.get("raw_path")
                pb = p.encode("ascii")
                scope = dict(scope)
                scope["path"] = rest
                if isinstance(raw, (bytes, bytearray)) and (raw == pb or raw.startswith(pb + b"/")):
                    scope["raw_path"] = bytes(raw[len(pb):]) or b"/"
                else:
                    scope["raw_path"] = quote(rest, safe="/%:@!$&'()*+,;=~").encode("ascii", "replace")
        await self.app(scope, receive, send)


class PrefixRewriter:
    """正文/Location 改写（纯函数部分，便于单测）。"""

    _ATTR = re.compile(
        r"""(?P<attr>\b(?:href|src|action|poster|formaction)\s*=\s*)(?P<q>["'])(?P<path>/(?!/)[^"'<>]*)(?P=q)""",
        re.I,
    )
    _URL = re.compile(r"""url\(\s*(?P<q>["']?)(?P<path>/(?!/)[^)"'\s]*)(?P=q)\s*\)""", re.I)
    _QUOTED = re.compile(r"""(?P<q>["'`])(?P<path>/[A-Za-z][^"'`\s\\<>]{0,300})(?P=q)""")
    _CONCAT_TAIL = re.compile(r"""\+=?\s*$""")
    _ORIGIN_TAIL = re.compile(r"""(?:origin|host|['"]//['"]|['"]https?://['"])\s*\+=?\s*$""")

    def __init__(self, pp: PublicPrefix, roots: Iterable[str] = ()):
        self.pp = pp
        self.roots = frozenset(roots)

    def is_exempt(self, path: str) -> bool:
        if not path.startswith("/") or path.startswith("//"):
            return True
        return _first_seg(path) in self.pp.exempt

    def prefix(self, path: str) -> str:
        if self.is_exempt(path):
            return path
        bare = path.split("?", 1)[0].split("#", 1)[0]
        if bare == "/login" and self.pp.login_path:
            return self.pp.login_path + path[len("/login"):]
        if path == "/":
            return self.pp.prefix + "/"
        return self.pp.prefix + path

    def location(self, loc: str) -> str:
        return self.prefix(loc)

    def _quoted_ok(self, text: str, start: int, path: str) -> bool:
        seg = _first_seg(path)
        if seg not in self.roots or seg in self.pp.exempt:
            return False
        head = text[max(0, start - 80):start]
        if self._CONCAT_TAIL.search(head):
            # 拼接片段：只有「源/主机 + 完整路径」或强路径首段才补
            return bool(self._ORIGIN_TAIL.search(head)) or seg in _PATH_START_SEGS
        return True

    def rewrite_text(self, text: str, ctype: str) -> str:
        low = (ctype or "").lower()
        text = self._ATTR.sub(
            lambda m: m.group("attr") + m.group("q") + self.prefix(m.group("path")) + m.group("q"), text)
        text = self._URL.sub(
            lambda m: "url(" + (m.group("q") or "") + self.prefix(m.group("path")) + (m.group("q") or "") + ")",
            text)
        if "javascript" in low or "html" in low:
            def quoted(m):
                path = m.group("path")
                if not self._quoted_ok(m.string, m.start(), path):
                    return m.group(0)
                return m.group("q") + self.prefix(path) + m.group("q")
            text = self._QUOTED.sub(quoted, text)
        return text

    def rewrite_body(self, raw: bytes, ctype: str) -> bytes:
        if len(raw) > _MAX_REWRITE_BYTES:
            return raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw
        return self.rewrite_text(text, ctype).encode("utf-8")


def _rewriteable(ctype: str) -> bool:
    c = (ctype or "").lower()
    return "text/html" in c or "text/css" in c or "javascript" in c


def _hget(headers, name: bytes) -> str:
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1", "replace")
    return ""


class PublicPrefixRewriteMiddleware:
    """最内层（压缩之前）：改写 HTML/CSS/JS 正文与重定向 Location。"""

    def __init__(self, app, pp: PublicPrefix):
        self.app = app
        self.pp = pp
        self._rw: Optional[PrefixRewriter] = None

    def _rewriter(self, scope) -> PrefixRewriter:
        if self._rw is None:
            app = scope.get("app")
            self._rw = PrefixRewriter(self.pp, route_roots(getattr(app, "routes", ()) or ()))
        return self._rw

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rw = self._rewriter(scope)
        state = {"pass": False, "start": None}
        chunks = []

        async def send_wrap(message):
            t = message["type"]
            if t == "http.response.start":
                headers = list(message.get("headers") or [])
                loc = _hget(headers, b"location")
                if loc:
                    new = rw.location(loc)
                    if new != loc:
                        headers = [(k, v) for k, v in headers if k.lower() != b"location"]
                        headers.append((b"location", new.encode("latin-1", "replace")))
                        message = dict(message, headers=headers)
                ctype = _hget(headers, b"content-type")
                if (_hget(headers, b"content-encoding")
                        or "attachment" in _hget(headers, b"content-disposition").lower()
                        or not _rewriteable(ctype)):
                    state["pass"] = True
                    await send(message)
                    return
                state["start"] = (message, headers, ctype)
                return
            if t != "http.response.body" or state["pass"]:
                await send(message)
                return
            chunks.append(message.get("body") or b"")
            if message.get("more_body"):
                return
            start, headers, ctype = state["start"]
            body = rw.rewrite_body(b"".join(chunks), ctype)
            drop = {b"etag", b"content-md5", b"content-length"}
            headers = [(k, v) for k, v in headers if k.lower() not in drop]
            headers.append((b"content-length", str(len(body)).encode()))
            await send(dict(start, headers=headers))
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrap)


def add_login_alias(app, login_path: str) -> int:
    """把 /login 的 GET/POST 端点原样挂到 login_path（如 /fleet/login）。返回新增条数。"""
    if not login_path or login_path == "/login":
        return 0
    from fastapi.routing import APIRoute

    have = {(getattr(r, "path", None), m) for r in app.routes for m in (getattr(r, "methods", None) or ())}
    added = 0
    for r in list(app.routes):
        if not isinstance(r, APIRoute) or r.path != "/login":
            continue
        methods = sorted(m for m in (r.methods or ()) if (login_path, m) not in have and m != "HEAD")
        if not methods:
            continue
        app.add_api_route(login_path, r.endpoint, methods=methods, response_class=r.response_class,
                          include_in_schema=False, name=f"public_{r.name}")
        added += 1
    return added


def install(app, pp: PublicPrefix) -> None:
    """注册登录别名与两层中间件：剥前缀最外层、改写最内层（必须在 create_app 末尾调用）。"""
    if not pp.enabled:
        return
    from starlette.middleware import Middleware

    add_login_alias(app, pp.login_path)
    app.user_middleware.append(Middleware(PublicPrefixRewriteMiddleware, pp=pp))
    app.add_middleware(PublicPrefixStripMiddleware, prefix=pp.prefix)
