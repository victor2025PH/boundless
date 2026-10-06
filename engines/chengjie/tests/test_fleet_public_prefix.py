"""公网前缀模式（web_admin.public_prefix）：把 VPS 上手补的 /fleet/ui 补丁收回仓库。

守住：
1. 默认关：不配 public_prefix 时行为与历史一致（无 /fleet/login、无运行时兜底脚本、base.html 输出不变）；
2. 开启后与 VPS 现网口径一致：/fleet/ui/* 剥前缀复用原路由、/fleet/login 登录、/fleet/static 静态；
3. 改写不再出 VPS ``_PAGE`` 正则的事故：拼接片段（'/revoke'、'/tasks'）不补前缀、不会补两遍；
4. 改写幂等：对已改写的页面再跑一遍不变（不存在 /fleet/ui/fleet/ui）。
"""
from __future__ import annotations

import asyncio
import re

import pytest
import yaml
from starlette.testclient import TestClient

from src.web.public_prefix import (
    DISABLED, PrefixRewriter, resolve_public_prefix, route_roots,
)

_TOKEN = "pp-test-token-0123456789"
_BROKEN_WRITE = re.compile(r"""["'`]/fleet/ui/(?:revoke|tasks|approve|reject|cancel|login-qr|status)\b""")


def _pp(**web):
    return resolve_public_prefix({"web_admin": web})


# ── 配置解析 ──────────────────────────────────────────────────────────────

def test_resolve_default_and_invalid_values_disable():
    assert resolve_public_prefix({}) is DISABLED
    assert resolve_public_prefix(None) is DISABLED
    assert not DISABLED.enabled
    for bad in ("", "fleet/ui", "/fleet ui", "https://x/fleet", "/fleet/../x", "//fleet", 123, None):
        assert not _pp(public_prefix=bad).enabled, bad


def test_resolve_fleet_ui_matches_vps_layout():
    pp = _pp(public_prefix="/fleet/ui/")
    assert pp.prefix == "/fleet/ui"
    assert pp.login_path == "/fleet/login"
    assert pp.static_prefix == "/fleet/static"
    assert pp.mount_root == "/fleet"
    assert {"fleet", "downloads", "sw.js"} <= set(pp.exempt)
    assert _pp(public_prefix="/fleet/ui", public_login_path="/fleet/ui/login").login_path == "/fleet/ui/login"
    single = _pp(public_prefix="/ui")
    assert single.login_path == "/ui/login" and single.static_prefix == "/ui/static"


# ── 改写规则（纯函数） ────────────────────────────────────────────────────

ROOTS = {"api", "static", "workspace", "templates", "login", "logout", "set_lang", "ws", "knowledge", "fleet"}


@pytest.fixture
def rw():
    return PrefixRewriter(_pp(public_prefix="/fleet/ui"), ROOTS)


def test_attributes_css_and_location(rw):
    html = ('<a href="/workspace">w</a><link href="/static/a.css"><img src="/x.png">'
            '<a href="/">home</a><a href="/fleet/console">c</a><a href="/downloads/a.exe">d</a>'
            '<a href="https://e.com/x">e</a><a href="//cdn/x">f</a><form action="/login">'
            '<style>b{background:url(/static/bg.png)}</style>')
    out = rw.rewrite_text(html, "text/html; charset=utf-8")
    assert 'href="/fleet/ui/workspace"' in out and 'href="/fleet/ui/static/a.css"' in out
    assert 'src="/fleet/ui/x.png"' in out and 'href="/fleet/ui/"' in out
    assert 'href="/fleet/console"' in out and 'href="/downloads/a.exe"' in out
    assert 'href="https://e.com/x"' in out and 'href="//cdn/x"' in out
    assert 'action="/fleet/login"' in out
    assert "url(/fleet/ui/static/bg.png)" in out
    assert rw.location("/login?next=%2Fworkspace") == "/fleet/login?next=%2Fworkspace"
    assert rw.location("/workspace") == "/fleet/ui/workspace"
    assert rw.location("/fleet/console") == "/fleet/console"
    assert rw.location("https://other/x") == "https://other/x"


def test_concat_fragments_are_not_prefixed(rw):
    """VPS _PAGE 事故：'/revoke' 这类拼接片段被补成 /fleet/ui/revoke，写操作全坏。"""
    js = ("fetch('/api/fleet/nodes/'+id+'/revoke',{method:'POST'});"
          "u = base + '/knowledge';  v += '/templates';"
          "var w=location.origin+'/workspace'; var s=proto+'//'+location.host+'/ws/live';"
          "go('/templates'); x = cond ? '/workspace' : '/knowledge';")
    out = rw.rewrite_text(js, "application/javascript")
    assert "fetch('/fleet/ui/api/fleet/nodes/'+id+'/revoke'" in out
    assert "base + '/knowledge'" in out and "v += '/templates'" in out
    assert "location.origin+'/fleet/ui/workspace'" in out
    assert "location.host+'/fleet/ui/ws/live'" in out
    assert "go('/fleet/ui/templates')" in out
    assert "? '/fleet/ui/workspace' : '/fleet/ui/knowledge'" in out


def test_quoted_literals_only_for_registered_roots(rw):
    js = "cmd('/start'); a='/help'; b='/api/x'; c='/'; d='/fleet/ui/api/x'; e='/notaroute/x';"
    out = rw.rewrite_text(js, "text/html")
    assert "cmd('/start')" in out and "a='/help'" in out and "e='/notaroute/x'" in out
    assert "b='/fleet/ui/api/x'" in out and "c='/'" in out and "d='/fleet/ui/api/x'" in out
    # CSS 不碰引号字面量；JSON 根本不进改写（见集成用例）
    assert rw.rewrite_text("a{content:'/api/x'}", "text/css") == "a{content:'/api/x'}"


def test_rewrite_is_idempotent(rw):
    html = ('<a href="/workspace">x</a><script>fetch("/api/a/"+i+"/tasks");location.href="/login?next=/x";'
            "var n={d:'/',t:'/templates'};</script>")
    once = rw.rewrite_text(html, "text/html")
    assert rw.rewrite_text(once, "text/html") == once
    assert "/fleet/ui/fleet/ui" not in once and "/fleet/ui/fleet/login" not in once


def test_route_roots_from_routes():
    class R:
        def __init__(self, p):
            self.path = p
    assert route_roots([R("/api/x/{id}"), R("/static"), R("/"), R("/{full_path}"), R("/fleet/console")]) == {
        "api", "static", "fleet"}


# ── 整站（create_app）集成 ────────────────────────────────────────────────

def _make_app(tmp_path, web_extra=None, fleet_extra=None):
    web = {"secret_key": "test-secret-very-long-key-for-testing", "auth_token": _TOKEN,
           "session_max_age": 3600}
    web.update(web_extra or {})
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "fleet_control",
        "fleet_control": {"db_path": str(tmp_path / "fleet.db"), **(fleet_extra or {})},
        "web_admin": web,
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    from src.utils.audit_store import AuditStore
    from src.utils.config_manager import ConfigManager
    from src.web.admin import create_app
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    asyncio.run(cm.load())
    return create_app(cm, audit_store=AuditStore(db_path=tmp_path / "audit.db"), boot_ts=0,
                      telegram_client=None, event_tracker=None, log_buffer=None)


def _login(c, path):
    r = c.post(path, data={"auth_token": _TOKEN}, follow_redirects=False)
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return r


def test_default_mode_unchanged(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/fleet/login", follow_redirects=False).status_code == 404
        assert c.get("/fleet/ui/login", follow_redirects=False).status_code == 404
        r = c.get("/login")
        assert r.status_code == 200 and 'action="/login"' in r.text and "/fleet/ui" not in r.text
        r = c.get("/workspace", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        _login(c, "/login")
        r = c.get("/fleet/console")
        assert r.status_code == 200
        assert 'data-api-base=""' in r.text
        assert "__publicPrefixFix" not in r.text


def test_prefix_mode_login_redirects_and_static(tmp_path):
    app = _make_app(tmp_path, {"public_prefix": "/fleet/ui"})
    with TestClient(app) as c:
        r = c.get("/fleet/ui/workspace", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/fleet/login?next=")
        r = c.get("/fleet/console", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/fleet/login?next=%2Ffleet%2Fconsole"
        for p in ("/fleet/login", "/fleet/ui/login"):
            r = c.get(p)
            assert r.status_code == 200, p
            assert 'action="/fleet/login"' in r.text and "/fleet/ui/fleet" not in r.text
            assert 'href="/static/' not in r.text
        assert c.get("/fleet/ui/static/brand/brand.css").status_code == 200
        assert c.get("/fleet/static/brand/brand.css").status_code == 200
        r = _login(c, "/fleet/login")                       # 别名 POST：CSRF 豁免 + 同一端点
        assert not r.headers["location"].startswith("/login")
        r = c.get("/fleet/ui/logout", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/fleet/login?manual=1")


def test_prefix_mode_console_and_shell_pages(tmp_path):
    app = _make_app(tmp_path, {"public_prefix": "/fleet/ui"})
    with TestClient(app) as c:
        _login(c, "/fleet/login")
        r = c.get("/fleet/console")
        assert r.status_code == 200
        html = r.text
        assert 'data-api-base="/fleet/ui"' in html          # console_api_base 没配 → 回落 public_prefix
        assert not _BROKEN_WRITE.search(html), _BROKEN_WRITE.search(html)
        assert "/fleet/ui/fleet/ui" not in html
        assert "etag" not in {k.lower() for k in r.headers}
        rw = PrefixRewriter(_pp(public_prefix="/fleet/ui"), route_roots(app.routes))
        assert rw.rewrite_text(html, "text/html") == html   # 幂等
        r = c.get("/fleet/ui/workspace", follow_redirects=False)
        if r.status_code == 200:
            assert "/fleet/ui/fleet/ui" not in r.text
        r = c.get("/fleet/ui/", follow_redirects=False)
        if r.status_code == 200 and "<head>" in r.text:
            assert "__publicPrefixFix" in r.text            # base.html 壳：运行时兜底只在前缀模式渲染
            assert "var nav={d:'/fleet/ui/'," in r.text
            assert "/fleet/ui/fleet/ui" not in r.text


def test_prefix_mode_json_untouched_and_same_as_root(tmp_path):
    app = _make_app(tmp_path, {"public_prefix": "/fleet/ui"})
    h = {"Authorization": f"Bearer {_TOKEN}"}
    with TestClient(app) as c:
        a = c.get("/api/fleet/nodes", headers=h)
        b = c.get("/fleet/ui/api/fleet/nodes", headers=h)
        assert a.status_code == b.status_code == 200
        assert a.headers["content-type"].startswith("application/json")
        assert a.json() == b.json()


def test_explicit_console_api_base_wins(tmp_path):
    app = _make_app(tmp_path, {"public_prefix": "/fleet/ui"}, {"console_api_base": "/fleet/x"})
    with TestClient(app) as c:
        _login(c, "/fleet/login")
        assert 'data-api-base="/fleet/x"' in c.get("/fleet/console").text


# ── 中间件栈（与 Starlette 版本无关：117 是 0.41，VPS 是 1.7） ───────────────

def test_middleware_stack_order_with_gzip_and_http_middleware():
    """剥前缀在最外层（内层中间件看到的是站内路径）；改写在压缩之前；Location / 登录别名生效。"""
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.middleware.gzip import GZipMiddleware
    from src.web.public_prefix import install

    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=10)

    @app.middleware("http")
    async def seen(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["x-seen-path"] = request.url.path
        return resp

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return '<form action="/login"><a href="/workspace">w</a><script>fetch("/api/x/"+i+"/tasks")</script></form>' * 20

    @app.post("/login")
    async def login_submit(auth_token: str = Form("")):
        return RedirectResponse("/workspace" if auth_token else "/login?err=1", status_code=303)

    @app.get("/workspace")
    async def ws():
        return RedirectResponse("/login?next=%2Fworkspace", status_code=303)

    @app.get("/api/x")
    async def api():
        return JSONResponse({"path": "/workspace"})

    @app.get("/fleet/console", response_class=HTMLResponse)
    async def console():
        return "<a href='/api/x'>x</a><a href='/fleet/console'>c</a>"

    install(app, _pp(public_prefix="/fleet/ui"))
    c = TestClient(app)
    r = c.get("/fleet/ui/login", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers["x-seen-path"] == "/login"
    assert r.headers.get("content-encoding") == "gzip"
    assert 'action="/fleet/login"' in r.text and 'href="/fleet/ui/workspace"' in r.text
    assert 'fetch("/fleet/ui/api/x/"+i+"/tasks")' in r.text
    r = c.post("/fleet/login", data={"auth_token": "t"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fleet/ui/workspace"
    assert c.get("/fleet/ui/workspace", follow_redirects=False).headers["location"] == "/fleet/login?next=%2Fworkspace"
    assert c.get("/fleet/ui/api/x").json() == {"path": "/workspace"}
    r = c.get("/fleet/console", headers={"accept-encoding": "identity"})
    assert "href='/fleet/ui/api/x'" in r.text and "href='/fleet/console'" in r.text
    assert int(r.headers["content-length"]) == len(r.content)
