"""端到端：`GET /api/platforms/{p}/modes` 必须把真实卡点带给前端。

诊断做得再准，没接到弹窗消费的那个接口上也等于没做。这里从 HTTP 层验收：

- LINE 开了开关但缺 okline → `reason_code=dep_missing`（不是笼统的 not_enabled）；
- WhatsApp 全开、provider 已注册，但 sidecar 不可达 → `ready=false` 且**摘掉推荐角标**
  （旧行为挂着「推荐」骗点击，用户点进去干等一个不会出现的二维码）；
- `?recheck=1` 必须真的绕过探测缓存（运维刚起完服务，用户点「重新检测」要立刻看到变化）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.integrations import line_protocol_login as lpl
from src.integrations import messenger_web_login as mwl
from src.integrations import platform_login as pl
from src.integrations import protocol_diagnostics as pd
from src.integrations import telegram_protocol_login as tpl
from src.integrations import whatsapp_baileys_login as wbl
from src.web.routes.unified_inbox_login_routes import register_platform_login_routes

# 每个 provider 模块各有一个 `_registered` 幂等标志：只清 _PROVIDERS 而不清它，
# 下一个用例的 _ensure_login_providers 会以为「已经注册过」而跳过，于是本该可用的
# 方式变成不可用——症状是用例互相污染、单跑绿、连跑红。
_PROVIDER_MODULES = (tpl, lpl, wbl, mwl)


class _CM:
    def __init__(self, cfg):
        self.config = cfg


@pytest.fixture(autouse=True)
def _clean_registry():
    """provider 注册表 + 各模块注册标志 + 探测缓存，都是进程级全局，用例间必须清。"""
    def _reset():
        pl._PROVIDERS.clear()
        for mod in _PROVIDER_MODULES:
            mod._registered = False
        pd._SERVICE_PROBE_CACHE.clear()
    _reset()
    yield
    _reset()


def _client(cfg):
    app = FastAPI()
    register_platform_login_routes(
        app, api_auth=lambda request: None, config_manager=_CM(cfg))
    return TestClient(app)


def _mode(resp_json, name):
    for m in resp_json["modes"]:
        if m["mode"] == name:
            return m
    raise AssertionError(f"响应里没有 mode={name}: {resp_json}")


def test_line_missing_dep_reports_dep_missing(monkeypatch):
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)

    async def _no_probe(_cfg, **_kw):
        return {}
    monkeypatch.setattr(pd, "probe_services", _no_probe)

    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True,
                              "line": {"protocol_enabled": True}}}
    d = _client(cfg).get("/api/platforms/line/modes").json()
    proto = _mode(d, "protocol")
    assert proto["available"] is False
    assert proto["ready"] is False
    assert proto["reason_code"] == "dep_missing"
    assert any(b["code"] == "dep_missing" for b in proto["blockers"])


def test_device_mode_stays_ready(monkeypatch):
    async def _no_probe(_cfg, **_kw):
        return {}
    monkeypatch.setattr(pd, "probe_services", _no_probe)
    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True}}
    d = _client(cfg).get("/api/platforms/line/modes").json()
    assert _mode(d, "device")["ready"] is True


def test_whatsapp_sidecar_down_drops_the_recommended_badge(monkeypatch):
    async def _down(_cfg, **_kw):
        return {"whatsapp": False}
    monkeypatch.setattr(pd, "probe_services", _down)

    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True,
                              "whatsapp": {"protocol_enabled": True}}}
    proto = _mode(_client(cfg).get("/api/platforms/whatsapp/modes").json(), "protocol")
    # provider 只看开关就注册了 → 仍然 available，但预检已知它一定失败
    assert proto["available"] is True
    assert proto["ready"] is False
    assert proto["reason_code"] == "service_down"
    assert proto["preferred"] is False, "不可用还挂推荐角标 = 骗点击"


def test_whatsapp_sidecar_up_is_preferred(monkeypatch):
    async def _up(_cfg, **_kw):
        return {"whatsapp": True}
    monkeypatch.setattr(pd, "probe_services", _up)

    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True,
                              "whatsapp": {"protocol_enabled": True}}}
    proto = _mode(_client(cfg).get("/api/platforms/whatsapp/modes").json(), "protocol")
    assert proto["ready"] is True and proto["preferred"] is True
    assert proto["blockers"] == []


def test_recheck_flag_bypasses_probe_cache(monkeypatch):
    seen = []

    async def _probe(_cfg, **kw):
        seen.append(bool(kw.get("force")))
        return {"whatsapp": True}
    monkeypatch.setattr(pd, "probe_services", _probe)

    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True,
                              "whatsapp": {"protocol_enabled": True}}}
    c = _client(cfg)
    c.get("/api/platforms/whatsapp/modes")
    c.get("/api/platforms/whatsapp/modes?recheck=1")
    assert seen == [False, True]


def test_probe_failure_degrades_to_unknown_not_to_error(monkeypatch):
    """探测本身炸了也只能当「未知」，绝不能把弹窗打不开。"""
    async def _boom(_cfg, **_kw):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(pd, "probe_services", _boom)

    cfg = {"platform_login": {"enabled": True, "orchestrator_enabled": True,
                              "whatsapp": {"protocol_enabled": True}}}
    r = _client(cfg).get("/api/platforms/whatsapp/modes")
    assert r.status_code == 200
    proto = _mode(r.json(), "protocol")
    assert proto["ready"] is True          # 未知不算故障
    assert proto["available"] is True


def test_every_mode_carries_blockers_field(monkeypatch):
    """契约：前端无条件读 m.blockers，缺字段会让渲染分支静默走空。"""
    async def _no_probe(_cfg, **_kw):
        return {}
    monkeypatch.setattr(pd, "probe_services", _no_probe)
    cfg = {"platform_login": {"enabled": True}}
    for plat in ("telegram", "line", "whatsapp", "messenger"):
        d = _client(cfg).get(f"/api/platforms/{plat}/modes").json()
        for m in d["modes"]:
            assert "blockers" in m and isinstance(m["blockers"], list), (plat, m)
            assert "ready" in m, (plat, m)
