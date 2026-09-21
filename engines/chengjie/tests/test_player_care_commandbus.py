"""player_care B4：commandbus 发送端（出箱 + 契约端点 pull/ack + STOP 先发 stop）。

    $env:PYTHONPATH=""; .\\.venv\\Scripts\\python.exe -m pytest tests\\test_player_care_commandbus.py -q -p no:cacheprovider
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from domains.player_care import goal_templates as gt
from domains.player_care.commandbus import (
    KIND_NOTE, KIND_REENGAGE, KIND_STOP, CommandOutbox, is_stop_message, reengage_pool,
    resolve_commandbus_cfg, send_note, send_reengage, send_stop, set_outbox,
)
from domains.player_care.gateway import LookupResult
from domains.player_care.hooks import PlayerCareDomainHook
from domains.player_care.profile import STAGE_DORMANT, PlayerProfileService, set_profile_service
from domains.player_care.sync import run_player_sync
from domains.player_care.web.routes import register_routes
from src.contacts.store import ContactStore
from src.hooks.base import HookContext

DAY = 86400.0
T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    gt.unregister_goal_templates()
    set_outbox(None)
    yield
    gt.unregister_goal_templates()
    set_outbox(None)


@pytest.fixture
def ob(tmp_path):
    o = CommandOutbox(tmp_path / "player_commandbus.db")
    yield o
    o.close()


# ── 配置 / STOP 识别 ────────────────────────────────────────────────────────

def test_cfg_defaults_off_and_overrides():
    c = resolve_commandbus_cfg({})
    assert c["enabled"] is False and c["reengage_on_dormant"] and c["stop_on_keyword"] and c["pull_limit"] == 20
    c2 = resolve_commandbus_cfg(SimpleNamespace(config={"player_care": {"commandbus": {"enabled": True, "pull_limit": 999}}}))
    assert c2["enabled"] is True and c2["pull_limit"] == 100


@pytest.mark.parametrize("text", ["STOP", "stop.", "Stop po", "please stop", "unsubscribe", "TANGGALIN",
                                  "wag mo na ako i-message", "别再发了", "不要再发消息了", "退订", "别烦我"])
def test_stop_message_positive(text):
    assert is_stop_message(text)


@pytest.mark.parametrize("text", ["stop na yung laro ko", "I can't stop laughing", "stop by later?",
                                  "别发了那个游戏了, 换一个", "hello", "", "unsub me from the group chat pls"])
def test_stop_message_negative(text):
    assert not is_stop_message(text)


def test_reengage_pool_has_no_digits_and_falls_back_mixed():
    for lang in ("tl", "en", "zh", "", "xx"):
        pool = reengage_pool(lang)
        assert pool and all(not any(ch.isdigit() for ch in m) for m in pool)
    assert len(reengage_pool("")) == len(reengage_pool("tl")) + len(reengage_pool("en"))


# ── 出箱语义 ────────────────────────────────────────────────────────────────

def test_enqueue_pull_ack_roundtrip_and_idempotent_ack(ob):
    env = ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001",
                     payload={"messages": ["Hi po"]}, created_by="t", now=T0)
    assert env["kind"] == KIND_REENGAGE and env["phone"] == "639170000001" and env["messages"] == ["Hi po"]
    assert env["dry_run"] is False and env["command_id"].startswith("c_")
    # 另一台设备看不到
    assert ob.pull("dev-B") == []
    got = ob.pull("dev-A", now=T0 + 1)
    assert [g["command_id"] for g in got] == [env["command_id"]]
    assert ob.pull("dev-A") == []                       # 已 pulled 不再给
    rec = ob.ack(env["command_id"], "done", "sent", now=T0 + 2)
    assert rec["status"] == "done" and rec["detail"] == "sent"
    rec2 = ob.ack(env["command_id"], "failed", "later", now=T0 + 3)   # 幂等：终态不再改
    assert rec2["status"] == "done" and rec2["detail"] == "sent"
    assert ob.ack("c_unknown", "done") is None
    assert ob.ack(env["command_id"], "weird") is None
    assert ob.stats()["by_kind"][KIND_REENGAGE]["done"] == 1


def test_enqueue_validation_and_dedupe(ob):
    assert ob.enqueue("greet", account="dev-A", phone="639170000001") is None       # 非本域 kind
    assert ob.enqueue(KIND_REENGAGE, account="", phone="639170000001") is None       # 没 account
    assert ob.enqueue(KIND_REENGAGE, account="dev-A", phone="") is None              # reengage 必须有号
    a = ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0)
    b = ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0 + 1)
    assert a["command_id"] == b["command_id"]                                        # 同号 queued 不重复
    n = ob.enqueue(KIND_NOTE, account="dev-A", payload={"text": "busy this week"}, now=T0)
    assert n["kind"] == KIND_NOTE and n["phone"] == "" and n["text"] == "busy this week"


def test_stop_first_cancels_pending_and_blocks_future_reengage(ob):
    r = ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0)
    n = ob.enqueue(KIND_NOTE, account="dev-A", phone="639170000001", payload={"text": "x"}, now=T0 + 1)
    other = ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000002", now=T0 + 2)
    s = ob.enqueue(KIND_STOP, account="dev-A", phone="639170000001", payload={"reason": "user_stop"}, now=T0 + 3)
    assert s["kind"] == KIND_STOP and ob.is_stopped("639170000001")
    assert ob.get(r["command_id"])["status"] == "cancelled"
    assert ob.get(n["command_id"])["status"] == "cancelled"
    assert ob.get(other["command_id"])["status"] == "queued"                          # 别人的不受影响
    # stop 排最前
    got = ob.pull("dev-A", now=T0 + 4)
    assert [g["kind"] for g in got] == [KIND_STOP, KIND_REENGAGE]
    assert got[0]["phone"] == "639170000001" and got[1]["phone"] == "639170000002"
    # 之后再对该号 reengage → 拒
    assert ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0 + 5) is None
    assert send_reengage(ob, account="dev-A", phone="639170000001", messages=["hi"]) is None
    # 重复 stop 不重复入箱
    s2 = ob.enqueue(KIND_STOP, account="dev-A", phone="639170000001", now=T0 + 6)
    assert s2["command_id"] != s["command_id"]        # 第一条已 pulled，才允许再入一条
    s3 = ob.enqueue(KIND_STOP, account="dev-A", phone="639170000001", now=T0 + 7)
    assert s3["command_id"] == s2["command_id"]       # queued 的 stop 只保留一条
    # 显式解除
    assert ob.clear_stop("639170000001") and not ob.is_stopped("639170000001")
    assert ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0 + 8) is not None


def test_send_helpers_fail_soft():
    assert send_reengage(None, account="a", phone="p", messages=["x"]) is None
    assert send_stop(None, account="a", phone="p") is None
    assert send_note(None, account="a", text="x") is None


# ── sync：dormant → reengage 指令 ───────────────────────────────────────────

def test_sync_dormant_enqueues_reengage_only_when_enabled_and_has_phone(tmp_path, ob):
    store = ContactStore(tmp_path / "contacts.db")
    svc = PlayerProfileService(store, dormant_after_days=7)
    set_profile_service(svc)
    try:
        svc.record_inbound(key="639170000001", text="hi", platform="whatsapp", account_id="wa-01",
                           external_id="639170000001", phone="639170000001", now=T0)
        svc.record_inbound(key="telegram:777", text="hi", platform="telegram", account_id="tg-01",
                           external_id="777", now=T0)
        cfg_on = {"player_care": {"commandbus": {"enabled": True}, "sync": {"interval_min": 30}}}
        s = run_player_sync(cfg_on, profile=svc, goal_store=None, outbox=ob, now=T0 + 8 * DAY, sleep=lambda _s: None)
        assert s["dormant"] == 2 and s["commands"] == 1                     # 没手机号的 TG 不发
        cmds = ob.pull("wa-01")
        assert len(cmds) == 1 and cmds[0]["kind"] == KIND_REENGAGE and cmds[0]["phone"] == "639170000001"
        assert cmds[0]["reason"] == "dormant" and cmds[0]["messages"] == reengage_pool("")
        assert svc.store.get_player_profile("639170000001")["stage"] == STAGE_DORMANT
        assert ob.pull("tg-01") == []
    finally:
        set_profile_service(None)
        store.close()


def test_sync_dormant_no_command_when_commandbus_disabled(tmp_path, ob):
    store = ContactStore(tmp_path / "contacts.db")
    svc = PlayerProfileService(store, dormant_after_days=7)
    try:
        svc.record_inbound(key="639170000001", text="hi", platform="whatsapp", account_id="wa-01",
                           external_id="639170000001", phone="639170000001", now=T0)
        set_outbox(ob)                                                      # 即使有出箱单例
        s = run_player_sync({"player_care": {"commandbus": {"enabled": False}}}, profile=svc, goal_store=None,
                            now=T0 + 8 * DAY, sleep=lambda _s: None)
        assert s["dormant"] == 1 and s["commands"] == 0 and ob.pull("wa-01") == []
    finally:
        store.close()


# ── hooks：对方说 STOP → 先发 stop ──────────────────────────────────────────

class _GW:
    configured = True

    def lookup(self, q, *, phone="", uid=""):
        return LookupResult(ok=True, found=False)


def _ctx(text, chat_id, account="wa-01"):
    return HookContext(text=text, user_id=chat_id, chat_id=chat_id, reply_lang="tl",
                       user_context={}, extra={"platform": "whatsapp", "account_id": account})


@pytest.mark.asyncio
async def test_hook_stop_message_enqueues_stop_and_cancels_reengage(ob):
    ob.enqueue(KIND_REENGAGE, account="wa-01", phone="639171234567", now=T0)
    hook = PlayerCareDomainHook(config={"player_care": {"commandbus": {"enabled": True}}}, gateway=_GW(), outbox=ob)
    ctx = _ctx("STOP", "639171234567@s.whatsapp.net")
    await hook.on_message_pre_process(ctx)
    cmds = ob.pull("wa-01")
    assert [c["kind"] for c in cmds] == [KIND_STOP]
    assert cmds[0]["phone"] == "639171234567" and cmds[0]["reason"] == "user_stop"
    assert ctx.user_context["_player_stop_sent"] == cmds[0]["command_id"]
    assert ob.stats()["by_kind"][KIND_REENGAGE]["cancelled"] == 1


@pytest.mark.asyncio
async def test_hook_normal_text_or_no_phone_sends_nothing(ob):
    hook = PlayerCareDomainHook(config={"player_care": {"commandbus": {"enabled": True}}}, gateway=_GW(), outbox=ob)
    await hook.on_message_pre_process(_ctx("kumusta!", "639171234567@s.whatsapp.net"))
    await hook.on_message_pre_process(_ctx("STOP", "123456789"))           # TG 数字 id，没手机号
    assert ob.pull("wa-01") == [] and ob.stats()["by_status"] == {}


@pytest.mark.asyncio
async def test_hook_stop_respects_keyword_switch(ob):
    hook = PlayerCareDomainHook(config={"player_care": {"commandbus": {"enabled": True, "stop_on_keyword": False}}},
                                gateway=_GW(), outbox=ob)
    await hook.on_message_pre_process(_ctx("STOP", "639171234567@s.whatsapp.net"))
    assert ob.pull("wa-01") == []


# ── 契约端点 ────────────────────────────────────────────────────────────────

def _client(cfg, ob):
    app = FastAPI()
    set_outbox(ob)

    def _auth(request: Request):
        return None

    def _write(perm):
        return _auth

    ctx = SimpleNamespace(config_manager=SimpleNamespace(config=cfg), api_auth=_auth, api_write_factory=_write)
    register_routes(app, ctx)
    return TestClient(app)


def test_routes_pull_ack_contract_shape(ob):
    c = _client({"player_care": {"commandbus": {"enabled": True}}}, ob)
    assert c.get("/api/commandbus/pull?account=dev-A").json() == {"available": True, "commands": []}
    r = c.post("/api/player-care/commands", json={"kind": "reengage", "account": "dev-A", "phone": "639170000001"})
    assert r.status_code == 200 and r.json()["command"]["kind"] == KIND_REENGAGE
    r = c.post("/api/player-care/commands", json={"kind": "note", "account": "dev-A", "text": "busy"})
    assert r.status_code == 200
    body = c.get("/api/commandbus/pull?account=dev-A&limit=10").json()
    assert body["available"] is True and [x["kind"] for x in body["commands"]] == [KIND_NOTE, KIND_REENGAGE]
    cid = body["commands"][1]["command_id"]
    assert c.post("/api/commandbus/ack", json={"command_id": cid, "status": "done", "detail": "ok"}).json() == {"available": True}
    assert c.post("/api/commandbus/ack", json={"command_id": cid, "status": "done"}).json() == {"available": True}
    assert c.post("/api/commandbus/ack", json={"command_id": "c_nope", "status": "rejected"}).json() == {"available": True}
    assert c.post("/api/commandbus/ack", json={"command_id": cid, "status": "sent"}).status_code == 400
    assert c.post("/api/commandbus/ack", json={"status": "done"}).status_code == 400
    listing = c.get("/api/player-care/commands?account=dev-A").json()
    assert listing["enabled"] is True and listing["stats"]["by_status"]["done"] == 1
    assert {x["status"] for x in listing["commands"]} == {"done", "pulled"}


def test_routes_operator_stop_then_reengage_rejected(ob):
    c = _client({"player_care": {"commandbus": {"enabled": True}}}, ob)
    assert c.post("/api/player-care/commands", json={"kind": "stop", "account": "dev-A", "phone": "639170000001"}).status_code == 200
    r = c.post("/api/player-care/commands", json={"kind": "reengage", "account": "dev-A", "phone": "639170000001"})
    assert r.status_code == 409 and "STOP" in r.json()["detail"]
    assert c.post("/api/player-care/commands", json={"kind": "greet", "account": "dev-A"}).status_code == 400
    assert c.post("/api/player-care/commands", json={"kind": "note", "phone": "1"}).status_code == 400
    assert c.post("/api/player-care/commands", json={"kind": "note", "account": "dev-A", "text": ""}).status_code == 400


def test_routes_disabled_are_fail_soft(ob):
    ob.enqueue(KIND_REENGAGE, account="dev-A", phone="639170000001", now=T0)
    c = _client({"player_care": {"commandbus": {"enabled": False}}}, ob)
    assert c.get("/api/commandbus/pull?account=dev-A").json() == {"available": True, "commands": []}
    assert c.post("/api/commandbus/ack", json={"command_id": "c_x", "status": "done"}).json() == {"available": True}
    assert c.post("/api/player-care/commands", json={"kind": "note", "account": "dev-A", "text": "x"}).status_code == 409
    assert c.get("/api/player-care/commands").json()["enabled"] is False
