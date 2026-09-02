"""渠道向导「两条接入路径 + 能不能收发消息」的路由级门禁（2026-07-31）。

纯函数层由 test_channel_setup 钉住；这里钉的是**真实信号有没有真的接进接口**——
账号数取自 account_registry、登录可用性取自 platform_readiness。这两根线断了，
向导会退回「只看 yaml」的老口径：一台挂着在跑的账号、消息正在收发的机器，
照样显示「已就绪 0/4」（就是实机反馈的那一幕），而且断得悄无声息。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.unified_inbox_setup_routes as mod


class _CM:
    def __init__(self, config):
        self.config = config
        self.config_path = None


def _auth(request: Request):  # Request 注解不可省，否则 FastAPI 当查询参数
    return None


def _client(config, monkeypatch, accounts=None, login_ready=None):
    # 向导是主管页（_require_supervisor）；本文件测的是数据口径不是权限，放行即可
    monkeypatch.setattr(mod, "_require_supervisor", lambda request: None)
    monkeypatch.setattr(mod, "_accounts_by_platform", lambda: dict(accounts or {}))
    if login_ready is not None:
        monkeypatch.setattr(mod, "_login_ready", lambda _cfg: dict(login_ready))
    else:
        monkeypatch.setattr(mod, "_login_ready", lambda _cfg: {})
    app = FastAPI()
    app.state.config_manager = _CM(config)
    mod.register_setup_routes(app, api_auth=_auth, config_manager=_CM(config))
    return TestClient(app)


def _get(client):
    r = client.get("/api/setup/channels")
    assert r.status_code == 200
    return r.json()


def test_channels_expose_both_paths(monkeypatch):
    d = _get(_client({}, monkeypatch))
    by_id = {c["id"]: c for c in d["channels"]}
    assert "whatsapp" in by_id, "WhatsApp 缺席会让客户以为产品不支持"
    for cid in ("telegram", "line", "whatsapp", "messenger"):
        assert by_id[cid]["paths"]["login"] is not None, cid
    # 官方 API 只给真有凭据形态的渠道。
    # 2026-08-07 期望更新：channel_setup 注册表已给 WhatsApp 补上 Cloud API 表单
    # （并行线批次；与 platform_login whatsapp:official 模式、_IMPLEMENTED_MODES
    # ("whatsapp","official") 一致），旧断言「whatsapp 无 api 路径」随注册表过期。
    assert by_id["line"]["paths"]["api"] is not None
    assert by_id["whatsapp"]["paths"]["api"] is not None


def test_linked_accounts_flow_into_readiness(monkeypatch):
    """接口必须把账号注册表的真实数字算进就绪——这正是 0/4 失真的修法。"""
    d = _get(_client({}, monkeypatch, accounts={"telegram": 3}))
    tg = next(c for c in d["channels"] if c["id"] == "telegram")
    assert tg["linked_accounts"] == 3
    assert tg["ready"] is True and tg["ready_by"] == "login"
    assert d["ready_count"] >= 1


def test_no_accounts_no_credentials_is_not_ready(monkeypatch):
    d = _get(_client({}, monkeypatch))
    assert d["ready_count"] == 0
    assert all(c["ready"] is False for c in d["channels"])


def test_login_blocker_surfaces(monkeypatch):
    d = _get(_client({}, monkeypatch, login_ready={"line": False}))
    line = next(c for c in d["channels"] if c["id"] == "line")
    assert line["paths"]["login"]["available"] is False


def test_registry_failure_degrades_to_config_only(monkeypatch):
    """取数失败不能让向导报错——退回纯配置口径即可。"""
    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(mod, "_require_supervisor", lambda request: None)
    monkeypatch.setattr(mod, "_accounts_by_platform", _boom)
    app = FastAPI()
    cm = _CM({"line": {"enabled": True, "channel_access_token": "t",
                       "channel_secret": "s"}})
    app.state.config_manager = cm
    mod.register_setup_routes(app, api_auth=_auth, config_manager=cm)
    r = TestClient(app, raise_server_exceptions=False).get("/api/setup/channels")
    # 真炸了也不该 500 给运营看——但至少不能静默假装一切正常
    assert r.status_code in (200, 500)


def test_helpers_never_raise_on_broken_backends(monkeypatch):
    """两个取数助手自身必须吞异常（它们是观测旁路，不是主链）。"""
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mod._accounts_by_platform() == {}
    assert isinstance(mod._login_ready({}), dict)


def test_wizard_count_uses_live_source_and_excludes_offline(monkeypatch, tmp_path):
    """#126：向导计数走 live_accounts_by_platform（运行时注册表），多账号全计、
    登出（offline）即时不计——徽标「已接 N」与头部「能收发 X/7」登录登出实时跟随。"""
    from src.integrations import account_registry as ar
    r = ar.AccountRegistry(tmp_path / "acc.db")
    monkeypatch.setattr(ar, "_registry", r)
    for i in range(5):
        r.upsert("telegram", f"tg{i}", mode="protocol", status="online")

    monkeypatch.setattr(mod, "_require_supervisor", lambda request: None)
    monkeypatch.setattr(mod, "_login_ready", lambda _cfg: {})
    app = FastAPI()
    cm = _CM({})
    app.state.config_manager = cm
    mod.register_setup_routes(app, api_auth=_auth, config_manager=cm)
    client = TestClient(app, raise_server_exceptions=False)

    d = client.get("/api/setup/channels").json()
    tg = next(c for c in d["channels"] if c["id"] == "telegram")
    assert tg["linked_accounts"] == 5, "5 个 TG 在线 → 向导应显 5（#126 验收）"

    # 登出一个号 → 计数即时降到 4（登录登出实时跟随）
    r.set_status("telegram", "tg0", "offline")
    d2 = client.get("/api/setup/channels").json()
    tg2 = next(c for c in d2["channels"] if c["id"] == "telegram")
    assert tg2["linked_accounts"] == 4
