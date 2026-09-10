"""TikTok × huoke 桥（TikTok 线续做 E，2026-09-10）——假 huoke 4 例，零网络：
线索进线（意向闸 / comment_id 幂等 / personal_rpa 登记 / 评论原文起草）；回传队列闸门（同一入站只回一条 / 150 字 /
日上限 / 认领 TTL / 回执 → out 回显 / 失败留痕）；路由默认关 + 挂载契约（含旧窗口字段名归一）；e2e：conftest 完整 app
人工发送回落到桥适配器进队列 → huoke 回执 → 线程出现 out 回显。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.integrations import protocol_bridge as pb
from src.integrations import tiktok_huoke_bridge as hb
from src.inbox.store import InboxStore

ACC = "dev-acc-1"
DEV = "device-A"
CFG = {"tiktok": {"huoke_bridge": {"enabled": True, "min_intent": 0.6, "daily_cap": 2}}}
T0 = 1_800_000_000.0


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def upsert(self, platform, account_id, **kw):
        self.rows[f"{platform}:{account_id}"] = dict(kw)


def _lead(cid: str, uid: str = "u1", text: str = "how much is this? ship to Manila?", intent: float = 0.9, **kw) -> Dict[str, Any]:
    d = {"comment_id": cid, "video_id": "v-100", "user_id": uid, "username": f"user_{uid}", "name": f"User {uid}",
         "text": text, "intent_score": intent, "ts": T0, "lang": "en"}
    d.update(kw)
    return d


@pytest.fixture()
def env(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = hb.TikTokHuokeStateStore(":memory:")
    yield store, st
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


# ── 1 线索进线 ────────────────────────────────────────────────────────────────────────────

async def test_leads_ingest_intent_gate_dedupe_and_draft(env):
    store, st = env
    reg = FakeRegistry()
    emitted: List[Dict[str, Any]] = []
    drafted: List[Dict[str, Any]] = []

    def emit(m):
        emitted.append(m)
        pb.ingest_incoming(store, **m)

    async def draft(m):
        drafted.append(m)

    payload = {"device_id": DEV, "account_id": ACC, "leads": [
        _lead("c1"), _lead("c2", uid="u2", intent=0.3), _lead("c1"), {"comment_id": "c3", "user_id": "u3", "text": ""}]}
    status, res = await hb.ingest_leads(payload, config=CFG, state=st, now=T0, emit=emit, auto_reply=draft, registry=reg)
    assert status == 200 and res == {"ok": True, "accepted": 1, "dup": 1, "low_intent": 1, "invalid": 1, "drafted": 1}
    # 账号：personal_rpa 登记（本机编排器不接管），桥状态库记设备
    from src.integrations.leadbus_account import PERSONAL_RPA_MODE
    from src.integrations.account_orchestrator import ORCHESTRATED_MODES
    assert reg.rows[f"tiktok:{ACC}"]["mode"] == PERSONAL_RPA_MODE and PERSONAL_RPA_MODE not in ORCHESTRATED_MODES
    assert st.account(ACC)["device_id"] == DEV and st.account(ACC)["leads_total"] == 1
    # 消息形状：评论原文（非「[线索捕获]」占位符）→ 允许起草；source 带 huoke/comment/personal_rpa
    m = emitted[0]
    from src.integrations.leadbus_account import is_lead_capture_text
    assert m["platform"] == "tiktok" and m["account_id"] == ACC and m["chat_key"] == "tiktok:comment:u1"
    assert m["direction"] == "in" and m["msg_id"] == "cmt:c1" and not is_lead_capture_text(m["text"])
    assert m["source"]["source"] == "huoke" and m["source"]["kind"] == "comment" and m["source"]["mode"] == "personal_rpa"
    assert m["source"]["comment_id"] == "c1" and m["source"]["video_id"] == "v-100" and m["source"]["intent_score"] == 0.9
    assert drafted and drafted[0] is m
    # 再送同一条 → 全 dup，不再起草
    status, res = await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c1")]},
                                        config=CFG, state=st, now=T0 + 1, emit=emit, auto_reply=draft, registry=reg)
    assert res["accepted"] == 0 and res["dup"] == 1 and len(drafted) == 1
    # 坏载荷
    assert (await hb.ingest_leads({"leads": []}, config=CFG, state=st, registry=reg))[0] == 400
    assert (await hb.ingest_leads("x", config=CFG, state=st, registry=reg))[0] == 400
    # 会话上下文：入站后 out_since_inbound 归零
    ctx = st.ctx(ACC, "tiktok:comment:u1")
    assert ctx["last_comment_id"] == "c1" and ctx["out_since_inbound"] == 0 and ctx["last_inbound_ts"] == T0


# ── 2 回传队列闸门 ────────────────────────────────────────────────────────────────────────

async def test_handback_queue_gates_claim_ack_and_echo(env):
    store, st = env
    reg = FakeRegistry()
    chat = "tiktok:comment:u1"
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c1")]}, config=CFG, state=st, now=T0,
                          emit=lambda m: None, auto_reply=_noop, registry=reg)
    # 没进过线的账号 / 非评论会话 → not_bridged
    assert hb.enqueue_reply("other", chat, "hi", config=CFG, state=st)["reason"] == hb.REASON_NOT_BRIDGED
    assert hb.enqueue_reply(ACC, "tiktok:user:u1", "hi", config=CFG, state=st)["reason"] == hb.REASON_NOT_BRIDGED
    # 评论 150 字上限
    r = hb.enqueue_reply(ACC, chat, "x" * 151, config=CFG, state=st, now=T0)
    assert r["ok"] is False and r["reason"].startswith(hb.REASON_TOO_LONG) and r["status"] == 409
    # 正常入队；同一入站第二条 → 只准回一条
    r1 = hb.enqueue_reply(ACC, chat, "Hi! Yes we ship to Manila, DM us for a quote.", config=CFG, state=st, now=T0 + 10)
    assert r1["ok"] is True and r1["handback"] is True
    r2 = hb.enqueue_reply(ACC, chat, "second", config=CFG, state=st, now=T0 + 11)
    assert r2["ok"] is False and r2["reason"] == hb.REASON_ONE_REPLY and r2["status"] == 409
    # 对方再评论 → 计数归零 → 可再回一条；日上限 2 → 第三条 429
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c2", text="ok how to order")]},
                          config=CFG, state=st, now=T0 + 20, emit=lambda m: None, auto_reply=_noop, registry=reg)
    r3 = hb.enqueue_reply(ACC, chat, "Link in bio", config=CFG, state=st, now=T0 + 30)
    assert r3["ok"] is True
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c3", text="thanks")]},
                          config=CFG, state=st, now=T0 + 40, emit=lambda m: None, auto_reply=_noop, registry=reg)
    r4 = hb.enqueue_reply(ACC, chat, "np", config=CFG, state=st, now=T0 + 50)
    assert r4["ok"] is False and r4["reason"].startswith(hb.REASON_DAILY_CAP) and r4["status"] == 429
    # 认领：按设备取到 2 条（带评论上下文），再取为空；TTL 过期回收后可再认领
    items = st.claim(DEV, limit=10, now=T0 + 60)
    assert [i["id"] for i in items] == [r1["item_id"], r3["item_id"]] and items[0]["comment_id"] == "c1" and items[1]["comment_id"] == "c2"
    assert items[0]["status"] == "claimed" and items[0]["claimed_by"] == DEV and items[0]["user_id"] == "u1"
    assert st.claim(DEV, limit=10, now=T0 + 61) == []
    assert st.claim("device-B", limit=10, now=T0 + 61) == []                        # 别的设备拿不到
    assert [i["id"] for i in st.claim(DEV, limit=10, now=T0 + 60 + 601)] == [r1["item_id"], r3["item_id"]]  # 10 分钟没回执 → 回收
    # 回执成功 → sent + out 回显（msg_id = 平台回评 id）；重复回执幂等不再回显
    echoed: List[Dict[str, Any]] = []
    status, res = hb.ack_handback({"item_id": r1["item_id"], "ok": True, "external_id": "reply-777"}, config=CFG, state=st,
                                  now=T0 + 100, emit=echoed.append)
    assert status == 200 and res["status"] == "sent" and res["dup"] is False
    assert len(echoed) == 1 and echoed[0]["direction"] == "out" and echoed[0]["msg_id"] == "reply-777"
    assert echoed[0]["chat_key"] == chat and echoed[0]["source"]["handback_item"] == r1["item_id"] and echoed[0]["source"]["echo"] is True
    status, res = hb.ack_handback({"item_id": r1["item_id"], "ok": True, "external_id": "reply-777"}, config=CFG, state=st, emit=echoed.append)
    assert res["dup"] is True and len(echoed) == 1
    # 回执失败 → failed 留痕、不回显、该入站的「一条」额度退回（可重试）
    status, res = hb.ack_handback({"item_id": r3["item_id"], "ok": False, "error": "comment_deleted"}, config=CFG, state=st, emit=echoed.append)
    assert res["status"] == "failed" and len(echoed) == 1
    assert st.ctx(ACC, chat)["out_since_inbound"] == 0
    assert hb.ack_handback({"item_id": 9999, "ok": True}, config=CFG, state=st)[0] == 404
    assert hb.ack_handback({"ok": True}, config=CFG, state=st)[0] == 400
    s = st.summary()
    assert (s["sent"], s["failed"], s["queued"], s["accounts"]) == (1, 1, 0, 1)


async def _noop(_m):
    return None


# ── 3 路由：默认关零痕迹；开了经 tiktok_official 唯一挂载口挂 4 条（幂等）+ 旧窗口字段名归一 ─────

def test_routes_default_off_then_mounted_with_window_field_normalization(env, monkeypatch):
    store, st = env
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.integrations import tiktok_official as tk
    routes = (hb.LEADS_ROUTE, hb.HANDBACK_ROUTE, hb.HANDBACK_ACK_ROUTE, hb.STATUS_ROUTE)
    app0 = FastAPI()
    tk.register_tiktok_routes(app0, SimpleNamespace(config={}))
    assert not any(getattr(r_, "path", "") in routes for r_ in app0.routes)
    assert hb.bridge_cfg({})["enabled"] is False and hb.bridge_cfg({})["daily_cap"] == hb.DEFAULT_DAILY_CAP
    assert hb.bridge_cfg({"tiktok": {"huoke_bridge": {"max_reply_len": 999}}})["max_reply_len"] == hb.COMMENT_MAX_LEN
    app = FastAPI()
    adapters: List[Any] = []
    monkeypatch.setattr("src.web.routes.unified_inbox_aggregate._INBOX_ADAPTERS", adapters)
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    paths = [getattr(r_, "path", "") for r_ in app.routes]
    assert all(paths.count(p) == 1 for p in routes) and tk.DEFAULT_WEBHOOK_PATH not in paths
    assert len(adapters) == 1 and adapters[0].platform == "tiktok"   # 适配器幂等追加
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    monkeypatch.setattr(pb, "maybe_auto_reply", _noop)
    reg = FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry", lambda: reg)
    c = TestClient(app)
    r = c.post(hb.LEADS_ROUTE, json={"device_id": DEV, "account_id": ACC, "leads": [_lead("c1"), _lead("c9", intent=0.1)]})
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["low_intent"] == 1
    assert c.post(hb.LEADS_ROUTE, content=b"{bad").status_code == 400
    ok = hb.enqueue_reply(ACC, "tiktok:comment:u1", "Yes! DM us", config=CFG, state=st)
    assert ok["ok"] is True
    r = c.get(hb.HANDBACK_ROUTE, params={"device_id": DEV})
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [ok["item_id"]]
    assert r.json()["policy"] == {"max_reply_len": 150, "daily_cap": 2}
    assert c.get(hb.HANDBACK_ROUTE).status_code == 400
    # 回执带 TK-1 规划旧名 → 归一到 DY 口径（D 段契约）
    r = c.post(hb.HANDBACK_ACK_ROUTE, json={"item_id": ok["item_id"], "ok": True, "external_id": "rp-1",
                                            "window": {"reply_window_deadline": T0 + 100, "window_sent_count": 1, "cap": 20}})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert r.json()["window"] == {"deadline_ts": T0 + 100, "sent": 1, "cap": 20}
    assert st.account(ACC)["last_window"] == {"deadline_ts": T0 + 100, "sent": 1, "cap": 20}
    r = c.get(hb.STATUS_ROUTE)
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["sent"] == 1 and r.json()["policy"]["min_intent"] == 0.6


# ── 4 e2e：conftest 完整 app——人工发送（编排器不拥有 personal_rpa 账号）回落桥适配器进队列 → 回执 → 线程 out 回显 ─

async def test_e2e_manual_send_falls_back_to_handback_adapter(auth_client, app, tmp_path, monkeypatch):
    from src.web.routes import unified_inbox_aggregate as agg
    store = InboxStore(tmp_path / "inbox_huoke_e2e.db")
    app.state.inbox_store = store
    st = hb.TikTokHuokeStateStore(":memory:")
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    reg = FakeRegistry()
    chat = "tiktok:comment:u1"
    drafts: List[Dict[str, Any]] = []

    async def draft_hook(m):
        drafts.append(m)

    status, res = await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c1")]}, config=CFG, state=st, now=T0,
                                        emit=lambda m: pb.ingest_incoming(store, **m), auto_reply=draft_hook, registry=reg)
    assert status == 200 and res["accepted"] == 1 and drafts and drafts[0]["chat_key"] == chat
    r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
    assert r.status_code == 200 and any("Manila" in str(m.get("text")) for m in r.json().get("messages") or [])
    # 桥未装适配器 → 旧行为：不支持的平台
    r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                        "text": "Yes we ship to Manila", "skip_translate": True})
    assert r.status_code == 400, r.text
    # 装上桥适配器（开关开）→ 人工发送进回传队列，不报错、不伪造送达
    added = hb.install_adapter(agg._INBOX_ADAPTERS, lambda: CFG)
    try:
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "Yes we ship to Manila", "skip_translate": True})
        assert r.status_code == 200, r.text
        items = st.claim(DEV, limit=10, now=T0 + 5)
        assert len(items) == 1 and items[0]["text"] == "Yes we ship to Manila" and items[0]["comment_id"] == "c1"
        # 同一入站第二条 → 409 且带 reason_code
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "and Cebu", "skip_translate": True})
        assert r.status_code == 409 and hb.REASON_ONE_REPLY in r.text, r.text
        # huoke 真机回评论成功 → 回执 → 线程出现 out 回显
        status, res = hb.ack_handback({"item_id": items[0]["id"], "ok": True, "external_id": "tt-reply-1"}, config=CFG, state=st,
                                      emit=lambda m: pb.ingest_incoming(store, **m))
        assert status == 200 and res["status"] == "sent"
        r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
        outs = [m for m in r.json().get("messages") or [] if m.get("direction") == "out"]
        assert outs and "Manila" in str(outs[-1].get("text"))
    finally:
        if added:
            agg._INBOX_ADAPTERS[:] = [a for a in agg._INBOX_ADAPTERS if not getattr(a, "_tiktok_huoke_bridge", False)]
