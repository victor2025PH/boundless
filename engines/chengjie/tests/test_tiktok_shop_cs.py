# -*- coding: utf-8 -*-
"""TikTok Shop 店铺客服（TikTok 线续做 C 段，2026-09-10）：webhook 验签 / NEW_MESSAGE 进收件箱（source=shop、会话键
tiktok:shop:<conversation_id>）/ 幂等（tts_notification_id + message_id）/ 卖家侧回显 / 订单卡只读进侧栏 / 发送形状
（签名 query + x-tts-access-token + content JSON 字符串）/ 图片上传 / 已读 / 补拉对账分页去重 / 令牌刷新与失效 /
分流工厂注册门控与能力位。全部假 transport，零网络。末例 e2e 用 conftest 完整 admin app 走进线 → 起草钩子 → 人工发送 → 回显。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import pytest

from src.integrations import protocol_bridge as pb
from src.integrations import tiktok_shop_cs as sc
from src.inbox.store import InboxStore

APP_KEY = "ak-1"
APP_SECRET = "as-1"
SHOP = "shop-77"
CONV = "conv-9"
CFG = {"tiktok": {"shop": {"enabled": True, "app_key": APP_KEY, "app_secret": APP_SECRET}}}
OK_SEND = (200, {"code": 0, "message": "Success", "request_id": "r1", "data": {"message_id": "m-out-1"}})
SEND_URL = f"{sc.API_BASE}{sc.CONVERSATIONS_PATH}/{CONV}/messages"


class FakeTransport:
    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.responses: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.queue: List[Tuple[int, Dict[str, Any]]] = []

    async def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.queue:
            return self.queue.pop(0)
        return self.responses.get(url, OK_SEND)


class _Reg:
    def __init__(self):
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, platform, account_id):
        return self.rows.get((platform, account_id))

    def upsert(self, platform, account_id, *, meta=None, merge_meta=False, **kw):
        row = self.rows.setdefault((platform, account_id), {"platform": platform, "account_id": account_id, "meta": {}})
        row.update({k: v for k, v in kw.items() if v is not None})
        if meta is not None:
            row["meta"].update(meta) if merge_meta else row.__setitem__("meta", dict(meta))
        return row


@pytest.fixture()
def env(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = sc.TikTokShopStateStore(":memory:")
    tr = FakeTransport()
    api = sc.TikTokShopApi(transport=tr, app_key=APP_KEY, app_secret=APP_SECRET, now=lambda: 1_800_000_000.0)
    yield store, st, tr, api
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


def _data(**kw) -> Dict[str, Any]:
    base = {"conversation_id": CONV, "message_id": "m-in-1", "index": 3, "create_time": int(time.time()),
            "type": "TEXT", "visibility": "ALL", "content": json.dumps({"content": "is this in stock?"}),
            "sender": {"role": "BUYER", "im_user_id": "buyer-1", "nickname": "Ann", "avatar": "https://p/a.jpg"}}
    base.update(kw)
    return base


def _event(data: Dict[str, Any], *, etype: int = sc.EVENT_NEW_MESSAGE, nid: str = "n-1", shop_id: str = SHOP) -> bytes:
    return json.dumps({"type": etype, "tts_notification_id": nid, "shop_id": shop_id, "timestamp": int(time.time()),
                       "data": data}, ensure_ascii=False).encode("utf-8")


def _sig(body: bytes) -> str:
    return sc.sign_webhook(APP_SECRET, APP_KEY, body)


async def _noop(_p):
    return None


def _worker(st, api, token="tok", **meta_extra):
    meta = {"source": "shop", "access_token": token, "shop_cipher": "cipher-1", "shop_region": "SG",
            "access_expires_at": time.time() + 86400, "refresh_token": "rft", "refresh_expires_at": time.time() + 365 * 86400}
    meta.update(meta_extra)
    return sc.TikTokShopCSWorker({"account_id": SHOP, "meta": meta}, CFG, api=api, state=st, registry=_Reg())


# ── 1 验签 ────────────────────────────────────────────────────────────────────

def test_webhook_signature_scheme():
    body = b'{"type":14}'
    sig = sc.sign_webhook(APP_SECRET, APP_KEY, body)
    assert len(sig) == 64 and sig == sig.lower()
    assert sc.verify_webhook(APP_SECRET, APP_KEY, body, sig)
    assert sc.verify_webhook(APP_SECRET, APP_KEY, body, "Bearer " + sig.upper())   # 前缀 / 大小写容忍
    assert not sc.verify_webhook(APP_SECRET, APP_KEY, body + b" ", sig)          # 改体
    assert not sc.verify_webhook(APP_SECRET, "other-key", body, sig)             # app_key 参与签名
    assert not sc.verify_webhook("", APP_KEY, body, sig) and not sc.verify_webhook(APP_SECRET, APP_KEY, body, "")
    # 请求签名：sign/access_token 不参与，键按字典序，正文拼在末尾
    p = {"app_key": APP_KEY, "timestamp": 1, "shop_cipher": "c", "sign": "x", "access_token": "t"}
    s1 = sc.sign_request(APP_SECRET, "/customer_service/202309/conversations/1/messages", p, b'{"a":1}')
    s2 = sc.sign_request(APP_SECRET, "/customer_service/202309/conversations/1/messages",
                         {"timestamp": 1, "shop_cipher": "c", "app_key": APP_KEY}, b'{"a":1}')
    assert s1 == s2 and len(s1) == 64
    assert s1 != sc.sign_request(APP_SECRET, "/customer_service/202309/conversations/1/messages", p, b'{"a":2}')


# ── 2 进线 / 幂等 / 回显 ──────────────────────────────────────────────────────

async def test_webhook_new_message_ingests_dedupes_and_echoes(env):
    store, st, tr, api = env
    body = _event(_data())
    drafts = []

    async def ar(p):
        drafts.append(p)

    status, resp = await sc.handle_shop_webhook(body, _sig(body), config=CFG, state=st, auto_reply=ar)
    assert (status, resp) == (200, {"ok": True})
    cid = f"tiktok:{SHOP}:tiktok:shop:{CONV}"
    conv = store.get_conversation(cid)
    assert conv and conv["platform"] == "tiktok" and conv["last_in_ts"] > 0
    rows = store.list_recent_messages(cid, limit=5)
    assert [(m["direction"], m["text"]) for m in rows] == [("in", "is this in stock?")]
    assert drafts and drafts[0]["chat_key"] == f"tiktok:shop:{CONV}" and drafts[0]["name"] == "Ann"
    assert drafts[0]["source"]["source"] == "shop" and drafts[0]["source"]["shop_id"] == SHOP
    assert drafts[0]["source"]["conversation_id"] == CONV and drafts[0]["source"]["index"] == 3
    assert st.get_ctx(SHOP, CONV)["buyer_id"] == "buyer-1" and st.get_ctx(SHOP, CONV)["last_index"] == 3
    # 同 tts_notification_id 重投 → dup；同 message_id 不同 notification → 也 dup（两把幂等键）
    assert (await sc.handle_shop_webhook(body, _sig(body), config=CFG, state=st, auto_reply=ar))[1].get("dup") is True
    b2 = _event(_data(), nid="n-2")
    assert (await sc.handle_shop_webhook(b2, _sig(b2), config=CFG, state=st, auto_reply=ar))[1].get("dup") is True
    assert len(drafts) == 1 and len(store.list_recent_messages(cid, limit=5)) == 1
    # 坏签名 401；坏 JSON 400；缺 message_id 400；新会话事件只记录；未知类型忽略
    assert (await sc.handle_shop_webhook(body, "deadbeef", config=CFG, state=st))[0] == 401
    assert (await sc.handle_shop_webhook(b"{", _sig(b"{"), config=CFG, state=st))[0] == 400
    b3 = _event(_data(message_id=""), nid="n-3")
    assert (await sc.handle_shop_webhook(b3, _sig(b3), config=CFG, state=st))[0] == 400
    b4 = _event({"conversation_id": CONV}, etype=sc.EVENT_NEW_CONVERSATION, nid="n-4")
    assert (await sc.handle_shop_webhook(b4, _sig(b4), config=CFG, state=st))[1].get("noted") is True
    b5 = _event({}, etype=1, nid="n-5")
    assert (await sc.handle_shop_webhook(b5, _sig(b5), config=CFG, state=st))[1].get("ignored") == 1
    # 卖家（坐席在 Seller Center 手发）→ out 回显，不触发起草、不推进回复窗
    echo = _event(_data(message_id="m-out-9", content=json.dumps({"content": "yes, 3 left"}),
                        sender={"role": "SELLER", "im_user_id": "agent-1", "nickname": "Shop"}), nid="n-6")
    assert (await sc.handle_shop_webhook(echo, _sig(echo), config=CFG, state=st, auto_reply=ar))[1].get("echo") is True
    rows = [(m["direction"], m["text"]) for m in store.list_recent_messages(cid, limit=5)]
    assert ("out", "yes, 3 left") in rows and len(drafts) == 1
    assert st.get_ctx(SHOP, CONV)["last_index"] == 3
    # 买家发图：占位 + media_type + image_url 进 source
    img = _event(_data(message_id="m-in-2", type="IMAGE", content=json.dumps({"url": "https://img/x.jpg", "width": 100, "height": 80})), nid="n-7")
    await sc.handle_shop_webhook(img, _sig(img), config=CFG, state=st, auto_reply=ar)
    assert drafts[-1]["media_type"] == "image" and drafts[-1]["text"] == "[图片]" and drafts[-1]["source"]["image_url"] == "https://img/x.jpg"
    assert st.stats(SHOP)["events_total"] >= 4


# ── 3 订单卡只读侧栏 ─────────────────────────────────────────────────────────

async def test_order_card_read_only_sidebar(env):
    store, st, tr, api = env
    body = _event(_data(message_id="m-oc-1", type="ORDER_CARD", content=json.dumps({"order_id": "5768"})), nid="n-oc")
    got = []
    await sc.handle_shop_webhook(body, _sig(body), config=CFG, state=st, auto_reply=_noop, emit=lambda m: got.append(m))
    assert got[0]["text"] == "[订单卡片]" and got[0]["source"]["order_card"] == {"order_id": "5768"}
    cards = st.order_cards(SHOP, CONV)
    assert [c["order_id"] for c in cards] == ["5768"] and cards[0]["summary"] == {}
    w = _worker(st, api)
    tr.responses[f"{sc.API_BASE}{sc.ORDERS_PATH}"] = (200, {"code": 0, "data": {"orders": [{
        "id": "5768", "status": "AWAITING_SHIPMENT", "create_time": 1_799_999_000,
        "payment": {"total_amount": "39.90", "currency": "SGD"}, "tracking_number": "",
        "line_items": [{"product_name": "Serum 30ml", "sku_name": "default", "quantity": 2, "sale_price": "19.95"}]}]}})
    card = await w.order_card(f"tiktok:shop:{CONV}", "5768")
    assert card["ok"] and card["cached"] is False and card["status"] == "AWAITING_SHIPMENT" and card["total"] == "39.90"
    assert card["items"] == [{"name": "Serum 30ml", "sku": "default", "qty": 2, "price": "19.95"}]
    method, url, kw = tr.calls[-1]
    assert (method, url) == ("GET", f"{sc.API_BASE}{sc.ORDERS_PATH}") and kw["params"]["ids"] == "5768"
    assert kw["params"]["shop_cipher"] == "cipher-1" and kw["headers"]["x-tts-access-token"] == "tok"
    # 第二次命中状态库缓存，不再打接口；只读——模块不暴露任何订单写操作
    n = len(tr.calls)
    card2 = await w.order_card(f"tiktok:shop:{CONV}", "5768")
    assert card2["cached"] is True and card2["total"] == "39.90" and len(tr.calls) == n
    assert st.order_cards(SHOP, CONV)[0]["summary"]["order_id"] == "5768"
    assert not any(n_.startswith(("cancel", "ship", "refund", "update_order")) for n_ in dir(sc.TikTokShopApi))


# ── 4 发送形状 ───────────────────────────────────────────────────────────────

async def test_worker_send_shape_and_echo_dedupe(env):
    store, st, tr, api = env
    w = _worker(st, api)
    await w.start()
    assert await w.healthy() is True and tr.calls == []
    assert (await w.send("tiktok:shop:", "hi"))["blocked"] == "policy_window_no_inbound"
    r = await w.send(f"tiktok:shop:{CONV}", "Yes, 3 left in stock")
    assert r == {"delivered": True, "message_id": "m-out-1", "kind": "text"}
    method, url, kw = tr.calls[-1]
    assert (method, url) == ("POST", SEND_URL)
    assert kw["headers"]["x-tts-access-token"] == "tok" and kw["headers"]["Content-Type"] == "application/json"
    assert kw["json_body"] == {"type": "TEXT", "content": json.dumps({"content": "Yes, 3 left in stock"}, ensure_ascii=False)}
    p = kw["params"]
    assert p["app_key"] == APP_KEY and p["shop_cipher"] == "cipher-1" and p["timestamp"] == 1_800_000_000
    expect_sign = sc.sign_request(APP_SECRET, f"{sc.CONVERSATIONS_PATH}/{CONV}/messages", {k: v for k, v in p.items() if k != "sign"},
                                  json.dumps(kw["json_body"], ensure_ascii=False, separators=(",", ":")).encode())
    assert p["sign"] == expect_sign
    # 我方发送成功后到达的回显：message_id 已入幂等表 → dup
    echo = _event(_data(message_id="m-out-1", sender={"role": "SELLER", "im_user_id": "a"}), nid="n-e")
    assert (await sc.handle_shop_webhook(echo, _sig(echo), config=CFG, state=st, auto_reply=_noop))[1].get("dup") is True
    assert (await w.send(f"tiktok:shop:{CONV}", "x" * 6001))["blocked"].startswith("policy_text_too_long")
    tr.queue.append((200, {"code": 36009001, "message": "conversation closed"}))
    r = await w.send(f"tiktok:shop:{CONV}", "late")
    assert r["delivered"] is False and r["error_kind"] == "tiktok_shop_error_36009001" and "blocked" not in r
    assert w.status()["type"] == "tiktok_shop_cs" and w.status()["source"] == "shop" and w.status()["shop_site"] == "SG"


# ── 5 图片 ────────────────────────────────────────────────────────────────────

async def test_worker_send_media_image(env, tmp_path):
    store, st, tr, api = env
    w = _worker(st, api)
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG" + b"\0" * 32)
    tr.responses[f"{sc.API_BASE}{sc.IMAGE_UPLOAD_PATH}"] = (200, {"code": 0, "data": {"url": "https://cdn/x.png", "width": 10, "height": 8}})
    r = await w.send_media(f"tiktok:shop:{CONV}", media_path=str(img), media_type="image", caption="see this")
    assert r["delivered"] is True and r["caption_delivered"] is True
    assert [c[1] for c in tr.calls] == [f"{sc.API_BASE}{sc.IMAGE_UPLOAD_PATH}", SEND_URL, SEND_URL]
    assert tr.calls[0][2]["file_field"] == ("data", str(img))
    assert tr.calls[1][2]["json_body"] == {"type": "IMAGE", "content": json.dumps({"url": "https://cdn/x.png", "width": 10, "height": 8})}
    assert (await w.send_media(f"tiktok:shop:{CONV}", media_path=str(img), media_type="video"))["blocked"] == "policy_media_type_denied:video"
    big = tmp_path / "big.png"
    big.write_bytes(b"\0" * (3 * 1024 * 1024 + 1))
    assert (await w.send_media(f"tiktok:shop:{CONV}", media_path=str(big), media_type="image"))["blocked"] == "policy_media_too_large:3MB"
    tr.responses[f"{sc.API_BASE}{sc.IMAGE_UPLOAD_PATH}"] = (200, {"code": 36001, "message": "too large"})
    assert (await w.send_media(f"tiktok:shop:{CONV}", media_path=str(img), media_type="image"))["error_kind"] == "tiktok_shop_image_upload"


# ── 6 已读 ────────────────────────────────────────────────────────────────────

async def test_worker_mark_read(env):
    store, st, tr, api = env
    w = _worker(st, api)
    tr.responses[f"{SEND_URL}/read"] = (200, {"code": 0, "message": "Success"})
    assert await w.mark_read(f"tiktok:shop:{CONV}") is True
    method, url, kw = tr.calls[-1]
    assert (method, url) == ("POST", f"{SEND_URL}/read") and kw["json_body"] == {} and kw["params"]["shop_cipher"] == "cipher-1"
    tr.responses[f"{SEND_URL}/read"] = (200, {"code": 105002, "message": "token expired"})
    assert await w.mark_read(f"tiktok:shop:{CONV}") is False
    assert await w.mark_read("") is False
    from src.integrations.platform_capabilities import worker_capabilities
    assert worker_capabilities(w) == {"send_text": True, "send_media": True, "mark_read": True, "typing": False}


# ── 7 补拉对账 ───────────────────────────────────────────────────────────────

async def test_backfill_paginates_and_dedupes(env):
    store, st, tr, api = env
    w = _worker(st, api)
    # 先经 webhook 落一条，再补拉三页（≤10/页）：已落的不重复，卖家消息 out，带 backfill 标
    body = _event(_data(message_id="m-1", index=1), nid="n-b1")
    await sc.handle_shop_webhook(body, _sig(body), config=CFG, state=st, auto_reply=_noop)
    list_url = f"{sc.API_BASE}{sc.CONVERSATIONS_PATH}/{CONV}/messages"
    pages = [
        (200, {"code": 0, "data": {"messages": [_data(message_id="m-1", index=1), _data(message_id="m-2", index=2, content=json.dumps({"content": "size M?"}))],
                                   "next_page_token": "p2"}}),
        (200, {"code": 0, "data": {"messages": [_data(message_id="m-3", index=3, sender={"role": "SELLER", "im_user_id": "a"},
                                                      content=json.dumps({"content": "M available"}))], "next_page_token": "p3"}}),
        (200, {"code": 0, "data": {"messages": [], "next_page_token": ""}}),
    ]
    tr.queue.extend(pages)
    got = []
    r = await w.backfill(f"tiktok:shop:{CONV}", emit=lambda m: (got.append(m), pb.emit_incoming(m)))
    assert r == {"ok": True, "pulled": 3, "new": 2}
    gets = [c for c in tr.calls if c[0] == "GET" and c[1] == list_url]
    assert len(gets) == 3 and all(c[2]["params"]["page_size"] == 10 for c in gets)
    assert "page_token" not in gets[0][2]["params"] and gets[1][2]["params"]["page_token"] == "p2" and gets[2][2]["params"]["page_token"] == "p3"
    assert [(m["msg_id"], m["direction"]) for m in got] == [("m-2", "in"), ("m-3", "out")]
    assert all(m["source"]["backfill"] == 1 and m["source"]["backfill_source"] == "tiktok_shop_cs" for m in got)
    cid = f"tiktok:{SHOP}:tiktok:shop:{CONV}"
    assert len(store.list_recent_messages(cid, limit=10, include_deleted=True)) == 3
    assert st.get_ctx(SHOP, CONV)["last_index"] == 2
    # 拉取不置已读：整个补拉过程没有 POST
    assert all(c[0] == "GET" for c in tr.calls)
    # 再补拉一遍全是旧的 → new 0
    tr.queue.append((200, {"code": 0, "data": {"messages": [_data(message_id="m-2", index=2)], "next_page_token": ""}}))
    assert (await w.backfill(f"tiktok:shop:{CONV}", emit=lambda m: None)) == {"ok": True, "pulled": 1, "new": 0}
    tr.queue.append((200, {"code": 105002, "message": "expired"}))
    assert (await w.backfill(f"tiktok:shop:{CONV}", emit=lambda m: None))["ok"] is False


# ── 8 令牌 + 分流工厂注册门控 ─────────────────────────────────────────────────

async def test_token_paths_and_dispatch_registration(env):
    store, st, tr, api = env
    # ① access 已到期、refresh 活 → start 自动刷新（auth.tiktok-shops.com，GET query）并落注册表
    w = _worker(st, api, access_expires_at=time.time() - 10)
    assert w.token_state == "refresh_due"
    tr.queue.append((200, {"code": 0, "data": {"access_token": "tok2", "access_token_expire_in": int(time.time()) + 7 * 86400,
                                               "refresh_token": "rft2", "refresh_token_expire_in": int(time.time()) + 300 * 86400,
                                               "seller_name": "My Shop", "seller_base_region": "SG"}}))
    await w.start()
    assert w.token_state == "ok" and w.meta["access_token"] == "tok2" and w.meta["refresh_token"] == "rft2"
    assert tr.calls[0][1] == f"{sc.AUTH_BASE}{sc.TOKEN_REFRESH_PATH}"
    assert tr.calls[0][2]["params"] == {"app_key": APP_KEY, "app_secret": APP_SECRET, "refresh_token": "rft", "grant_type": "refresh_token"}
    assert w._registry.get("tiktok", SHOP)["meta"]["access_token"] == "tok2"
    # ② 发送遇鉴权码 → 强刷 → 重试成功；③ 刷新也失败 → needs_reauth 清令牌，后续直接拒
    tr.queue += [(200, {"code": 105002, "message": "expired"}),
                 (200, {"code": 0, "data": {"access_token": "tok3", "access_token_expire_in": int(time.time()) + 86400}}), OK_SEND]
    assert (await w.send(f"tiktok:shop:{CONV}", "again"))["delivered"] is True and w.meta["access_token"] == "tok3"
    tr.queue += [(200, {"code": 105002, "message": "expired"}), (200, {"code": 105004, "message": "refresh invalid"})]
    r = await w.send(f"tiktok:shop:{CONV}", "again")
    assert r["blocked"] == "tiktok_needs_reauth" and w.token_state == "needs_reauth" and await w.healthy() is False
    n = len(tr.calls)
    assert (await w.send(f"tiktok:shop:{CONV}", "x"))["blocked"] == "tiktok_needs_reauth" and len(tr.calls) == n
    # ④ 分流工厂：默认关不注册；开了把 (tiktok, official) 包成分流器，meta.source=shop → Shop worker，其余 → 私信 worker；幂等
    from src.integrations import account_orchestrator as ao
    from src.integrations import tiktok_official as tk
    ao._WORKER_FACTORIES.pop("tiktok:official", None)
    assert sc.register_tiktok_shop_cs_worker({}) is False and ao.get_worker_factory("tiktok", "official") is None
    assert sc.is_shop_account({"meta": {"source": "shop"}}) and sc.is_shop_account({"meta": {"kind": "SHOP"}})
    assert not sc.is_shop_account({"meta": {}}) and not sc.is_shop_account(None)
    assert tk.register_tiktok_official_worker({"tiktok": {"enabled": True}}) is True
    assert sc.register_tiktok_shop_cs_worker(CFG) is True
    f = ao.get_worker_factory("tiktok", "official")
    assert getattr(f, "_tiktok_shop_dispatch", False) is True
    assert sc.register_tiktok_shop_cs_worker(CFG) is True and ao.get_worker_factory("tiktok", "official") is f   # 不重复包
    assert isinstance(f({"account_id": SHOP, "meta": {"source": "shop"}}, CFG), sc.TikTokShopCSWorker)
    assert isinstance(f({"account_id": "biz", "meta": {}}, CFG), tk.TikTokOfficialWorker)
    # 只开 shop 不开私信：非 shop 账号回落私信 worker（needs_reauth，无凭据即不健康），不抛
    ao._WORKER_FACTORIES.pop("tiktok:official", None)
    assert sc.register_tiktok_shop_cs_worker(CFG) is True
    assert isinstance(ao.get_worker_factory("tiktok", "official")({"account_id": "biz", "meta": {}}, CFG), tk.TikTokOfficialWorker)
    ao._WORKER_FACTORIES.pop("tiktok:official", None)
    # 路由：默认关不挂；开了挂 webhook + 订单卡只读接口（幂等）；私信 webhook 仍按自己的开关
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app0 = FastAPI()
    tk.register_tiktok_routes(app0, SimpleNamespace(config={}))
    assert all(getattr(r_, "path", "") not in (sc.DEFAULT_WEBHOOK_PATH, sc.ORDER_CARD_ROUTE, tk.DEFAULT_WEBHOOK_PATH) for r_ in app0.routes)
    app = FastAPI()
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    paths = [getattr(r_, "path", "") for r_ in app.routes]
    assert paths.count(sc.DEFAULT_WEBHOOK_PATH) == 1 and paths.count(sc.ORDER_CARD_ROUTE) == 1 and tk.DEFAULT_WEBHOOK_PATH not in paths
    sc._reset_for_tests()
    c = TestClient(app)
    body = _event(_data(message_id="m-route-1"), nid="n-route")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sc, "get_state_store", lambda *_a, **_k: st)
        r = c.post(sc.DEFAULT_WEBHOOK_PATH, content=body, headers={"Authorization": _sig(body)})
        assert r.status_code == 200 and r.json() == {}      # 官方要求空 200
        assert c.post(sc.DEFAULT_WEBHOOK_PATH, content=body, headers={"Authorization": "bad"}).status_code == 401
        r = c.get(sc.ORDER_CARD_ROUTE, params={"account_id": SHOP, "chat_key": f"tiktok:shop:{CONV}"})
        assert r.status_code == 200 and r.json()["ok"] is True and isinstance(r.json()["cards"], list)


# ── 9 e2e：进线 → 起草钩子 → 收件箱可见 → 人工发送（走编排器 = 同一道 channel_policy）→ 回显 ──────

@pytest.fixture()
def shop_ready(app, tmp_path):
    from src.integrations import account_orchestrator as ao
    from src.inbox.store import InboxStore as _Store
    app.state.inbox_store = _Store(tmp_path / "inbox_shop_e2e.db")
    st = sc.TikTokShopStateStore(":memory:")
    tr = FakeTransport()
    api = sc.TikTokShopApi(transport=tr, app_key=APP_KEY, app_secret=APP_SECRET)
    w = _worker(st, api)
    w.running = True
    w._report_session_health()   # 令牌健康 → 会话健康表 authorized（前面单测的 needs_reauth 记录是同进程单例，须覆盖）
    orch = ao.get_orchestrator()
    key = ao.account_key("tiktok", SHOP)
    orch._managed[key] = ao._Managed(key=key, platform="tiktok", account_id=SHOP, mode="official", worker=w, state="running")
    try:
        yield app.state.inbox_store, st, tr, w
    finally:
        orch._managed.pop(key, None)


async def test_shop_e2e_inbound_draft_send_echo(auth_client, app, shop_ready):
    store, st, tr, w = shop_ready
    chat = f"tiktok:shop:{CONV}"
    drafts = []

    async def draft_hook(p):
        drafts.append(p)

    # 1) 进线：webhook → 收件箱（source=shop）→ 起草钩子被调（真实链路里是 maybe_auto_reply → System Z 拟稿）
    body = _event(_data(content=json.dumps({"content": "can you ship to Johor?"})), nid="n-e2e")
    status, _ = await sc.handle_shop_webhook(body, _sig(body), config=CFG, state=st, auto_reply=draft_hook,
                                             emit=lambda m: pb.ingest_incoming(store, **m))
    assert status == 200 and drafts and drafts[0]["chat_key"] == chat and drafts[0]["source"]["source"] == "shop"
    # 2) 列表可见，形状与其它平台一致
    r = auth_client.get("/api/unified-inbox/chats", params={"platform": "tiktok", "limit": 30})
    assert r.status_code == 200, r.text
    mine = [c for c in (r.json().get("chats") or []) if c.get("chat_key") == chat]
    assert mine and mine[0]["account_id"] == SHOP and mine[0].get("can_send") is True
    # 3) 线程可读
    r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": SHOP, "chat_key": chat})
    assert r.status_code == 200 and any("Johor" in str(m.get("text")) for m in r.json().get("messages") or [])
    # 4) 人工发送首条带外链 → 同一道 channel_policy（TikTok 首条禁链）409，worker 未被调
    r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": SHOP, "chat_key": chat,
                                                        "text": "order here https://bd2026.cc/x", "skip_translate": True})
    assert r.status_code == 409, r.text
    d = r.json().get("detail") or r.json()
    assert d.get("code") == "send_blocked" and d.get("reason") == "policy_link_denied" and tr.calls == []
    # 5) 正常发送 → 假 transport 收到 Shop CS 发送形状 → 出站镜像回线程
    r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": SHOP, "chat_key": chat,
                                                        "text": "Yes, we ship to Johor in 3-5 days.", "skip_translate": True})
    assert r.status_code == 200, r.text
    assert tr.calls and tr.calls[-1][1] == SEND_URL and json.loads(tr.calls[-1][2]["json_body"]["content"])["content"].startswith("Yes, we ship")
    r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": SHOP, "chat_key": chat})
    assert any(m.get("direction") == "out" and "Johor" in str(m.get("text")) for m in r.json().get("messages") or [])
    # 6) 平台回显同 message_id → 幂等，不二次落库
    echo = _event(_data(message_id="m-out-1", sender={"role": "SELLER", "im_user_id": "a"}, content=json.dumps({"content": "Yes, we ship to Johor in 3-5 days."})), nid="n-e2e-echo")
    assert (await sc.handle_shop_webhook(echo, _sig(echo), config=CFG, state=st, auto_reply=_noop,
                                         emit=lambda m: pb.ingest_incoming(store, **m)))[1].get("dup") is True
    # 7) send-caps 带回复窗快照（与私信同一 UI 契约字段：cap / sent / remaining / deadline_ts）
    r = auth_client.get("/api/unified-inbox/send-caps", params={"platform": "tiktok", "account_id": SHOP, "chat_key": chat})
    rw = r.json().get("reply_window") or {}
    assert rw.get("cap") == 10 and rw.get("sent") == 1 and rw.get("remaining") == 9 and rw.get("deadline_ts") > 0
