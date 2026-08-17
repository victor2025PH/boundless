"""群成员提取管理台整页：渲染冒烟 + 自包含不变量。

不启浏览器：只验「路由能把模板渲染出来（200 + HTML + 关键 DOM 骨架）」——这是整页最大的
落地风险（模板/上下文错就整页 500）；JS 交互留部署窗口真机验。另钉「自包含」不变量
（无 Jinja 变量 / 无 window.T 依赖 / 只用 addEventListener）——独立页故意不接 base.html 基建。
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.group_members_routes import register_group_members_routes

_TPL = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
        / "tg_members.html")


def _client(enabled=True):
    class _CM:
        config = {"companion": {"group_members": {"enabled": enabled}}}

    app = FastAPI()

    async def _auth():
        return True

    register_group_members_routes(app, auth_dep=_auth, audit_store=None,
                                  config_manager=_CM())
    return TestClient(app)


def test_page_renders_html_shell():
    c = _client()
    r = c.get("/tools/tg-members")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    for marker in ('id="gm-start"', 'id="gm-accts"', 'id="gm-jobs"', 'id="gm-mbody"',
                   "/api/tg-members/jobs", "/api/accounts"):
        assert marker in body, f"管理台缺骨架 {marker}"


def test_page_renders_even_when_feature_disabled():
    # 页面不 gate enabled（管理员总能打开；页内探测 quota 得 403 后显示提示横幅）
    c = _client(enabled=False)
    assert c.get("/tools/tg-members").status_code == 200


def test_page_is_self_contained():
    src = _TPL.read_text(encoding="utf-8")
    assert "{{" not in src and "{%" not in src   # 无 Jinja（HTMLResponse 直出，braces 安全）
    assert "window.T(" not in src                 # 内联 t() i18n，不依赖 web_i18n
    assert "addEventListener" in src              # 事件绑定走 addEventListener
    assert 'onclick="' not in src and "onclick='" not in src  # 无内联 on* handler


def test_html_page_uses_page_auth_not_api_json():
    """管理台是整页导航：未登录必须 303 去登录页，不能走 API 的 401 JSON。

    桌面壳 target=_blank 弹窗会把 {"detail":"Unauthorized"} 渲染成 Chromium JSON
    预览（「美观输出」），坐席以为管理台坏了。page_auth 与 api_auth 必须分家。
    """
    from fastapi import HTTPException

    class _CM:
        config = {"companion": {"group_members": {"enabled": True}}}

    async def _api_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _page_ok():
        return True

    app = FastAPI()
    register_group_members_routes(
        app, auth_dep=_api_auth, page_auth=_page_ok, audit_store=None,
        config_manager=_CM())
    r = TestClient(app).get("/tools/tg-members")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "Unauthorized" not in r.text


def test_html_page_unauthenticated_redirects_not_401_json():
    from fastapi import HTTPException

    class _CM:
        config = {"companion": {"group_members": {"enabled": True}}}

    async def _api_auth():
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _page_auth():
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    app = FastAPI()
    register_group_members_routes(
        app, auth_dep=_api_auth, page_auth=_page_auth, audit_store=None,
        config_manager=_CM())
    r = TestClient(app, follow_redirects=False).get("/tools/tg-members")
    assert r.status_code == 303
    assert r.headers.get("location") == "/login"
    assert r.status_code != 401
