"""TK-3 ②-A：TikTok 个人号网页边车骨架（assistOnly）——Python provider 桥接 + Node 边车契约 + 纯解析器。

不联网；node 不在 PATH 时纯解析器用例 skip（不假绿）。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

from src.integrations import platform_login as pl
from src.integrations import tiktok_web_login as twl
from src.integrations.account_registry import AccountRegistry

ROOT = pathlib.Path(__file__).resolve().parents[1]
SVC = ROOT / "services" / "tiktok-web"


def test_default_off_and_base_url():
    assert twl.web_enabled({}) is False
    assert twl.web_enabled({"platform_login": {"tiktok": {"web_enabled": True}}}) is True
    assert twl.service_base_url({}) == "http://127.0.0.1:8794"
    assert twl.service_base_url({"platform_login": {"tiktok": {"web_url": "http://h:9/"}}}) == "http://h:9"
    assert twl.ASSIST_ONLY is True
    # 阶段 1 刻意不进登录弹窗静态表（②-B 连同 worker / 矩阵一起）
    assert "tiktok" not in pl.DEFAULT_PLATFORM_MODES and "tiktok" not in pl._PERSONAL_WEB_LOGIN


def test_maybe_register_gated_and_idempotent():
    twl._registered = False
    pl._PROVIDERS.pop(pl._pkey("tiktok", "web"), None)
    try:
        assert twl.maybe_register({}) is False and pl.mode_available("tiktok", "web") is False
        assert twl.maybe_register({"platform_login": {"tiktok": {"web_enabled": True}}}) is True
        assert pl.mode_available("tiktok", "web") is True
        assert twl.maybe_register({}) is True  # 已注册 → 幂等 True
    finally:
        twl._registered = False
        pl._PROVIDERS.pop(pl._pkey("tiktok", "web"), None)


def test_provider_flow_registers_mode_web_assist_only(monkeypatch):
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "tt_abc", "qr_image": "data:image/png;base64,xxx"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if url.endswith("/health"):
            return {"ok": True, "svc": "tiktok-web", "assist_only": True}
        if url.endswith("/accounts"):
            return {"accounts": [{"account_id": "7123", "logged_in": True}]}
        if calls["n"] == 1:
            return {"status": "pending", "hint_code": "captcha"}
        return {"status": "logged_in", "account_id": "7123", "username": "shop_ph", "name": "Shop PH"}

    monkeypatch.setattr(twl, "_post_json", fake_post)
    monkeypatch.setattr(twl, "_get_json", fake_get)
    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "tt.db"))
    monkeypatch.setattr(twl, "get_account_registry", lambda: reg)

    async def run():
        provider = twl.make_provider({"platform_login": {"tiktok": {"web_url": "http://x"}}})
        info = await provider(None, "tiktok", "web", "")
        assert info["qr_image"].startswith("data:image/png") and info["instruction_key"] == "inbox.connect.instr_tt_web"
        assert "不使用二维码" in info["instruction"] and "不代发" in info["instruction"]
        assert info["state"]["assist_only"] is True
        r1 = await info["poll"](None)
        assert r1["status"] == "pending" and r1["hint_code"] == "captcha"
        r2 = await info["poll"](None)
        assert r2["status"] == "authorized" and r2["account_id"] == "7123"
        h = await twl.service_health({"platform_login": {"tiktok": {"web_url": "http://x"}}})
        assert h == {"reachable": True, "assist_only": True, "accounts": 1, "base": "http://x"}

    asyncio.run(run())
    g = reg.get("tiktok", "7123")
    assert g and g["mode"] == "web" and g["status"] == "online"
    assert g["meta"]["assist_only"] is True and g["meta"]["username"] == "shop_ph"


def test_provider_service_down_and_health_unreachable(monkeypatch):
    async def boom(url, *a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(twl, "_post_json", boom)
    monkeypatch.setattr(twl, "_get_json", boom)

    async def run():
        info = await twl.make_provider({})(None, "tiktok", "web", "")
        assert info.get("reason_code") == "service_down" and "poll" not in info
        h = await twl.service_health({})
        assert h["reachable"] is False and h["assist_only"] is True and "refused" in h["error"]

    asyncio.run(run())


def test_onboarding_web_tab_reflects_switch():
    from src.web.routes import onboarding_guide_routes as og
    off = og._tiktok_web_panel({})
    assert off["enabled"] is False and off["ready"] is False and off["phase"] == "assistOnly" and off["service_url"].endswith(":8794")
    on = og._tiktok_web_panel({"platform_login": {"tiktok": {"web_enabled": True, "web_url": "http://h:1"}}})
    assert on["enabled"] is True and on["ready"] is False and on["service_url"] == "http://h:1"


def test_sidecar_contract_is_assist_only_and_loopback():
    """server.js 文本契约：发送端点恒 501 assist_only；入站 payload platform=tiktok + direction in + source mode web；只绑回环。"""
    src = (SVC / "server.js").read_text(encoding="utf-8")
    assert 'app.post("/accounts/:id/send", assistOnlyReject)' in src
    assert 'app.post("/accounts/:id/send-media", assistOnlyReject)' in src
    assert "res.status(501)" in src and '"assist_only"' in src
    assert "const ASSIST_ONLY = true;" in src
    # 不得出现任何真正把文字打进输入框的发送实现
    assert not re.search(r"keyboard\.press\(\"Enter\"\)|\.fill\(text\)|setInputFiles", src)
    ingest = src[src.index("async function ingestMessage"):src.index("// ── 入站轮询")]
    assert 'platform: "tiktok"' in ingest and 'direction: "in"' in ingest and 'mode: "web"' in ingest
    assert 'process.env.BIND_HOST || "127.0.0.1"' in src and "process.env.PORT || 8794" in src
    assert 'if (row.directionHint === "out") continue;' in src   # 自己发的不当入站
    readme = (SVC / "README.md").read_text(encoding="utf-8")
    assert "501" in readme and "assist" in readme.lower() and "72" in readme and "联调核对清单" in readme
    pkg = (SVC / "package.json").read_text(encoding="utf-8")
    assert '"name": "tiktok-web-login-service"' in pkg and "playwright" in pkg


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH：纯解析器用例跳过（不假绿）")
def test_tt_threads_parser_node_tests_pass():
    r = subprocess.run(["node", "--test", "test/tt_threads.test.js"], cwd=str(SVC), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
