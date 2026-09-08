# -*- coding: utf-8 -*-
"""TikTok 官方通道骨架（指令 TK-1 A+B，2026-09-08）：地区模型 / 签名 / webhook→收件箱 / 幂等 / 发送形状与
错误映射 / 图片（地区门控 + 3MB）/ 注册门控 / 路由。全部假 transport，零网络。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import pytest

from src.integrations import tiktok_official as tk
from src.integrations import tiktok_regions as tr_
from src.integrations import protocol_bridge as pb
from src.inbox.store import InboxStore

SECRET = "wh-secret"
BIZ = "biz-1"
USER = "user-9"
CFG = {"tiktok": {"enabled": True, "app_id": "app", "secret": "s", "webhook_secret": SECRET}}


class FakeTransport:
    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.responses: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.queue: List[Tuple[int, Dict[str, Any]]] = []

    async def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.queue:
            return self.queue.pop(0)
        return self.responses.get(url, (200, {"code": 0, "message": "OK", "request_id": "r1",
                                              "data": {"message": {"message_id": "m-out-1"}}}))


@pytest.fixture()
def env(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = tk.TikTokStateStore(":memory:")
    tr = FakeTransport()
    api = tk.TikTokApi(transport=tr)
    yield store, st, tr, api
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


def _event(event, msg, **top):
    d = {"event": event, "business_id": BIZ, "message": msg}
    d.update(top)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def _recv(text="hello there", mid="m-in-1", conv="conv-1"):
    return _event(tk.EVENT_RECEIVE, {"sender": USER, "recipient": BIZ, "conversation_id": conv, "message_id": mid,
                                     "timestamp": int(time.time() * 1000), "message_type": "TEXT",
                                     "text": {"body": text}, "from_user": {"role": "USER", "id": USER}})


async def _noop(_p):
    return None


# ── 地区模型 ───────────────────────────────────────────────────────────────────

def test_region_model():
    assert tr_.dm_api_available("DE") is False and tr_.dm_api_available("GB") is False
    assert tr_.dm_api_available("CH") is False and tr_.dm_api_available("US") is False and tr_.dm_api_available("IN") is False
    assert tr_.dm_api_available("PH") is True and tr_.dm_api_available("sg") is True and tr_.dm_api_available("MX") is True
    assert tr_.dm_api_available("XX") is None and tr_.dm_api_available("") is None
    assert tr_.media_send_allowed("PH") is False and tr_.media_send_allowed("JP") is False
    assert tr_.media_send_allowed("SG") is True and tr_.media_send_allowed("MY") is True
    assert tr_.normalize_region("uk") == "GB" and tr_.shop_site("uk") == "UK" and tr_.shop_site("KH") == ""
    assert tr_.dm_block_reason("FR") == "region_unsupported" and tr_.dm_block_reason("XX") == "region_unknown"
    caps = tr_.capabilities("FR")
    assert caps["dm_api"] is False and "shop_customer_service" in caps["alternatives"]
    assert tr_.capabilities("SG")["alternatives"] == []
    assert len(tr_.EEA) == 30 and "NO" in tr_.EEA and "NO" not in tr_.EU


# ── 签名 / webhook ─────────────────────────────────────────────────────────────

def test_signature():
    body = b'{"event":"im_receive_msg"}'
    sig = tk.sign_body(SECRET, body)
    assert tk.verify_signature(SECRET, body, sig) and tk.verify_signature(SECRET, body, "sha256=" + sig.upper())
    assert not tk.verify_signature(SECRET, body + b"x", sig) and not tk.verify_signature("", body, sig)


async def test_webhook_receive_and_send_echo(env):
    store, st, tr, api = env
    body = _recv("how much is it?")
    calls = []

    async def ar(p):
        calls.append(p)

    status, resp = await tk.handle_webhook(body, tk.sign_body(SECRET, body), config=CFG, state=st, auto_reply=ar)
    assert (status, resp) == (200, {"ok": True})
    cid = f"tiktok:{BIZ}:tiktok:user:{USER}"
    conv = store.get_conversation(cid)
    assert conv and conv["platform"] == "tiktok" and conv["last_in_ts"] > 0
    assert [(m["direction"], m["text"]) for m in store.list_recent_messages(cid, limit=5)] == [("in", "how much is it?")]
    assert calls and calls[0]["chat_key"] == f"tiktok:user:{USER}"
    assert st.get_ctx(BIZ, USER)["conversation_id"] == "conv-1"
    # 重放 → dup
    status, resp = await tk.handle_webhook(body, tk.sign_body(SECRET, body), config=CFG, state=st, auto_reply=ar)
    assert resp.get("dup") is True and len(calls) == 1
    # 坏签名 / 坏 JSON / 探活 challenge
    assert (await tk.handle_webhook(body, "nope", config=CFG, state=st))[0] == 401
    assert (await tk.handle_webhook(b"{", tk.sign_body(SECRET, b"{"), config=CFG, state=st))[0] == 400
    ch = json.dumps({"challenge": "abc"}).encode()
    assert await tk.handle_webhook(ch, tk.sign_body(SECRET, ch), config=CFG, state=st) == (200, {"challenge": "abc"})
    # 我方回显镜像为 out；图片消息占位
    echo = _event(tk.EVENT_SEND, {"sender": BIZ, "recipient": USER, "conversation_id": "conv-1", "message_id": "m-out-9",
                                  "timestamp": int(time.time() * 1000), "message_type": "TEXT", "text": {"body": "sure"}})
    await tk.handle_webhook(echo, tk.sign_body(SECRET, echo), config=CFG, state=st, auto_reply=_noop)
    img = _event(tk.EVENT_RECEIVE, {"sender": USER, "recipient": BIZ, "conversation_id": "conv-1", "message_id": "m-in-2",
                                    "timestamp": int(time.time() * 1000), "message_type": "IMAGE", "image": {"media_id": "x"}})
    await tk.handle_webhook(img, tk.sign_body(SECRET, img), config=CFG, state=st, auto_reply=_noop)
    rows = [(m["direction"], m["text"], m["media_type"]) for m in store.list_recent_messages(cid, limit=10)]
    assert ("out", "sure", "") in rows and ("in", "[图片]", "image") in rows
    # 高意向评论：只记录
    hi = _event(tk.EVENT_HIGH_INTENT_COMMENT, {"comment_id": "c1"})
    assert (await tk.handle_webhook(hi, tk.sign_body(SECRET, hi), config=CFG, state=st))[1].get("noted") is True


# ── worker ───────────────────────────────────────────────────────────────────

class _Reg:
    def __init__(self):
        self.upserts = []

    def upsert(self, platform, account_id, **kw):
        self.upserts.append((platform, account_id, kw))


def _worker(st, api, region="SG", token="tok"):
    return tk.TikTokOfficialWorker({"account_id": BIZ, "meta": {"access_token": token, "region": region}}, CFG,
                                   api=api, state=st, registry=_Reg())


async def test_worker_send_shape_and_rules(env):
    store, st, tr, api = env
    w = _worker(st, api)
    await w.start()
    assert await w.healthy() is True
    r = await w.send(f"tiktok:user:{USER}", "hi")
    assert r == {"delivered": False, "blocked": "policy_window_no_inbound"} and tr.calls == []
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m-in-1", ts=time.time() - 60)
    r = await w.send(f"tiktok:user:{USER}", "Sure, it's $99", reply_to={"id": "m-in-1"})
    assert r["delivered"] is True and r["message_id"] == "m-out-1" and r["quote_applied"] is True
    method, url, kw = tr.calls[-1]
    assert (method, url) == ("POST", tk.SEND_URL) and kw["headers"]["Access-Token"] == "tok"
    assert kw["json_body"] == {"business_id": BIZ, "message_type": "TEXT", "recipient_type": "CONVERSATION",
                               "recipient": "conv-1", "text": {"body": "Sure, it's $99"},
                               "referenced_message_info": {"referenced_message_id": "m-in-1"}}
    assert (await w.send(f"tiktok:user:{USER}", "x" * 6001))["blocked"].startswith("policy_text_too_long")
    # 48h 后 → 超窗
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m-old", ts=time.time() - 49 * 3600)
    st.record_inbound(BIZ, "other", conversation_id="conv-2", msg_id="m-o", ts=time.time() - 49 * 3600)
    assert (await w.send("tiktok:user:other", "late"))["blocked"] == "policy_window_expired"
    # 鉴权错误码 → needs_reauth 并清 token；再发直接拒
    tr.queue.append((200, {"code": 40105, "message": "Access token is invalid", "request_id": "r2"}))
    r = await w.send(f"tiktok:user:{USER}", "again")
    assert r["blocked"] == "tiktok_needs_reauth" and w.meta["access_token"] == ""
    n = len(tr.calls)
    assert (await w.send(f"tiktok:user:{USER}", "again"))["blocked"] == "tiktok_needs_reauth" and len(tr.calls) == n
    assert await w.healthy() is False and w.status()["token_state"] == "needs_reauth"


async def test_worker_region_gating(env):
    store, st, tr, api = env
    w = _worker(st, api, region="DE")
    await w.start()
    assert await w.healthy() is False and w.status()["dm_api"] is False
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m", ts=time.time())
    assert (await w.send(f"tiktok:user:{USER}", "hi"))["blocked"] == "tiktok_region_unsupported" and tr.calls == []
    # 菲律宾：私信可用、图片不可用
    w2 = _worker(st, api, region="PH")
    assert w2.dm_ok is True and w2.media_ok is False
    r = await w2.send_media(f"tiktok:user:{USER}", media_path="/nonexistent.png", media_type="image")
    assert r["blocked"] == "tiktok_media_region_unsupported"


async def test_worker_send_media_image(env, tmp_path):
    store, st, tr, api = env
    w = _worker(st, api, region="SG")
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m", ts=time.time())
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG" + b"\0" * 32)
    tr.responses[tk.MEDIA_UPLOAD_URL] = (200, {"code": 0, "message": "OK", "data": {"media_id": "media-1"}})
    r = await w.send_media(f"tiktok:user:{USER}", media_path=str(img), media_type="image", caption="see this")
    assert r["delivered"] is True and r["caption_delivered"] is True
    assert [c[1] for c in tr.calls] == [tk.MEDIA_UPLOAD_URL, tk.SEND_URL, tk.SEND_URL]
    assert tr.calls[0][2]["form"] == {"business_id": BIZ, "media_type": "IMAGE"} and tr.calls[0][2]["file_field"] == ("file", str(img))
    assert tr.calls[1][2]["json_body"]["image"] == {"media_id": "media-1"} and tr.calls[1][2]["json_body"]["message_type"] == "IMAGE"
    assert (await w.send_media(f"tiktok:user:{USER}", media_path=str(img), media_type="video"))["blocked"] == "policy_media_type_denied:video"
    big = tmp_path / "big.png"
    big.write_bytes(b"\0" * (3 * 1024 * 1024 + 1))
    assert (await w.send_media(f"tiktok:user:{USER}", media_path=str(big), media_type="image"))["blocked"] == "policy_media_too_large:3MB"


def test_registration_gated_and_capabilities():
    from src.integrations import account_orchestrator as ao
    from src.integrations.platform_capabilities import worker_capabilities
    ao._WORKER_FACTORIES.pop("tiktok:official", None)
    assert tk.register_tiktok_official_worker({}) is False and ao.get_worker_factory("tiktok", "official") is None
    assert tk.register_tiktok_official_worker({"tiktok": {"enabled": True}}) is True
    w = ao.get_worker_factory("tiktok", "official")({"account_id": BIZ, "meta": {}}, CFG)
    assert isinstance(w, tk.TikTokOfficialWorker) and w.token_state == "needs_reauth"
    assert worker_capabilities(w) == {"send_text": True, "send_media": True, "mark_read": False, "typing": False}
    ao._WORKER_FACTORIES.pop("tiktok:official", None)


def test_routes_mount_only_when_enabled(env):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app0 = FastAPI()
    tk.register_tiktok_routes(app0, SimpleNamespace(config={}))
    assert all(getattr(r, "path", "") != tk.DEFAULT_WEBHOOK_PATH for r in app0.routes)
    app = FastAPI()
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    c = TestClient(app)
    ch = json.dumps({"challenge": 5}).encode()
    r = c.post(tk.DEFAULT_WEBHOOK_PATH, content=ch, headers={tk.DEFAULT_SIGNATURE_HEADER: tk.sign_body(SECRET, ch)})
    assert r.status_code == 200 and r.json() == {"challenge": 5}
    assert c.post(tk.DEFAULT_WEBHOOK_PATH, content=ch, headers={tk.DEFAULT_SIGNATURE_HEADER: "bad"}).status_code == 401
