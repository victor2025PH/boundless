# -*- coding: utf-8 -*-
"""WhatsApp 官方 Cloud API 通道（与 Baileys 个人号并列的双轨）——2026-10-08 智聊 DM 接入。

覆盖：webhook 验签 → 入箱 → 自答发送；模板消息与 24h 窗口回退；进待人工（点名真人 /
AI 异常 / 空回复 / 媒体 / 投递失败）；STOP 硬闸（入站冻结 + 出站 text/media/template
全拦、不碰网络）；健康状态（探活只读 GET、判词）；Meta 按条费用台账（单列）。

无真实官方号：用 **回环假 Graph**（127.0.0.1 上的 ThreadingHTTPServer）做端到端——
真 FastAPI 路由 + 真 HMAC 验签 + 真 aiohttp 出站，只是 Graph 基址指向本机。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations import wa_cloud_billing
from src.integrations import whatsapp_cloud as wac
from src.integrations.shared import official_handoff, official_stop_gate

PNID = "1098765432"
SECRET = "unit-" + "wa-app-" + "secret"      # 运行期拼接：测试假值
TOKEN = "unit-" + "wa-access-" + "token"
VERIFY = "unit-" + "verify"
USER = "8613800138000"


# ── 回环假 Graph ─────────────────────────────────────────────────────────────

class _FakeGraph:
    def __init__(self):
        self.requests = []          # (method, path, headers, json)
        self.script = []            # 预置响应 [(status, body_dict)]，空 = 200 成功
        self.lock = threading.Lock()
        graph = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                return

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                with graph.lock:
                    graph.requests.append(("POST", self.path, dict(self.headers), data))
                    nxt = graph.script.pop(0) if graph.script else None
                if nxt:
                    return self._reply(*nxt)
                self._reply(200, {"messaging_product": "whatsapp",
                                  "messages": [{"id": f"wamid.T{len(graph.requests)}"}]})

            def do_GET(self):
                with graph.lock:
                    graph.requests.append(("GET", self.path, dict(self.headers), None))
                    nxt = graph.script.pop(0) if graph.script else None
                if nxt:
                    return self._reply(*nxt)
                self._reply(200, {"display_phone_number": "15550001234", "verified_name": "ZhiLiao",
                                  "quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K",
                                  "id": PNID})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v21.0"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _SM:
    def __init__(self, reply="Hi, how can I help?", exc=None):
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
    from src.integrations import official_webhook_stats as ows
    from src.integrations import protocol_bridge

    graph = _FakeGraph()
    ows.reset_for_tests(tmp_path / "ows.json")
    wa_cloud_billing.set_db_path_for_tests(None)
    official_stop_gate.reset_for_tests()
    official_handoff.reset_for_tests()
    wac.reset_stats_for_tests()
    account_blocklist.reset_for_tests()
    monkeypatch.delenv("WA_CLOUD_GRAPH_BASE", raising=False)
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    cfg = {"whatsapp_cloud": {
        "enabled": True, "phone_number_id": PNID, "access_token": TOKEN,
        "app_secret": SECRET, "verify_token": VERIFY, "webhook_path": "/wa/webhook",
        "graph_base": graph.base,
        "window_fallback_template": {"name": "reengage_v1", "language": "en_US"},
        "pricing": {"currency": "USD", "rates": {"default": {"marketing": 0.05, "utility": 0.01},
                                                 "86": {"marketing": 0.08}}},
    }}
    sm = _SM()
    app = FastAPI()
    wac.register_whatsapp_cloud_routes(app, SimpleNamespace(config=cfg), SimpleNamespace(skill_manager=sm))
    client = TestClient(app)
    yield SimpleNamespace(graph=graph, store=store, cfg=cfg, sm=sm, client=client, app=app)
    client.close()
    graph.close()
    protocol_bridge.register_inbox_store_getter(None)
    account_blocklist.reset_for_tests()
    ows.reset_for_tests(None)
    wac.configure_runtime({})
    wac.reset_stats_for_tests()
    official_stop_gate.reset_for_tests()
    official_handoff.reset_for_tests()
    wa_cloud_billing.set_db_path_for_tests(None)


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _post(client, body, *, sign=True):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(raw)
    return client.post("/wa/webhook", content=raw, headers=headers)


def _msg_body(msg):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": PNID}, "messages": [msg]}}]}]}


def _text(t, mid="wamid.IN1", frm=USER):
    return _msg_body({"from": frm, "id": mid, "timestamp": "1760000000", "type": "text",
                      "text": {"body": t}})


def _cid(user=USER):
    return f"whatsapp:{PNID}:wa:user:{user}"


def _tags(store, user=USER):
    return list(store.get_conv_tags(_cid(user)) or [])


# ── webhook 验签 ─────────────────────────────────────────────────────────────

def test_verify_handshake_and_signature(env):
    c = env.client
    r = c.get("/wa/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                     "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    r = c.get("/wa/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                     "hub.challenge": "42"})
    assert r.status_code == 403
    assert _post(c, _text("hi"), sign=False).status_code == 403
    raw = json.dumps(_text("hi")).encode()
    bad = c.post("/wa/webhook", content=raw, headers={
        "X-Hub-Signature-256": "sha256=" + "0" * 64, "Content-Type": "application/json"})
    assert bad.status_code == 403
    assert env.sm.calls == [] and env.graph.posts() == []


# ── 入箱 + 自答发送（回环端到端）────────────────────────────────────────────

def test_e2e_inbound_reply_goes_to_loopback_graph(env):
    r = _post(env.client, _text("hello"))
    assert r.status_code == 200
    assert env.sm.calls == ["hello"]
    posts = env.graph.posts()
    assert len(posts) == 1
    _, path, headers, payload = posts[0]
    assert path == f"/v21.0/{PNID}/messages"
    assert headers.get("Authorization") == f"Bearer {TOKEN}"
    assert payload["to"] == USER and payload["type"] == "text"
    assert payload["text"]["body"] == "Hi, how can I help?"
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["send"]["ok"] == 1 and h["webhook"]["verdict"] == "live"


def test_graph_base_override_rejects_plain_http_remote(monkeypatch):
    assert wac._safe_graph_base("http://evil.example.com/v21.0") == ""
    assert wac._safe_graph_base("https://graph.example.com/v21.0").startswith("https://")
    assert wac._safe_graph_base("http://127.0.0.1:9/v21.0").startswith("http://127.0.0.1")
    wac.configure_runtime({"graph_base": "http://evil.example.com"})
    try:
        assert wac.graph_base() == wac.GRAPH_BASE
    finally:
        wac.configure_runtime({})


def test_button_reply_text_is_routed(env):
    body = _msg_body({"from": USER, "id": "wamid.B1", "type": "interactive",
                      "interactive": {"type": "button_reply",
                                      "button_reply": {"id": "b1", "title": "Pricing"}}})
    assert _post(env.client, body).status_code == 200
    assert env.sm.calls == ["Pricing"]


# ── 模板消息 + 24h 窗口回退 ─────────────────────────────────────────────────

def test_send_template_payload(env):
    out = asyncio.run(wac.wa_send_template(USER, "order_update", "en_US", PNID, TOKEN,
                                           body_params=["A-1", "tomorrow"]))
    assert out["ok"] is True and out["template"] == "order_update"
    payload = env.graph.posts()[-1][3]
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "order_update"
    assert payload["template"]["language"] == {"code": "en_US"}
    params = payload["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["A-1", "tomorrow"]


def test_window_expired_falls_back_to_template(env):
    env.graph.script.append((400, {"error": {"code": 131047, "message": "Re-engagement message"}}))
    out = asyncio.run(wac.wa_send_text(USER, "your order shipped", PNID, TOKEN))
    assert out["ok"] is True and out.get("window_fallback") is True
    p1, p2 = env.graph.posts()[-2:]
    assert p1[3]["type"] == "text" and p2[3]["type"] == "template"
    assert p2[3]["template"]["name"] == "reengage_v1"
    assert p2[3]["template"]["components"][0]["parameters"][0]["text"] == "your order shipped"
    assert wac.send_stats_snapshot()["send"]["window_fallback"] == 1


def test_window_expired_without_template_hands_off(env):
    wac.configure_runtime(dict(env.cfg["whatsapp_cloud"], window_fallback_template={}))
    env.graph.script.append((400, {"error": {"code": 131047, "message": "Re-engagement message"}}))
    assert _post(env.client, _text("are you there?")).status_code == 200
    assert "需人工" in _tags(env.store)
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("window_expired") == 1


# ── 进待人工 ─────────────────────────────────────────────────────────────────

def test_human_request_goes_to_handoff_without_ai(env):
    assert _post(env.client, _text("I want to talk to a real person")).status_code == 200
    assert env.sm.calls == []
    assert env.graph.posts() == []
    assert "需人工" in _tags(env.store)
    meta = env.store.get_handoff_meta(_cid())
    assert meta.get("reason") == "human_request"


def test_ai_exception_and_empty_reply_hand_off(env):
    env.sm.exc = RuntimeError("boom")
    _post(env.client, _text("q1", mid="m1"))
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("generate_error") == 1
    env.sm.exc, env.sm.reply = None, ""
    _post(env.client, _text("q2", mid="m2", frm="8613900000000"))
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("empty_reply") == 1
    assert "需人工" in _tags(env.store, "8613900000000")
    assert env.graph.posts() == []


def test_media_inbound_hands_off_but_sticker_does_not(env):
    img = _msg_body({"from": USER, "id": "m-img", "type": "image", "image": {"id": ""}})
    _post(env.client, img)
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("media_inbound") == 1
    sticker = _msg_body({"from": USER, "id": "m-st", "type": "sticker", "sticker": {"id": ""}})
    _post(env.client, sticker)
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("media_inbound") == 1


# ── STOP 硬闸 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["STOP", "stop.", "Unsubscribe", "退订", "Stop promotions"])
def test_stop_keywords_detected(word):
    assert official_stop_gate.detect_stop(word, use_phrase_lexicon=False)


@pytest.mark.parametrize("text", ["don't stop", "where is the bus stop?", "stopwatch price",
                                  "I can't stop thinking about it"])
def test_stop_keywords_not_overmatched(text):
    assert official_stop_gate.keyword_hit(text) == ""


def test_stop_freezes_and_blocks_every_outbound(env):
    assert _post(env.client, _text("STOP")).status_code == 200
    assert env.sm.calls == [] and env.graph.posts() == []
    tags = _tags(env.store)
    assert "客户要求停联" in tags and "需人工" in tags
    assert official_stop_gate.is_stopped("whatsapp", PNID, f"wa:user:{USER}")
    # 之后任何出站都不碰网络
    t = asyncio.run(wac.wa_send_text(USER, "one more thing", PNID, TOKEN))
    tp = asyncio.run(wac.wa_send_template(USER, "promo", "en_US", PNID, TOKEN))
    md = asyncio.run(wac.wa_send_media(USER, __file__, PNID, TOKEN, media_type="document"))
    for out in (t, tp, md):
        assert out["ok"] is False and out["blocked"] == "stop_contact"
        assert out["error"].startswith("stop_gate:")
    assert env.graph.posts() == []
    # 客户再来消息：镜像照常，但不自答
    _post(env.client, _text("hello again", mid="m9"))
    assert env.sm.calls == [] and env.graph.posts() == []
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["stop_gate"]["stop_hits"] == 1
    assert h["stop_gate"]["outbound_blocked"] >= 3
    assert h["send"]["blocked_stop"] >= 3


def test_meta_marketing_optout_button_counts_as_stop(env):
    body = _msg_body({"from": USER, "id": "m-btn", "type": "button",
                      "button": {"text": "Stop promotions", "payload": "Stop promotions"}})
    _post(env.client, body)
    assert official_stop_gate.is_stopped("whatsapp", PNID, f"wa:user:{USER}")
    assert env.sm.calls == []


def test_stop_phrase_lexicon_also_freezes(env):
    _post(env.client, _text("please stop messaging me"))
    assert env.sm.calls == []
    assert official_stop_gate.is_stopped("whatsapp", PNID, f"wa:user:{USER}")


def test_official_worker_send_reports_blocked(env):
    from src.integrations.official_api_worker import OfficialApiWorker
    official_stop_gate.apply_stop("whatsapp", PNID, f"wa:user:{USER}", hits=["stop"])
    w = OfficialApiWorker({"platform": "whatsapp", "account_id": PNID}, env.cfg)
    res = asyncio.run(w.send(f"wa:user:{USER}", "hi"))
    assert res["delivered"] is False and res["blocked"] == "stop_contact"
    assert env.graph.posts() == []


def test_farewell_blocked_by_default_and_optional(env):
    from src.inbox.stop_contact import farewell_text
    official_stop_gate.apply_stop("whatsapp", PNID, f"wa:user:{USER}", hits=["stop"])
    bye = farewell_text("en")
    assert asyncio.run(wac.wa_send_text(USER, bye, PNID, TOKEN))["ok"] is False
    wac.configure_runtime(dict(env.cfg["whatsapp_cloud"], stop_gate={"allow_farewell": True}))
    assert asyncio.run(wac.wa_send_text(USER, bye, PNID, TOKEN))["ok"] is True
    assert asyncio.run(wac.wa_send_text(USER, "anything else", PNID, TOKEN))["ok"] is False


# ── Meta 按条费用（status webhook pricing）+ 投递失败 ─────────────────────────

def _status_body(*sts):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": PNID}, "statuses": list(sts)}}]}]}


def test_billing_ledger_from_status_webhook(env):
    mk = {"billable": True, "pricing_model": "PMP", "category": "marketing", "type": "regular"}
    svc = {"billable": False, "pricing_model": "PMP", "category": "service",
           "type": "free_customer_service"}
    _post(env.client, _status_body(
        {"id": "w1", "status": "sent", "recipient_id": USER, "pricing": mk},
        {"id": "w1", "status": "delivered", "recipient_id": USER, "pricing": mk},
        {"id": "w2", "status": "sent", "recipient_id": "14155550100", "pricing": mk},
        {"id": "w3", "status": "delivered", "recipient_id": USER, "pricing": svc},
        {"id": "w4", "status": "sent", "recipient_id": USER,
         "pricing": {"billable": True, "pricing_model": "PMP", "category": "authentication"}},
    ))
    assert env.sm.calls == []          # status 不喂 SkillManager
    s = wa_cloud_billing.summary()
    assert s["messages"] == 4          # w1 只记一次
    assert s["billable"] == 3 and s["free"] == 1
    assert s["unpriced_billable"] == 1  # authentication 未配单价
    assert s["est_cost"] == pytest.approx(0.08 + 0.05)   # 86 前缀 0.08 + default 0.05
    assert s["by_category"]["marketing"]["billable"] == 2
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["meta_billing"]["est_cost"] == pytest.approx(0.13)
    assert any("未配置单价" in a for a in h["alerts"])
    assert USER not in json.dumps(h)   # 健康面板不出收件人号码


def test_failed_status_unbills_and_hands_off(env):
    mk = {"billable": True, "pricing_model": "PMP", "category": "utility", "type": "regular"}
    _post(env.client, _status_body(
        {"id": "w9", "status": "sent", "recipient_id": USER, "pricing": mk},
        {"id": "w9", "status": "failed", "recipient_id": USER, "pricing": mk,
         "errors": [{"code": 131026, "title": "Message undeliverable"}]},
    ))
    s = wa_cloud_billing.summary()
    assert s["billable"] == 0
    assert official_handoff.handoff_snapshot("whatsapp")["by_reason"].get("delivery_failed") == 1
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["delivery"]["failed_codes"].get("131026") == 1
    assert h["verdict"] == "degraded"


# ── 健康状态 ────────────────────────────────────────────────────────────────

def test_health_probe_is_read_only_get(env):
    p = asyncio.run(wac.wa_cloud_probe(env.cfg))
    assert p["ok"] is True and p["quality_rating"] == "GREEN"
    assert p["display_phone_number"] != "15550001234"   # 打码
    assert env.graph.posts() == []                       # 只读：零 POST
    gets = [r for r in env.graph.requests if r[0] == "GET"]
    assert gets and gets[-1][1].startswith(f"/v21.0/{PNID}?fields=")
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["probe"]["ok"] is True
    blob = json.dumps(h)
    assert TOKEN not in blob and SECRET not in blob and VERIFY not in blob


def test_health_verdicts(env):
    assert wac.wa_cloud_health({"whatsapp_cloud": {"enabled": False}})["verdict"] == "disabled"
    miss = wac.wa_cloud_health({"whatsapp_cloud": {"enabled": True, "phone_number_id": PNID}})
    assert miss["verdict"] == "misconfigured" and miss["creds"]["access_token"] is False
    env.graph.script.append((401, {"error": {"code": 190, "message": "Invalid OAuth access token"}}))
    p = asyncio.run(wac.wa_cloud_probe(env.cfg))
    assert p["ok"] is False and p["error_kind"] == "invalid_token"
    h = wac.wa_cloud_health(env.cfg, app_state=env.app.state)
    assert h["verdict"] == "down"


def test_health_route_registered_in_admin(app):
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/admin/whatsapp-cloud/health" in paths


def test_health_route_returns_payload(auth_client):
    r = auth_client.get("/api/admin/whatsapp-cloud/health")
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "whatsapp" and "verdict" in body and "meta_billing" in body


# ── 与智安统一闸 src.compliance.stop_gate 的软委托（模块在途；此处用假模块钉契约）──────

def test_soft_delegation_to_compliance_stop_gate(env, monkeypatch):
    import sys
    import types
    calls = []
    fake = types.ModuleType("src.compliance.stop_gate")
    fake.detect = lambda text: "tigil na" if "tigil na" in str(text).lower() else ""
    fake.contact_stopped = lambda store, plat, acct, peer, phone="", **k: (
        "blocklist_phone" if phone.endswith("0000099") else "")
    def _record(store, **kw):
        calls.append(("record", kw["platform"], kw["peer"]))
        return {"frozen": True, "listed": True, "already": False}
    fake.record_stop = _record
    fake.audit = lambda store=None, **kw: calls.append(("audit", kw["action"], kw["path"])) or True
    monkeypatch.setitem(sys.modules, "src.compliance.stop_gate", fake)
    # 判定：统一闸词表（他加禄语）命中
    assert official_stop_gate.detect_stop("Tigil na") == ["tigil na"]
    # 跨账号 / 同手机号视角：统一闸说停联过 → 本通道也拦，且出站记审计
    out = asyncio.run(wac.wa_send_text("639170000099", "hi", PNID, TOKEN))
    assert out["blocked"] == "stop_contact" and out["error"] == "stop_gate:blocklist_phone"
    assert ("audit", "blocked", "official:whatsapp") in calls
    # 登记走统一闸 record_stop
    _post(env.client, _text("tigil na", frm="639171111111"))
    assert ("record", "whatsapp", "wa:user:639171111111") in calls
    assert env.sm.calls == [] and env.graph.posts() == []
