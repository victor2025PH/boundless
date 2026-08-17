"""P1：Zalo 个人号（zca-js）扫码登录 Python 桥接 + 契约层单测（不联网）。

覆盖：桥接 provider 流程 / 开关门控 / 状态归一 / channel_setup 门控（inert-by-default）/
platform_login.list_modes 运行时注入 / platform_readiness (zalo,web) 诊断。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations import zalo_personal_login as zpl
from src.integrations.account_registry import AccountRegistry


# ── 桥接基础 ────────────────────────────────────────────────────────────────

def test_service_base_url_default_and_override():
    assert zpl.service_base_url({}) == "http://127.0.0.1:8792"
    cfg = {"platform_login": {"zalo": {"zca_url": "http://h:9/"}}}
    assert zpl.service_base_url(cfg) == "http://h:9"


def test_web_enabled_flag_explicit_only():
    # 未写过 → False（不随桌面默认开：非官方接入必须显式 opt-in）
    assert zpl.web_enabled({}) is False
    assert zpl.web_enabled(
        {"platform_login": {"zalo": {"web_enabled": True}}}) is True
    assert zpl.web_enabled(
        {"platform_login": {"zalo": {"web_enabled": False}}}) is False


def test_normalize_status():
    assert zpl._normalize_status("open") == "authorized"
    assert zpl._normalize_status("logged_in") == "authorized"
    assert zpl._normalize_status("qr_scanned") == "scanned"
    assert zpl._normalize_status("timeout") == "expired"
    assert zpl._normalize_status("logged_out") == "failed"
    assert zpl._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    zpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("zalo", "web"), None)
    assert zpl.maybe_register({}) is False
    assert pl.mode_available("zalo", "web") is False
    try:
        assert zpl.maybe_register(
            {"platform_login": {"zalo": {"web_enabled": True}}}) is True
        assert pl.mode_available("zalo", "web") is True
    finally:
        zpl._registered = False
        pl._PROVIDERS.pop(pl._pkey("zalo", "web"), None)


def test_provider_flow_authorized(monkeypatch):
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "zl_abc", "qr_image": "data:image/png;base64,xxx"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "open", "account_id": "1234567890"}

    monkeypatch.setattr(zpl, "_post_json", fake_post)
    monkeypatch.setattr(zpl, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "zl.db"))
    monkeypatch.setattr(zpl, "get_account_registry", lambda: reg)

    async def run():
        provider = zpl.make_provider(
            {"platform_login": {"zalo": {"zca_url": "http://x"}}})
        info = await provider(None, "zalo", "web", "")
        assert info["qr_image"].startswith("data:image/png")
        assert info["instruction_key"] == "inbox.connect.instr_zalo_web"
        poll = info["poll"]
        r1 = await poll(None)
        assert r1["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "1234567890"

    asyncio.run(run())
    g = reg.get("zalo", "1234567890")
    # 个人号落库为 mode="web"（编排器 (zalo,web) worker 接管）
    assert g and g["mode"] == "web" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(zpl, "_post_json", boom)

    async def run():
        provider = zpl.make_provider({})
        info = await provider(None, "zalo", "web", "")
        assert "instruction" in info
        assert "poll" not in info
        assert info.get("reason_code") == "service_down"

    asyncio.run(run())


def test_provider_poll_forwards_self_profile(monkeypatch):
    import src.integrations.account_self_profile as sp

    async def fake_post(url, payload, timeout=20.0):
        return {"login_id": "zl_abc", "qr_image": "data:image/png;base64,xxx"}

    async def fake_get(url, timeout=20.0):
        return {"status": "open", "account_id": "1234567890",
                "display_name": "小越", "avatar_url": "https://zalo/p.jpg"}

    enriched = []

    async def fake_enrich(platform, account_id, **kw):
        enriched.append((platform, account_id, kw.get("name"), kw.get("avatar_url")))
        return {"self_name": kw.get("name", "")}

    monkeypatch.setattr(zpl, "_post_json", fake_post)
    monkeypatch.setattr(zpl, "_get_json", fake_get)
    monkeypatch.setattr(sp, "enrich_from_fields", fake_enrich)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "zl.db"))
    monkeypatch.setattr(zpl, "get_account_registry", lambda: reg)

    async def run():
        provider = zpl.make_provider(
            {"platform_login": {"zalo": {"zca_url": "http://x"}}})
        info = await provider(None, "zalo", "web", "")
        r = await info["poll"](None)
        assert r["status"] == "authorized"

    asyncio.run(run())
    assert enriched == [
        ("zalo", "1234567890", "小越", "https://zalo/p.jpg")]


# ── 契约层：inert-by-default（关键——保证不扰动官方漏斗） ────────────────────

def test_list_modes_web_injection_gated_by_flag():
    # 未开 web_enabled → 与今日一致：只有 official
    modes_off = pl.list_modes("zalo", {})
    assert [m["mode"] for m in modes_off] == ["official"]
    # 开了 → web 作为主路径插到最前 + 成为默认方式
    modes_on = pl.list_modes("zalo", {"web_enabled": True})
    names = [m["mode"] for m in modes_on]
    assert names[0] == "web" and "official" in names
    web = [m for m in modes_on if m["mode"] == "web"][0]
    assert web["recommended"] is True
    assert web["login_kind"] == "qr"
    assert web["label_key"] == "inbox.connect.mode_l_zalo_web"
    # 个人号「稳定运行建议」随 /modes 下发（P3；2026-08-10 运营决策改建议口径
    # severity=info，不再作风险恐吓）；官方渠道无 notice
    assert web.get("notice") and web["notice"]["key"] == "inbox.connect.notice_unofficial"
    assert web["notice"]["severity"] == "info"
    off = [m for m in modes_on if m["mode"] == "official"][0]
    assert off.get("notice") is None


def test_default_platform_modes_static_unchanged():
    # 静态声明保持官方独存（test_official_channel_onboarding 同款断言，防回归）
    assert pl.DEFAULT_PLATFORM_MODES["zalo"] == {
        "modes": ["official"], "default": "official"}


def test_channel_status_login_path_gated():
    from src.utils.channel_setup import channel_status

    def _zalo(cfg):
        return [c for c in channel_status(cfg) if c["id"] == "zalo"][0]

    # 关：无个人号登录路径（与官方独存时一致）
    z_off = _zalo({})
    assert z_off["paths"]["login"] is None
    assert z_off["paths"]["api"] is not None
    # 开：呈现个人号登录路径 + 风险告知键（与 /modes notice 同键，两处话术一致）
    z_on = _zalo({"platform_login": {"zalo": {"web_enabled": True}}})
    assert z_on["paths"]["login"] is not None
    assert z_on["paths"]["login"]["platform"] == "zalo"
    assert z_on["paths"]["login"]["notice_key"] == "inbox.connect.notice_unofficial"


def test_readiness_zalo_web_implemented_and_diagnosed():
    from src.integrations import platform_readiness as PR

    assert ("zalo", "web") in PR._IMPLEMENTED_MODES
    assert ("zalo", "web") in PR._SIDECAR_MODES
    cfg = {"platform_login": {"zalo": {"web_enabled": True}},
           "orchestrator_enabled": True}
    # sidecar 挂了 → service_down（block）
    d_down = PR.diagnose_mode("zalo", "web", cfg, service_ok=False)
    assert d_down["reason_code"] == PR.BLOCK_SERVICE_DOWN
    # 开关没开 → 需服务器端配置（不是 not_enabled 的笼统码）
    d_off = PR.diagnose_mode("zalo", "web", {}, service_ok=None)
    assert d_off["reason_code"] == PR.BLOCK_NEEDS_SERVER_SETUP
