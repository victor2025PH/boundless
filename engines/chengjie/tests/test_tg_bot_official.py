# -*- coding: utf-8 -*-
"""Telegram Bot API 官方通道（与个人号协议轨并列）——2026-10-08 智聊 DM 接入。

无真实 bot：**回环假 Bot API**（127.0.0.1 ThreadingHTTPServer）+ 真 FastAPI 路由 + 真 secret 校验 +
真 aiohttp 出站。覆盖：webhook 验签 / 入箱 / 自答发送 / update 去重 / 进待人工 / STOP 硬闸
（/stop、STOP、拉黑 bot）/ 坐席接管 worker / 健康（只读探活 + 判词）。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations import telegram_bot_official as tgb
from src.integrations.shared import official_handoff, official_stop_gate

BOT_ID = "7012345678"
TOKEN = BOT_ID + ":" + "unit" + "-fake-" + "bot-token"     # 运行期拼接：测试假值
SECRET = "unit_" + "webhook_" + "secret"
CHAT = "5550001111"


class _FakeBotApi:
    def __init__(self):
        self.requests = []
        self.script = []
        self.webhook_url = "https://bot.example.com/tg/bot/webhook"
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self, method, data):
                api.requests.append((method, self.path, data))
                if api.script:
                    return self._reply(*api.script.pop(0))
                if self.path.endswith("/getMe"):
                    return self._reply(200, {"ok": True, "result": {
                        "id": int(BOT_ID), "is_bot": True, "username": "zhiliao_bot"}})
                if self.path.endswith("/getWebhookInfo"):
                    return self._reply(200, {"ok": True, "result": {
                        "url": api.webhook_url, "pending_update_count": 0}})
                return self._reply(200, {"ok": True, "result": {
                    "message_id": len(api.requests), "chat": {"id": int(CHAT)}}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._route("POST", json.loads(self.rfile.read(n) or b"{}"))

            def do_GET(self):
                self._route("GET", None)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sends(self):
        return [r for r in self.requests if r[0] == "POST" and r[1].endswith("/sendMessage")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _SM:
    def __init__(self, reply="Hello from bot", exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    async def process_message(self, text, user_id, context):
        self.calls.append(text)
        if self.exc:
            raise self.exc
        return self.reply


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from src.inbox import account_blocklist
    from src.inbox.store import InboxStore
    from src.integrations import protocol_bridge

    api = _FakeBotApi()
    tgb.reset_for_tests()
    official_stop_gate.reset_for_tests()
    official_handoff.reset_for_tests()
    account_blocklist.reset_for_tests()
    monkeypatch.delenv("TG_BOT_API_BASE", raising=False)
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    cfg = {"telegram_bot": {"enabled": True, "bot_token": TOKEN, "webhook_secret": SECRET,
                            "webhook_path": "/tg/bot/webhook", "api_base": api.base,
                            "auto_account": False}}
    sm = _SM()
    app = FastAPI()
    tgb.register_telegram_bot_routes(app, SimpleNamespace(config=cfg), SimpleNamespace(skill_manager=sm))
    client = TestClient(app)
    yield SimpleNamespace(api=api, store=store, cfg=cfg, sm=sm, client=client, app=app)
    client.close()
    api.close()
    protocol_bridge.register_inbox_store_getter(None)
    account_blocklist.reset_for_tests()
    tgb.reset_for_tests()
    official_stop_gate.reset_for_tests()
    official_handoff.reset_for_tests()


_UID = [100]


def _upd(text=None, *, chat=CHAT, chat_type="private", uid=None, **extra):
    _UID[0] += 1
    msg = {"message_id": _UID[0], "date": 1760000000,
           "chat": {"id": int(chat), "type": chat_type},
           "from": {"id": int(chat), "is_bot": False, "first_name": "Ana", "language_code": "en"}}
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return {"update_id": uid if uid is not None else _UID[0], "message": msg}


def _post(client, update, *, secret=SECRET):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return client.post("/tg/bot/webhook", content=json.dumps(update).encode(), headers=headers)


def _cid(chat=CHAT):
    return f"telegram:{BOT_ID}:{chat}"


# ── 验签 / 解析 ──────────────────────────────────────────────────────────────

def test_secret_required_and_constant_time():
    assert tgb.verify_tg_secret(SECRET, SECRET) is True
    assert tgb.verify_tg_secret("wrong", SECRET) is False
    assert tgb.verify_tg_secret("", SECRET) is False
    assert tgb.verify_tg_secret(SECRET, "") is False      # 空 secret 硬拒


def test_not_registered_without_secret():
    app = FastAPI()
    cfg = {"telegram_bot": {"enabled": True, "bot_token": TOKEN, "webhook_secret": ""}}
    tgb.register_telegram_bot_routes(app, SimpleNamespace(config=cfg), SimpleNamespace(skill_manager=_SM()))
    assert "/tg/bot/webhook" not in {getattr(r, "path", "") for r in app.routes}


def test_bot_id_never_includes_secret_part():
    assert tgb.bot_id_from_token(TOKEN) == BOT_ID
    assert tgb.bot_id_from_token("abc:def") == ""


def test_webhook_rejects_bad_secret(env):
    assert _post(env.client, _upd("hi"), secret=None).status_code == 403
    assert _post(env.client, _upd("hi"), secret="nope").status_code == 403
    assert env.sm.calls == [] and env.api.sends() == []
    assert tgb.stats_snapshot()["bad_secret"] == 2


# ── 入箱 + 自答（回环端到端）─────────────────────────────────────────────────

def test_e2e_private_text_replied_via_loopback_api(env):
    assert _post(env.client, _upd("how much is it?")).status_code == 200
    assert env.sm.calls == ["how much is it?"]
    (_, path, data), = env.api.sends()
    assert path == f"/bot{TOKEN}/sendMessage"
    assert str(data["chat_id"]) == CHAT and data["text"] == "Hello from bot"
    st = tgb.stats_snapshot()
    assert st["events"] == 1 and st["send_ok"] == 1


def test_group_messages_and_bots_ignored(env):
    _post(env.client, _upd("hello group", chat="-100123", chat_type="supergroup"))
    assert env.sm.calls == [] and env.api.sends() == []


def test_duplicate_update_id_processed_once(env):
    u = _upd("dup?", uid=999001)
    _post(env.client, u)
    _post(env.client, u)
    assert env.sm.calls == ["dup?"]
    assert tgb.stats_snapshot()["duplicates"] == 1


def test_callback_query_routed_as_text(env):
    upd = {"update_id": 777001, "callback_query": {
        "id": "cb1", "from": {"id": int(CHAT), "first_name": "Ana"}, "data": "Pricing",
        "message": {"message_id": 5, "chat": {"id": int(CHAT), "type": "private"}}}}
    _post(env.client, upd)
    assert env.sm.calls == ["Pricing"]


def test_start_command_uses_start_reply(env):
    tgb.configure_runtime(dict(env.cfg["telegram_bot"], start_reply="Welcome!"))
    _post(env.client, _upd("/start"))
    assert env.sm.calls == []
    assert env.api.sends()[-1][2]["text"] == "Welcome!"


# ── 进待人工 ────────────────────────────────────────────────────────────────

def test_human_request_hands_off(env):
    _post(env.client, _upd("转人工"))
    assert env.sm.calls == [] and env.api.sends() == []
    assert "需人工" in list(env.store.get_conv_tags(_cid()) or [])


def test_ai_error_empty_and_send_failure_hand_off(env):
    env.sm.exc = RuntimeError("x")
    _post(env.client, _upd("q1"))
    env.sm.exc, env.sm.reply = None, ""
    _post(env.client, _upd("q2", chat="5550002222"))
    env.sm.reply = "ok"
    env.api.script.append((403, {"ok": False, "error_code": 403,
                                 "description": "Forbidden: bot was blocked by the user"}))
    _post(env.client, _upd("q3", chat="5550003333"))
    by = official_handoff.handoff_snapshot("telegram")["by_reason"]
    assert by.get("generate_error") == 1 and by.get("empty_reply") == 1 and by.get("send_error") == 1
    assert tgb.stats_snapshot()["by_error_kind"].get("recipient_unavailable") == 1


def test_media_hands_off_sticker_does_not(env):
    _post(env.client, _upd(None, photo=[{"file_id": "f1"}]))
    _post(env.client, _upd(None, sticker={"file_id": "s1"}))
    by = official_handoff.handoff_snapshot("telegram")["by_reason"]
    assert by.get("media_inbound") == 1


# ── STOP 硬闸 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["/stop", "/stop@zhiliao_bot", "STOP", "退订", "unsubscribe"])
def test_stop_variants_freeze_and_block(env, word):
    _post(env.client, _upd(word))
    assert env.sm.calls == [] and env.api.sends() == []
    assert official_stop_gate.is_stopped("telegram", BOT_ID, CHAT)
    out = asyncio.run(tgb.tg_bot_send_text(CHAT, "still there?", TOKEN, account_id=BOT_ID))
    assert out["ok"] is False and out["blocked"] == "stop_contact"
    assert env.api.sends() == []


def test_stop_then_later_messages_not_answered(env):
    _post(env.client, _upd("please stop messaging me"))
    _post(env.client, _upd("hello?"))
    assert env.sm.calls == [] and env.api.sends() == []
    tags = list(env.store.get_conv_tags(_cid()) or [])
    assert "客户要求停联" in tags


def test_user_blocking_bot_counts_as_stop(env):
    upd = {"update_id": 888001, "my_chat_member": {
        "chat": {"id": int(CHAT), "type": "private"}, "from": {"id": int(CHAT)},
        "new_chat_member": {"status": "kicked"}}}
    _post(env.client, upd)
    assert official_stop_gate.is_stopped("telegram", BOT_ID, CHAT)


def test_worker_takeover_send_and_stop_block(env):
    w = tgb.TelegramBotWorker({"platform": "telegram", "account_id": BOT_ID}, env.cfg)
    asyncio.run(w.start())
    res = asyncio.run(w.send(CHAT, "agent reply"))
    assert res["delivered"] is True
    official_stop_gate.apply_stop("telegram", BOT_ID, CHAT, hits=["stop"])
    res = asyncio.run(w.send(CHAT, "agent again"))
    assert res["delivered"] is False and res["blocked"] == "stop_contact"
    assert len(env.api.sends()) == 1


def test_worker_refuses_mismatched_bot_token(env):
    w = tgb.TelegramBotWorker({"platform": "telegram", "account_id": "999"}, env.cfg)
    with pytest.raises(RuntimeError):
        asyncio.run(w.start())


def test_worker_factory_registered(env):
    from src.integrations.account_orchestrator import get_worker_factory
    assert get_worker_factory("telegram", "official") is not None


# ── 健康 ────────────────────────────────────────────────────────────────────

def test_probe_read_only_and_health_ok(env):
    p = asyncio.run(tgb.tg_bot_probe(env.cfg))
    assert p["ok"] is True and p["username"] == "zhiliao_bot"
    assert p["webhook"]["set"] is True and p["webhook"]["host"] == "bot.example.com"
    assert env.api.sends() == []
    h = tgb.tg_bot_health(env.cfg, app_state=env.app.state)
    assert h["verdict"] == "ok" and h["bot_id"] == BOT_ID
    blob = json.dumps(h)
    assert TOKEN not in blob and SECRET not in blob


def test_health_verdicts(env):
    assert tgb.tg_bot_health({"telegram_bot": {"enabled": False}})["verdict"] == "disabled"
    assert tgb.tg_bot_health({"telegram_bot": {"enabled": True, "bot_token": TOKEN}}
                             )["verdict"] == "misconfigured"
    env.api.webhook_url = ""
    asyncio.run(tgb.tg_bot_probe(env.cfg))
    assert tgb.tg_bot_health(env.cfg, app_state=env.app.state)["verdict"] == "degraded"
    env.api.script.append((401, {"ok": False, "error_code": 401, "description": "Unauthorized"}))
    p = asyncio.run(tgb.tg_bot_probe(env.cfg))
    assert p["error_kind"] == "invalid_token"
    assert tgb.tg_bot_health(env.cfg, app_state=env.app.state)["verdict"] == "down"


def test_classify_rate_limit():
    info = tgb.classify_tg_error(429, {"ok": False, "error_code": 429,
                                       "description": "Too Many Requests: retry after 7",
                                       "parameters": {"retry_after": 7}})
    assert info == {"kind": "rate_limited", "retriable": True, "retry_after": 7}


def test_health_route_registered(app):
    assert "/api/admin/telegram-bot/health" in {getattr(r, "path", "") for r in app.routes}


def test_health_route_payload(auth_client):
    r = auth_client.get("/api/admin/telegram-bot/health")
    assert r.status_code == 200
    assert r.json()["platform"] == "telegram" and "verdict" in r.json()
