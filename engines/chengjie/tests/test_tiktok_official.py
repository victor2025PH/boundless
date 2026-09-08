# -*- coding: utf-8 -*-
"""TikTok 官方通道（实施100 T1/T2，2026-09-08）：地区模型 / 官方验签（Tiktok-Signature t=,s=）/ 官方载荷
（user_openid + content 字符串、方向按 to_user.id）→ 收件箱 / 幂等与回显去重 / 发送形状 / 令牌刷新与重试 /
图片（地区门控 + 3MB）/ OAuth 换令牌落注册表 / webhook 编程注册 / 注册门控 / 路由。全部假 transport，零网络。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from src.integrations import tiktok_official as tk
from src.integrations import tiktok_regions as tr_
from src.integrations import protocol_bridge as pb
from src.inbox.store import InboxStore

SECRET = "app-secret"
BIZ = "biz-1"
USER = "user-9"
CFG = {"tiktok": {"enabled": True, "app_id": "app-1", "secret": SECRET}}
OK_SEND = (200, {"code": 0, "message": "OK", "request_id": "r1", "data": {"message": {"message_id": "m-out-1"}}})


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


def _content(**kw) -> Dict[str, Any]:
    base = {"conversation_id": "conv-1", "message_id": "m-in-1", "timestamp": int(time.time() * 1000),
            "message_type": "text", "text": {"body": "hello there"}, "from": "Ann",
            "from_user": {"id": USER, "role": "USER"}, "to": "Shop", "to_user": {"id": BIZ, "role": "BUSINESS"}}
    base.update(kw)
    return base


def _event(event: str, content: Dict[str, Any], business_id: str = BIZ) -> bytes:
    # 官方外壳：content 是二次序列化的 JSON 字符串
    return json.dumps({"event": event, "user_openid": business_id, "content": json.dumps(content, ensure_ascii=False)},
                      ensure_ascii=False).encode("utf-8")


def _sig(body: bytes, ts=None) -> str:
    return tk.sign_body(SECRET, body, ts)


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


# ── 官方验签 ───────────────────────────────────────────────────────────────────

def test_signature_official_scheme():
    body = b'{"event":"im_receive_msg"}'
    now = 1_800_000_000
    hdr = tk.sign_body(SECRET, body, now)
    ts, s = tk.parse_signature_header(hdr)
    assert ts == now and len(s) == 64
    assert tk.verify_signature(SECRET, body, hdr, now=now + 10)
    assert tk.verify_signature(SECRET, body, f"s={s}, t={now}", now=now)          # 顺序无关
    assert not tk.verify_signature(SECRET, body, hdr, now=now + 301)               # 超容忍
    assert tk.verify_signature(SECRET, body, hdr, now=now + 301, tolerance=600)
    assert not tk.verify_signature(SECRET, body + b"x", hdr, now=now)              # 改体
    assert not tk.verify_signature("other", body, hdr, now=now)                    # 换密钥
    assert not tk.verify_signature(SECRET, body, f"t={now + 1},s={s}", now=now)    # 改时间戳
    assert not tk.verify_signature("", body, hdr, now=now) and not tk.verify_signature(SECRET, body, "garbage", now=now)


# ── webhook ──────────────────────────────────────────────────────────────────

async def test_webhook_receive_echo_read_and_dedupe(env):
    store, st, tr, api = env
    body = _event(tk.EVENT_RECEIVE, _content(text={"body": "how much is it?"},
                                             referral={"short_link": [{"ref": "ig_bio", "prefilled_message": "hi"}]}))
    calls = []

    async def ar(p):
        calls.append(p)

    status, resp = await tk.handle_webhook(body, _sig(body), config=CFG, state=st, auto_reply=ar)
    assert (status, resp) == (200, {"ok": True})
    cid = f"tiktok:{BIZ}:tiktok:user:{USER}"
    conv = store.get_conversation(cid)
    assert conv and conv["platform"] == "tiktok" and conv["last_in_ts"] > 0
    rows = store.list_recent_messages(cid, limit=5)
    assert [(m["direction"], m["text"]) for m in rows] == [("in", "how much is it?")]
    assert calls and calls[0]["chat_key"] == f"tiktok:user:{USER}" and calls[0]["name"] == "Ann"
    assert calls[0]["source"]["ref"] == "ig_bio" and calls[0]["source"]["conversation_id"] == "conv-1"
    assert st.get_ctx(BIZ, USER)["conversation_id"] == "conv-1"
    # 重放 → dup；坏签名 401；坏 JSON 400；探活 challenge
    assert (await tk.handle_webhook(body, _sig(body), config=CFG, state=st, auto_reply=ar))[1].get("dup") is True
    assert len(calls) == 1
    assert (await tk.handle_webhook(body, "t=1,s=00", config=CFG, state=st))[0] == 401
    assert (await tk.handle_webhook(b"{", _sig(b"{"), config=CFG, state=st))[0] == 400
    ch = json.dumps({"challenge": "abc"}).encode()
    assert await tk.handle_webhook(ch, _sig(ch), config=CFG, state=st) == (200, {"challenge": "abc"})
    # 商家从 App 手发：im_send_msg 且 to_user 是客户 → out 回显镜像
    echo = _event(tk.EVENT_SEND, _content(message_id="m-out-9", text={"body": "sure"}, from_user={"id": BIZ},
                                          to_user={"id": USER}))
    assert (await tk.handle_webhook(echo, _sig(echo), config=CFG, state=st, auto_reply=_noop))[1].get("echo") is True
    # 客户发图：占位 + media_type + media_id 进 source
    img = _event(tk.EVENT_RECEIVE, _content(message_id="m-in-2", message_type="image", text=None,
                                            image={"media_id": "media-x"}))
    await tk.handle_webhook(img, _sig(img), config=CFG, state=st, auto_reply=_noop)
    rows = [(m["direction"], m["text"], m["media_type"]) for m in store.list_recent_messages(cid, limit=10)]
    assert ("out", "sure", "") in rows and ("in", "[图片]", "image") in rows
    # 已读事件 / 高意向评论：只记录
    rd = _event(tk.EVENT_MARK_READ, {"conversation_id": "conv-1"})
    assert (await tk.handle_webhook(rd, _sig(rd), config=CFG, state=st))[1].get("read") is True
    hi = _event(tk.EVENT_HIGH_INTENT_COMMENT, {"comment_id": "c1"})
    assert (await tk.handle_webhook(hi, _sig(hi), config=CFG, state=st))[1].get("noted") is True


async def test_webhook_direction_by_to_user_even_for_receive_event(env):
    """官方文档：两个事件都可能带商家侧消息，方向唯一以 to_user.id == business_id 判定。"""
    store, st, tr, api = env
    body = _event(tk.EVENT_RECEIVE, _content(message_id="m-x", from_user={"id": BIZ}, to_user={"id": USER},
                                             text={"body": "from shop"}))
    status, resp = await tk.handle_webhook(body, _sig(body), config=CFG, state=st, auto_reply=_noop)
    assert resp.get("echo") is True
    assert st.get_ctx(BIZ, USER) == {}   # 不是进线，不开回复窗


# ── worker ───────────────────────────────────────────────────────────────────

class _Reg:
    def __init__(self):
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, platform, account_id):
        return self.rows.get((platform, account_id))

    def list(self, platform=None, **_):
        return [r for (p, _a), r in self.rows.items() if platform is None or p == platform]

    def upsert(self, platform, account_id, *, meta=None, merge_meta=False, **kw):
        row = self.rows.setdefault((platform, account_id), {"platform": platform, "account_id": account_id, "meta": {}})
        for k, v in kw.items():
            if v is not None:
                row[k] = v
        if meta is not None:
            if merge_meta:
                row["meta"].update(meta)
            else:
                row["meta"] = dict(meta)
        return row


def _worker(st, api, region="SG", token="tok", **meta_extra):
    meta = {"access_token": token, "region": region, "access_expires_at": time.time() + 86400,
            "refresh_token": "rft", "refresh_expires_at": time.time() + 365 * 86400}
    meta.update(meta_extra)
    return tk.TikTokOfficialWorker({"account_id": BIZ, "meta": meta}, CFG, api=api, state=st, registry=_Reg())


async def test_worker_send_shape_and_rules(env):
    store, st, tr, api = env
    w = _worker(st, api)
    await w.start()
    assert await w.healthy() is True and tr.calls == []      # 令牌未到期，不刷
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
    # 我方发送成功后到达的回显：幂等表命中 → dup，不二次落库
    echo = _event(tk.EVENT_SEND, _content(message_id="m-out-1", from_user={"id": BIZ}, to_user={"id": USER},
                                          text={"body": "Sure, it's $99"}))
    assert (await tk.handle_webhook(echo, _sig(echo), config=CFG, state=st, auto_reply=_noop))[1].get("dup") is True
    assert (await w.send(f"tiktok:user:{USER}", "x" * 6001))["blocked"].startswith("policy_text_too_long")
    st.record_inbound(BIZ, "other", conversation_id="conv-2", msg_id="m-o", ts=time.time() - 49 * 3600)
    assert (await w.send("tiktok:user:other", "late"))["blocked"] == "policy_window_expired"


async def test_worker_token_refresh_paths(env):
    store, st, tr, api = env
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m", ts=time.time())
    # ① access 已到期、refresh 活 → start 时自动刷新并落 meta
    w = _worker(st, api, access_expires_at=time.time() - 10)
    assert w.token_state == "refresh_due"
    tr.queue.append((200, {"code": 0, "data": {"access_token": "tok2", "expires_in": 86400, "refresh_token": "rft2",
                                               "refresh_token_expires_in": 3000000, "open_id": BIZ, "scope": "x"}}))
    await w.start()
    assert w.token_state == "ok" and w.meta["access_token"] == "tok2" and w.meta["refresh_token"] == "rft2"
    assert tr.calls[0][1] == tk.REFRESH_URL and tr.calls[0][2]["json_body"]["grant_type"] == "refresh_token"
    assert w._registry.get("tiktok", BIZ)["meta"]["access_token"] == "tok2"
    # ② 发送遇鉴权码 → 强刷一次 → 重试成功
    tr.queue += [(200, {"code": 40105, "message": "Access token is invalid"}),
                 (200, {"code": 0, "data": {"access_token": "tok3", "expires_in": 86400, "refresh_token": "rft3",
                                            "refresh_token_expires_in": 3000000}}), OK_SEND]
    r = await w.send(f"tiktok:user:{USER}", "again")
    assert r["delivered"] is True and w.meta["access_token"] == "tok3"
    assert [c[1] for c in tr.calls[-3:]] == [tk.SEND_URL, tk.REFRESH_URL, tk.SEND_URL]
    # ③ 鉴权码且刷新也失败 → needs_reauth，清 token，后续直接拒
    tr.queue += [(200, {"code": 40105, "message": "invalid"}), (200, {"code": 40105, "message": "refresh invalid"})]
    r = await w.send(f"tiktok:user:{USER}", "again")
    assert r["blocked"] == "tiktok_needs_reauth" and w.token_state == "needs_reauth"
    n = len(tr.calls)
    assert (await w.send(f"tiktok:user:{USER}", "x"))["blocked"] == "tiktok_needs_reauth" and len(tr.calls) == n
    assert await w.healthy() is False
    # ④ 纯函数矩阵
    now = 1_800_000_000.0
    assert tk.token_state({}, now) == "needs_reauth"
    assert tk.token_state({"access_token": "a", "access_expires_at": now + 3600}, now) == "ok"
    assert tk.token_state({"access_token": "a", "access_expires_at": now + 60, "refresh_token": "r",
                           "refresh_expires_at": now + 1e6}, now) == "refresh_due"
    assert tk.token_state({"access_token": "a", "access_expires_at": now + 60}, now) == "ok"          # 无 refresh：撑到到期
    assert tk.token_state({"access_token": "a", "access_expires_at": now - 1, "refresh_token": "r",
                           "refresh_expires_at": now - 1}, now) == "needs_reauth"


async def test_worker_region_gating(env):
    store, st, tr, api = env
    w = _worker(st, api, region="DE")
    await w.start()
    assert await w.healthy() is False and w.status()["dm_api"] is False
    st.record_inbound(BIZ, USER, conversation_id="conv-1", msg_id="m", ts=time.time())
    assert (await w.send(f"tiktok:user:{USER}", "hi"))["blocked"] == "tiktok_region_unsupported" and tr.calls == []
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


# ── OAuth / webhook 注册 ─────────────────────────────────────────────────────────

def test_oauth_state_and_authorize_url():
    now = 1_800_000_000.0
    st = tk.oauth_state(SECRET, "sg", now)
    assert tk.verify_oauth_state(SECRET, st, now + 5) == "SG"
    assert tk.verify_oauth_state(SECRET, st, now + 601) is None and tk.verify_oauth_state("other", st, now) is None
    ts, reg, mac = st.split(".")
    assert tk.verify_oauth_state(SECRET, f"{ts}.MY.{mac}", now) is None     # 改地区
    assert tk.verify_oauth_state(SECRET, tk.oauth_state(SECRET, "", now), now) == ""
    u = tk.authorize_url("app-1", "https://x.example.com/webhook/tiktok/oauth/callback", st)
    p = urlparse(u)
    assert f"{p.scheme}://{p.netloc}{p.path}" == tk.AUTHORIZE_URL
    q = parse_qs(p.query)
    assert q["client_key"] == ["app-1"] and q["response_type"] == ["code"] and q["state"] == [st]
    assert set(q["scope"][0].split(",")) >= set(tk.MESSAGING_SCOPES)
    assert tk.missing_scopes("user.info.basic,message.list.read") == ("message.list.send", "message.list.manage")
    assert tk.missing_scopes(",".join(tk.OAUTH_SCOPES)) == ()


async def test_complete_oauth_registers_account(env):
    store, st, tr, api = env
    reg = _Reg()
    reg.upsert("tiktok", "open-1", label="我的号", meta={"persona_id": "p-1"})
    now = 1_800_000_000.0
    tr.responses[tk.TOKEN_URL] = (200, {"code": 0, "data": {
        "access_token": "act", "expires_in": 86400, "refresh_token": "rft", "refresh_token_expires_in": 31536000,
        "open_id": "open-1", "scope": ",".join(tk.OAUTH_SCOPES)}})
    tr.responses[tk.BUSINESS_GET_URL] = (200, {"code": 0, "data": {"username": "shop_sg", "display_name": "Shop SG",
                                                                    "profile_image": "https://p/a.jpg"}})
    res = await tk.complete_oauth("code-1", config=CFG, redirect_uri="https://x/cb", region="SG", registry=reg, api=api, now=now)
    assert res["ok"] and res["open_id"] == "open-1" and res["username"] == "shop_sg"
    assert tr.calls[0][1] == tk.TOKEN_URL and tr.calls[0][2]["json_body"] == {
        "client_id": "app-1", "client_secret": SECRET, "grant_type": "authorization_code", "auth_code": "code-1",
        "redirect_uri": "https://x/cb"}
    assert tr.calls[1][1] == tk.BUSINESS_GET_URL and tr.calls[1][2]["headers"]["Access-Token"] == "act"
    row = reg.get("tiktok", "open-1")
    m = row["meta"]
    assert row["mode"] == "official" and row["label"] == "我的号" and m["persona_id"] == "p-1"
    assert m["access_token"] == "act" and m["access_expires_at"] == now + 86400 and m["refresh_expires_at"] == now + 31536000
    assert m["region"] == "SG" and m["display_name"] == "Shop SG" and m["avatar_url"] == "https://p/a.jpg"
    assert tk.token_state(m, now) == "ok"
    # scope 不齐 → 明确错误、不落库
    tr.responses[tk.TOKEN_URL] = (200, {"code": 0, "data": {"access_token": "a", "open_id": "open-2",
                                                            "scope": "user.info.basic,message.list.read"}})
    res = await tk.complete_oauth("c", config=CFG, redirect_uri="https://x/cb", registry=reg, api=api)
    assert res["error"] == "ungranted_scopes" and res["missing"] == ["message.list.send", "message.list.manage"]
    assert reg.get("tiktok", "open-2") is None
    tr.responses[tk.TOKEN_URL] = (200, {"code": 40110, "message": "invalid auth_code"})
    assert (await tk.complete_oauth("c", config=CFG, redirect_uri="u", registry=reg, api=api))["error"] == "exchange_failed"
    assert (await tk.complete_oauth("c", config={}, registry=reg, redirect_uri="u", api=api))["error"] == "missing_credentials"


async def test_ensure_webhook_idempotent(env):
    store, st, tr, api = env
    tr.responses[tk.WEBHOOK_LIST_URL] = (200, {"code": 0, "data": {"callback_url": "https://x/webhook/tiktok"}})
    r = await tk.ensure_webhook(CFG, "https://x/webhook/tiktok", api=api)
    assert r == {"ok": True, "callback_url": "https://x/webhook/tiktok", "changed": False}
    assert [c[1] for c in tr.calls] == [tk.WEBHOOK_LIST_URL]
    tr.responses[tk.WEBHOOK_UPDATE_URL] = (200, {"code": 0, "data": {"callback_url": "https://y/webhook/tiktok"}})
    r = await tk.ensure_webhook(CFG, "https://y/webhook/tiktok", api=api)
    assert r["ok"] and r["changed"] is True
    assert tr.calls[-1][2]["json_body"] == {"app_id": "app-1", "secret": SECRET, "event_type": "DIRECT_MESSAGE",
                                            "callback_url": "https://y/webhook/tiktok"}
    tr.responses[tk.WEBHOOK_UPDATE_URL] = (200, {"code": 40001, "message": "bad"})
    assert (await tk.ensure_webhook(CFG, "https://z/webhook/tiktok", api=api))["error"] == "webhook_update_failed"


async def test_capabilities_probe(env):
    store, st, tr, api = env
    tr.responses[tk.CAPABILITIES_URL] = (200, {"code": 0, "data": {"capability_infos": [
        {"capability_type": "SEND_TEXT", "capability_result": True}, {"capability_type": "SEND_IMAGE", "capability_result": False}]}})
    r = await api.capabilities(access_token="t", business_id=BIZ, capability_types=["SEND_TEXT", "SEND_IMAGE"])
    assert r == {"ok": True, "capabilities": {"SEND_TEXT": True, "SEND_IMAGE": False}}
    assert json.loads(tr.calls[0][2]["params"]["capability_types"]) == ["SEND_TEXT", "SEND_IMAGE"]


# ── T4 入口经营 / T5 值守 ────────────────────────────────────────────────────────

def test_tiktok_me_link_and_ref_sanitize():
    assert tk.tiktok_me_link("@shop_sg") == "https://tiktok.me/shop_sg"
    assert tk.tiktok_me_link("shop_sg", "ig-bio_2026=a") == "https://tiktok.me/shop_sg?ref=ig-bio_2026=a"
    assert tk.tiktok_me_link("shop_sg", "bad ref!#") == "https://tiktok.me/shop_sg?ref=badref"
    assert tk.sanitize_ref("x" * 100) == "x" * 60 and tk.tiktok_me_link("", "r") == ""


async def test_ref_attribution_and_event_stats(env):
    store, st, tr, api = env
    t0 = 1_800_000_000.0
    for i, ref in enumerate(["ig_bio", "ig_bio", "poster", ""]):
        body = _event(tk.EVENT_RECEIVE, _content(message_id=f"m-{i}", from_user={"id": f"u{i}"},
                                                 referral=({"short_link": [{"ref": ref}]} if ref else {}),
                                                 timestamp=int((t0 + i) * 1000)))
        await tk.handle_webhook(body, _sig(body, int(t0 + i)), config=CFG, state=st, now=t0 + i, auto_reply=_noop)
    assert st.ref_counts(BIZ) == {"ig_bio": 2, "poster": 1}
    assert st.get_ctx(BIZ, "u0")["ref"] == "ig_bio" and st.get_ctx(BIZ, "u3")["ref"] == ""
    # 同一用户再来不改首条进线的 ref
    body = _event(tk.EVENT_RECEIVE, _content(message_id="m-9", from_user={"id": "u0"},
                                             referral={"short_link": [{"ref": "other"}]}))
    await tk.handle_webhook(body, _sig(body, int(t0 + 9)), config=CFG, state=st, now=t0 + 9, auto_reply=_noop)
    assert st.get_ctx(BIZ, "u0")["ref"] == "ig_bio"
    s = st.stats(BIZ)
    assert s["events_total"] == 5 and s["first_event_ts"] == t0 and s["last_event_ts"] == t0 + 9
    assert tk.webhook_silence(s, t0 + 9 + 3600)["silent"] is False
    sil = tk.webhook_silence(s, t0 + 9 + 25 * 3600)
    assert sil["silent"] is True and sil["seen_any"] is True and int(sil["silent_sec"]) == 25 * 3600
    assert tk.webhook_silence(st.stats("nobody"), t0)["seen_any"] is False


def test_reauth_days_and_health_warning(monkeypatch):
    now = 1_800_000_000.0
    d = 86400.0
    assert tk.reauth_days_left({"access_token": "a", "access_expires_at": now + 3 * d}, now) == 3
    assert tk.reauth_days_left({"access_token": "a", "access_expires_at": now + d, "refresh_token": "r",
                                "refresh_expires_at": now + 40 * d}, now) == 40
    assert tk.reauth_days_left({"access_token": "a"}, now) is None
    records = []

    class _H:
        def record(self, platform, account_id, status, *, detail="", login_id=""):
            records.append((status, detail))
            return {}

    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "get_platform_session_health", lambda: _H())
    clock = {"t": now}
    meta = {"access_token": "a", "access_expires_at": now + 30 * d, "refresh_token": "r", "refresh_expires_at": now + 6 * d + 100,
            "region": "SG"}
    w = tk.TikTokOfficialWorker({"account_id": BIZ, "meta": meta}, CFG, api=tk.TikTokApi(transport=FakeTransport()),
                                state=tk.TikTokStateStore(":memory:"), registry=_Reg(), now=lambda: clock["t"])
    w._report_session_health()
    w._report_session_health()
    assert len(records) == 1 and records[0][0] == "authorized" and "6 天后到期" in records[0][1]
    clock["t"] = now + d
    w._report_session_health()
    assert len(records) == 2 and "5 天后到期" in records[1][1]
    st = w.status()
    assert st["reauth_days_left"] == 5 and st["auto_refresh"] is True and st["webhook"]["seen_any"] is False
    # 手填令牌（无 refresh）也预警
    w2 = tk.TikTokOfficialWorker({"account_id": "b2", "meta": {"access_token": "a", "access_expires_at": now + 2 * d, "region": "SG"}},
                                 CFG, api=tk.TikTokApi(transport=FakeTransport()), state=tk.TikTokStateStore(":memory:"),
                                 registry=_Reg(), now=lambda: now)
    w2._report_session_health()
    assert records[-1][0] == "authorized" and "手填的 access_token 2 天后到期" in records[-1][1]


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
    r = c.post(tk.DEFAULT_WEBHOOK_PATH, content=ch, headers={tk.DEFAULT_SIGNATURE_HEADER: _sig(ch)})
    assert r.status_code == 200 and r.json() == {"challenge": 5}
    assert c.post(tk.DEFAULT_WEBHOOK_PATH, content=ch, headers={tk.DEFAULT_SIGNATURE_HEADER: "t=1,s=bad"}).status_code == 401
