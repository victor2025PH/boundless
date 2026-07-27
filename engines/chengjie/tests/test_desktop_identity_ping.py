"""后端身份探针门禁（桌面壳「连错后端」防线的服务端半边）。

桌面壳原先只判「端口上有没有 HTTP 响应」就复用外部后端。同机同时跑主后端(18799)
+ Baileys(8790) + messenger-web(8791)，三平台全开后更多——端口被别的程序或上一版本
残留后端占用时，壳会连上去当自己的用：工作台打得开、部分页面 404/500，极难排查。

`GET /api/desktop/ping` 是壳复用前的核对依据，因此有三条硬不变量：
1. **免鉴权**——探针要在登录之前可用，加了鉴权整条防线就废了；
2. **app 恒为 chengjie**——壳按它判定身份，改了就等于把所有装机版的判定弄错；
3. **不泄露多余信息**——它无鉴权，只能给 app 与 version。
"""
from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.utils import app_identity
from src.web.routes.unified_inbox_desktop_routes import register_desktop_routes


def _client():
    app = FastAPI()

    def _deny(_request=None):
        raise AssertionError("ping 端点不该走鉴权")

    register_desktop_routes(app, api_auth=_deny)
    return TestClient(app)


def test_ping_is_unauthenticated_and_identifies_the_app():
    r = _client().get("/api/desktop/ping")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["app"] == "chengjie"
    assert isinstance(d["version"], str) and d["version"]


def test_ping_exposes_nothing_beyond_identity():
    """无鉴权端点，字段只能是身份三件套——别顺手加主机名/路径/pid。"""
    assert set(_client().get("/api/desktop/ping").json()) == {"ok", "app", "version"}


def test_app_id_is_frozen():
    """改这个常量＝让所有已装机的桌面壳判定失败（它们硬编码期望 chengjie）。"""
    assert app_identity.APP_ID == "chengjie"


def test_version_comes_from_launcher_env(monkeypatch):
    """发布态由桌面 launcher 注入 AITR_APP_VERSION，故能发现「新壳连旧后端」。"""
    monkeypatch.setenv("AITR_APP_VERSION", "9.9.9")
    assert app_identity.app_version() == "9.9.9"
    assert _client().get("/api/desktop/ping").json()["version"] == "9.9.9"


def test_version_falls_back_to_dev(monkeypatch):
    monkeypatch.delenv("AITR_APP_VERSION", raising=False)
    assert app_identity.app_version() == "dev"


def test_blank_env_does_not_produce_empty_version(monkeypatch):
    """空串版本会让壳的比对逻辑拿到假值，必须回落 dev。"""
    monkeypatch.setenv("AITR_APP_VERSION", "   ")
    assert app_identity.app_version() == "dev"
