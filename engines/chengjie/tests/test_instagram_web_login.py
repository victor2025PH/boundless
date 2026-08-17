"""P2：Instagram 个人号（Playwright 网页托管）登录 Python 桥接 + 契约层单测（不联网）。

覆盖：桥接 provider 流程（hosted）/ 开关门控 / 状态归一 / channel_setup 门控（inert-by-default）/
platform_login.list_modes 运行时注入（hosted + notice）/ platform_readiness (instagram,web) 诊断。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import instagram_web_login as iwl
from src.integrations import platform_login as pl
from src.integrations.account_registry import AccountRegistry


# ── 桥接基础 ────────────────────────────────────────────────────────────────

def test_service_base_url_default_and_override():
    assert iwl.service_base_url({}) == "http://127.0.0.1:8793"
    cfg = {"platform_login": {"instagram": {"web_url": "http://h:9/"}}}
    assert iwl.service_base_url(cfg) == "http://h:9"


def test_web_enabled_flag_explicit_only():
    assert iwl.web_enabled({}) is False
    assert iwl.web_enabled(
        {"platform_login": {"instagram": {"web_enabled": True}}}) is True
    assert iwl.web_enabled(
        {"platform_login": {"instagram": {"web_enabled": False}}}) is False


def test_normalize_status():
    assert iwl._normalize_status("open") == "authorized"
    assert iwl._normalize_status("logged_in") == "authorized"
    assert iwl._normalize_status("timeout") == "expired"
    assert iwl._normalize_status("logged_out") == "failed"
    assert iwl._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    iwl._registered = False
    pl._PROVIDERS.pop(pl._pkey("instagram", "web"), None)
    assert iwl.maybe_register({}) is False
    assert pl.mode_available("instagram", "web") is False
    try:
        assert iwl.maybe_register(
            {"platform_login": {"instagram": {"web_enabled": True}}}) is True
        assert pl.mode_available("instagram", "web") is True
    finally:
        iwl._registered = False
        pl._PROVIDERS.pop(pl._pkey("instagram", "web"), None)


def test_provider_flow_authorized(monkeypatch):
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "ig_abc", "qr_image": "data:image/png;base64,xxx"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "logged_in", "account_id": "17841400000000000",
                "name": "shop_vn"}

    monkeypatch.setattr(iwl, "_post_json", fake_post)
    monkeypatch.setattr(iwl, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "ig.db"))
    monkeypatch.setattr(iwl, "get_account_registry", lambda: reg)

    async def run():
        provider = iwl.make_provider(
            {"platform_login": {"instagram": {"web_url": "http://x"}}})
        info = await provider(None, "instagram", "web", "")
        assert info["qr_image"].startswith("data:image/png")
        assert info["instruction_key"] == "inbox.connect.instr_ig_web"
        # hosted：明确「不使用二维码」，不给正向扫码指引
        assert "不使用二维码" in info["instruction"]
        poll = info["poll"]
        assert (await poll(None))["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "17841400000000000"

    asyncio.run(run())
    g = reg.get("instagram", "17841400000000000")
    assert g and g["mode"] == "web" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(iwl, "_post_json", boom)

    async def run():
        provider = iwl.make_provider({})
        info = await provider(None, "instagram", "web", "")
        assert "instruction" in info and "poll" not in info
        assert info.get("reason_code") == "service_down"

    asyncio.run(run())


# ── 契约层：inert-by-default + hosted + notice ─────────────────────────────

def test_list_modes_web_injection_gated_and_hosted():
    modes_off = pl.list_modes("instagram", {})
    assert [m["mode"] for m in modes_off] == ["official"]
    modes_on = pl.list_modes("instagram", {"web_enabled": True})
    names = [m["mode"] for m in modes_on]
    assert names[0] == "web" and "official" in names
    web = [m for m in modes_on if m["mode"] == "web"][0]
    assert web["recommended"] is True
    assert web["login_kind"] == "hosted"   # IG 网页个人登录＝托管，不是扫码
    assert web["label_key"] == "inbox.connect.mode_l_ig_web"
    assert web.get("notice") and web["notice"]["key"] == "inbox.connect.notice_unofficial"


def test_default_platform_modes_static_unchanged():
    # 静态声明保持官方独存（test_official_channel_onboarding 同款断言，防回归）
    assert pl.DEFAULT_PLATFORM_MODES["instagram"] == {
        "modes": ["official"], "default": "official"}


def test_channel_status_login_path_gated():
    from src.utils.channel_setup import channel_status

    def _ig(cfg):
        return [c for c in channel_status(cfg) if c["id"] == "instagram"][0]

    ig_off = _ig({})
    assert ig_off["paths"]["login"] is None
    assert ig_off["paths"]["api"] is not None
    ig_on = _ig({"platform_login": {"instagram": {"web_enabled": True}}})
    assert ig_on["paths"]["login"] is not None
    assert ig_on["paths"]["login"]["platform"] == "instagram"
    assert ig_on["paths"]["login"]["notice_key"] == "inbox.connect.notice_unofficial"


def test_readiness_ig_web_implemented_and_diagnosed():
    from src.integrations import platform_readiness as PR

    assert ("instagram", "web") in PR._IMPLEMENTED_MODES
    assert ("instagram", "web") in PR._SIDECAR_MODES
    cfg = {"platform_login": {"instagram": {"web_enabled": True}},
           "orchestrator_enabled": True}
    d_down = PR.diagnose_mode("instagram", "web", cfg, service_ok=False)
    assert d_down["reason_code"] == PR.BLOCK_SERVICE_DOWN
    d_off = PR.diagnose_mode("instagram", "web", {}, service_ok=None)
    assert d_off["reason_code"] == PR.BLOCK_NEEDS_SERVER_SETUP
