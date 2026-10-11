# -*- coding: utf-8 -*-
"""Telegram 官方 bot 模式入口与接入向导（2026-10-08）。

覆盖：平台注册（modes / registry / readiness / 官方平台清单）· 向导卡（字段 ↔ 运行时 config 键
对齐、account_id 由 token 推导）· setWebhook 只手动触发 · 媒体 multipart 直传 · i18n 词条。
无真实 bot：回环假 Bot API（127.0.0.1），不发任何真实消息。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.integrations import telegram_bot_official as tgb
from src.integrations.shared import official_stop_gate

_ROOT = Path(__file__).resolve().parents[1]
BOT_ID = "7012345678"
TOKEN = BOT_ID + ":" + "unit" + "-fake-" + "bot-token"
SECRET = "unit_" + "webhook_" + "secret"
CHAT = "5550001111"


class _FakeApi:
    def __init__(self):
        self.requests = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) or b""
                ctype = self.headers.get("Content-Type") or ""
                body = json.loads(raw or b"{}") if ctype.startswith("application/json") else raw
                api.requests.append((self.path, ctype, body))
                out = json.dumps({"ok": True, "result": {"message_id": len(api.requests)}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from src.inbox import account_blocklist
    from src.inbox.store import InboxStore
    from src.integrations import protocol_bridge
    a = _FakeApi()
    tgb.reset_for_tests()
    official_stop_gate.reset_for_tests()
    account_blocklist.reset_for_tests()
    monkeypatch.delenv("TG_BOT_API_BASE", raising=False)
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    tgb.configure_runtime({"api_base": a.base})
    yield a
    a.close()
    protocol_bridge.register_inbox_store_getter(None)
    account_blocklist.reset_for_tests()
    official_stop_gate.reset_for_tests()
    tgb.reset_for_tests()
    tgb.configure_runtime({})


def _cfg(api_base="", **kw):
    tb = {"enabled": True, "bot_token": TOKEN, "webhook_secret": SECRET,
          "webhook_path": "/tg/bot/webhook", "api_base": api_base}
    tb.update(kw)
    return {"telegram_bot": tb}


# ── 平台注册 ────────────────────────────────────────────────────────────────

def test_telegram_official_mode_registered_without_stealing_default():
    from src.integrations import platform_registry as reg
    from src.integrations.official_api_worker import DEDICATED_WORKER_PLATFORMS, OFFICIAL_PLATFORMS
    from src.integrations.platform_login import DEFAULT_PLATFORM_MODES, login_kind
    from src.integrations.platform_readiness import _IMPLEMENTED_MODES
    spec = DEFAULT_PLATFORM_MODES["telegram"]
    assert "official" in spec["modes"] and spec["default"] == "protocol"
    assert login_kind("telegram", "official") == "credentials"
    assert "official" in reg.get("telegram").modes
    assert ("telegram", "official") in _IMPLEMENTED_MODES
    assert "telegram" in OFFICIAL_PLATFORMS and "telegram" in DEDICATED_WORKER_PLATFORMS


def test_readiness_points_to_bot_card_fields():
    from src.integrations.platform_readiness import _official_blockers
    out = _official_blockers("telegram", {})
    assert out and out[0]["code"] == "official_creds_missing"
    assert "Bot Token" in out[0]["params"]["field"]
    assert "API ID" not in out[0]["params"]["field"]   # 不是个人号 API 凭证
    assert _official_blockers("telegram", _cfg()) == []


def test_official_enabled_and_caps():
    from src.integrations.official_api_worker import official_enabled, official_send_caps
    assert official_enabled(_cfg(), "telegram") is True
    assert official_enabled({}, "telegram") is False
    caps = official_send_caps("telegram", {})
    assert caps["can_media"] is True and caps["can_voice"] is True


# ── 接入向导卡 ──────────────────────────────────────────────────────────────

def test_bot_card_fields_match_runtime_config_keys():
    from src.utils.channel_setup import get_channel
    ch = get_channel("telegram_bot")
    assert ch is not None and ch.enable_key == "telegram_bot.enabled"
    assert ch.official_platform == "telegram"
    assert ch.official_account_id_from_token == "telegram_bot.bot_token"
    keys = {f.key for f in ch.fields}
    assert {"telegram_bot.bot_token", "telegram_bot.webhook_secret"} <= keys
    src = (_ROOT / "src" / "integrations" / "telegram_bot_official.py").read_text(encoding="utf-8")
    for f in ch.fields:
        block, _, key = f.key.partition(".")
        assert block == "telegram_bot", f.key
        assert (f'cfg.get("{key}")' in src) or (f'_rt("{key}"' in src), f.key
    secrets = {f.key for f in ch.fields if f.secret}
    assert secrets == {"telegram_bot.bot_token", "telegram_bot.webhook_secret"}


def test_bot_card_status_masks_and_ready():
    from src.utils.channel_setup import channel_status
    ch = next(c for c in channel_status(_cfg()) if c["id"] == "telegram_bot")
    assert ch["configured"] is True and ch["ready"] is True
    dumped = json.dumps(ch, ensure_ascii=False)
    assert TOKEN not in dumped and SECRET not in dumped


def test_provision_uses_bot_id_from_token(monkeypatch):
    from src.web.routes import unified_inbox_setup_routes as r
    calls = []

    class _Reg:
        def upsert(self, platform, account_id, **kw):
            calls.append((platform, account_id, kw.get("mode")))

    monkeypatch.setattr("src.integrations.account_registry.get_account_registry", lambda: _Reg())
    assert r._provision_official_account("telegram_bot", _cfg()) == BOT_ID
    assert calls == [("telegram", BOT_ID, "official")]
    calls.clear()
    assert r._provision_official_account("telegram_bot", _cfg(bot_token="not-a-token")) == ""
    assert calls == []


# ── setWebhook 只手动触发 ──────────────────────────────────────────────────

def test_set_webhook_never_called_automatically():
    src = (_ROOT / "src" / "integrations" / "telegram_bot_official.py").read_text(encoding="utf-8")
    assert src.count('"setWebhook"') == 1, "setWebhook 只能出现在手动工具函数里"
    i = src.index('"setWebhook"')
    assert src.rfind("async def tg_bot_set_webhook", 0, i) > src.rfind("\ndef register_telegram_bot_routes", 0, i)
    routes = (_ROOT / "src" / "web" / "routes" / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    seg = routes[routes.index("async def api_telegram_bot_set_webhook"):][:1500]
    assert "_require_account_manager(request)" in seg


def test_set_webhook_posts_url_secret_and_allowed_updates(api):
    out = asyncio.run(tgb.tg_bot_set_webhook(_cfg(api.base), "https://bot.example.com/"))
    assert out["ok"] is True and out["host"] == "bot.example.com" and out["path"] == "/tg/bot/webhook"
    path, _, body = api.requests[-1]
    assert path.endswith("/setWebhook")
    assert body["url"] == "https://bot.example.com/tg/bot/webhook"
    assert body["secret_token"] == SECRET
    assert set(body["allowed_updates"]) == set(tgb.TG_ALLOWED_UPDATES)
    assert TOKEN not in json.dumps(out)


@pytest.mark.parametrize("base,kind", [
    ("http://bot.example.com", "bad_url"), ("https://bot.example.com/?x=1", "bad_url"), ("", "bad_url"),
])
def test_set_webhook_rejects_bad_url_without_calling(api, base, kind):
    out = asyncio.run(tgb.tg_bot_set_webhook(_cfg(api.base), base))
    assert out["ok"] is False and out["error_kind"] == kind
    assert api.requests == []


def test_set_webhook_rejects_bad_secret_and_missing_token(api):
    assert asyncio.run(tgb.tg_bot_set_webhook(_cfg(api.base, webhook_secret="bad secret!"),
                                              "https://bot.example.com"))["error_kind"] == "bad_secret"
    assert asyncio.run(tgb.tg_bot_set_webhook(_cfg(api.base, bot_token=""),
                                              "https://bot.example.com"))["error_kind"] == "missing_credentials"
    assert api.requests == []


# ── 媒体 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,mt,method", [
    ("a.jpg", "image", "sendPhoto"), ("a.png", "", "sendPhoto"), ("v.ogg", "voice", "sendVoice"),
    ("v.mp3", "voice", "sendAudio"), ("m.mp4", "video", "sendVideo"), ("f.pdf", "file", "sendDocument"),
    ("f.zip", "", "sendDocument"),
])
def test_media_method_selection(name, mt, method):
    assert tgb.tg_media_method(name, mt, 1000)[0] == method


def test_big_photo_falls_back_to_document():
    assert tgb.tg_media_method("a.jpg", "image", tgb.TG_PHOTO_MAX + 1)[0] == "sendDocument"


def test_send_media_multipart_and_worker(api, tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    out = asyncio.run(tgb.tg_bot_send_media(CHAT, str(f), TOKEN, media_type="image", caption="hi"))
    assert out["ok"] is True and out["method"] == "sendPhoto"
    path, ctype, body = api.requests[-1]
    assert path.endswith("/sendPhoto") and ctype.startswith("multipart/form-data")
    assert b'name="photo"' in body and b'name="chat_id"' in body and CHAT.encode() in body
    w = tgb.TelegramBotWorker({"account_id": BOT_ID, "meta": {}}, _cfg(api.base))
    res = asyncio.run(w.send_media(f"telegram:user:{CHAT}", media_path=str(f), media_type="image"))
    assert res["delivered"] is True


def test_send_media_blocked_after_stop_and_missing_file(api, tmp_path):
    f = tmp_path / "v.ogg"
    f.write_bytes(b"OggS" + b"0" * 32)
    official_stop_gate.apply_stop("telegram", BOT_ID, CHAT, hits=["stop"])
    out = asyncio.run(tgb.tg_bot_send_media(CHAT, str(f), TOKEN, media_type="voice"))
    assert out["ok"] is False and out.get("blocked") == "stop_contact"
    out2 = asyncio.run(tgb.tg_bot_send_media("999", str(tmp_path / "nope.ogg"), TOKEN))
    assert out2["error_kind"] == "bad_media"
    assert api.requests == []


# ── i18n ────────────────────────────────────────────────────────────────────

def test_wizard_hook_strings_are_i18n_keys():
    from src.web.i18n_packs import setup_channels as pack
    tpl = (_ROOT / "src" / "web" / "templates" / "setup_wizard.html").read_text(encoding="utf-8")
    used = {k for k in ("sw_tg_hook_title", "sw_tg_hook_ph", "sw_tg_hook_hint", "sw_tg_hook_btn",
                        "sw_tg_hook_running", "sw_tg_hook_ok", "sw_tg_hook_fail", "sw_tg_hook_bad_url")
            if f"'{k}'" in tpl}
    assert len(used) == 8
    for k in used:
        assert k in pack.ZH and k in pack.EN, k
    assert pack.ZH["sw_tg_hook_ok"].count("{host}") == pack.EN["sw_tg_hook_ok"].count("{host}") == 1
