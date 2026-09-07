# -*- coding: utf-8 -*-
"""抖音官方通道骨架（实施96 P1-1）：签名 / challenge / 幂等 / 事件→收件箱 / 场景选择 / 发送与错误映射 /
图片上传 / 令牌生命周期 / 进私快路径 / 注册门控 / FastAPI 路由。全部经假 transport，零网络。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import pytest

from src.integrations import douyin_official as dy
from src.integrations import protocol_bridge as pb
from src.inbox.store import InboxStore

SECRET = "s3cr3t"
BIZ = "biz-open-id"          # 经营者 open_id ＝ account_id
CUST = "cust-open-id"        # 客户 open_id
CFG = {"douyin": {"enabled": True, "client_key": "ck", "client_secret": SECRET,
                  "enter_greeting": "您好，欢迎咨询，请问想了解哪款？"}}


class FakeTransport:
    """记录每次调用；按 URL 返回预置响应（可用 queue 覆盖单次）。"""

    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.responses: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.queue: List[Tuple[int, Dict[str, Any]]] = []

    async def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.queue:
            return self.queue.pop(0)
        return self.responses.get(url, (200, {"extra": {"error_code": 0}, "data": {"error_code": 0},
                                              "msg_id": "@srv-out-1"}))


def _event(event, content, *, frm=CUST, to=BIZ):
    return json.dumps({"event": event, "client_key": "ck", "from_user_id": frm, "to_user_id": to,
                       "content": content, "log_id": "log-1"}, ensure_ascii=False).encode("utf-8")


def _recv(text="多少钱", smid="@srv-in-1", ts_ms=None):
    return _event(dy.EVENT_RECEIVE, {"conversation_short_id": "@conv-1", "server_message_id": smid,
                                     "conversation_type": 1, "message_type": "text", "text": text,
                                     "create_time": int((ts_ms or time.time() * 1000))})


@pytest.fixture()
def env(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = dy.DouyinStateStore(":memory:")
    tr = FakeTransport()
    api = dy.DouyinApi(transport=tr)
    yield store, st, tr, api
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


async def _noop_auto_reply(_payload):
    return None


# ── 签名 / challenge / 幂等 ──────────────────────────────────────────────────────

def test_signature_roundtrip_and_rejects():
    body = b'{"event":"verify_webhook","client_key":"abc","content":{"challenge":12345}}'
    sig = dy.sign_body(SECRET, body)
    assert len(sig) == 40 and dy.verify_signature(SECRET, body, sig)
    assert dy.verify_signature(SECRET, body, sig.upper())
    assert not dy.verify_signature(SECRET, body + b" ", sig)
    assert not dy.verify_signature("other", body, sig)
    assert not dy.verify_signature("", body, sig)   # 未配 secret 一律拒


async def test_webhook_verify_challenge_and_bad_signature(env):
    store, st, tr, api = env
    body = _event(dy.EVENT_VERIFY, {"challenge": 4242}, frm="", to="")
    status, resp = await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st, api=api)
    assert (status, resp) == (200, {"challenge": 4242})
    status, resp = await dy.handle_webhook(body, "deadbeef", config=CFG, state=st, api=api)
    assert status == 401 and resp["error"] == "bad_signature"
    status, _ = await dy.handle_webhook(b"not json", dy.sign_body(SECRET, b"not json"), config=CFG, state=st)
    assert status == 400


async def test_receive_event_lands_in_inbox_and_is_idempotent(env):
    store, st, tr, api = env
    body = _recv("多少钱")
    calls = []

    async def ar(payload):
        calls.append(payload)

    status, resp = await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st,
                                           api=api, auto_reply=ar)
    assert (status, resp) == (200, {"ok": True})
    conv = store.get_conversation(f"douyin:{BIZ}:douyin:user:{CUST}")
    assert conv is not None and conv["platform"] == "douyin" and conv["last_in_ts"] > 0
    msgs = store.list_recent_messages(conv["conversation_id"], limit=10)
    assert [(m["direction"], m["text"]) for m in msgs] == [("in", "多少钱")]
    assert calls and calls[0]["chat_key"] == f"douyin:user:{CUST}"
    ctx = st.get_ctx(BIZ, CUST)
    assert ctx["conversation_id"] == "@conv-1" and ctx["last_msg_id"] == "@srv-in-1"
    # 重放 → dup，不再落库、不再触发回复
    status, resp = await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st,
                                           api=api, auto_reply=ar)
    assert resp.get("dup") is True and len(calls) == 1
    assert len(store.list_recent_messages(conv["conversation_id"], limit=10)) == 1


async def test_non_text_types_become_placeholders(env):
    store, st, tr, api = env
    body = _event(dy.EVENT_RECEIVE, {"conversation_short_id": "@c", "server_message_id": "@img-1",
                                     "message_type": "image", "create_time": int(time.time() * 1000)})
    await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st, api=api,
                            auto_reply=_noop_auto_reply)
    msgs = store.list_recent_messages(f"douyin:{BIZ}:douyin:user:{CUST}", limit=5)
    assert msgs and msgs[0]["text"] == "[图片]" and msgs[0]["media_type"] == "image"


async def test_send_echo_event_mirrors_outbound(env):
    store, st, tr, api = env
    body = _event(dy.EVENT_SEND, {"conversation_short_id": "@c", "server_message_id": "@srv-out-9",
                                  "message_type": "text", "text": "好的",
                                  "create_time": int(time.time() * 1000)}, frm=BIZ, to=CUST)
    status, _ = await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st, api=api,
                                        auto_reply=_noop_auto_reply)
    msgs = store.list_recent_messages(f"douyin:{BIZ}:douyin:user:{CUST}", limit=5)
    assert status == 200 and [(m["direction"], m["text"]) for m in msgs] == [("out", "好的")]


# ── 场景选择 ───────────────────────────────────────────────────────────────────

def test_choose_scene_rules():
    now = 1_000_000.0
    assert dy.choose_scene({}, now)[3] == "policy_window_no_inbound"
    ctx = {"conversation_id": "@c", "last_msg_id": "@m", "last_msg_ts": now - 3600}
    assert dy.choose_scene(ctx, now)[:3] == (dy.SCENE_REPLY, "@m", "@c")
    assert dy.choose_scene({**ctx, "last_msg_ts": now - 25 * 3600}, now)[3] == "policy_window_expired"
    enter = {**ctx, "enter_msg_id": "@e", "enter_ts": now - 10}
    assert dy.choose_scene(enter, now)[:2] == (dy.SCENE_ENTER, "@e")          # 30 秒内优先进私场景
    assert dy.choose_scene({**enter, "enter_ts": now - 31}, now)[0] == dy.SCENE_REPLY  # 超 30 秒回落回复场景


# ── worker 发送 ─────────────────────────────────────────────────────────────────

def _worker(st, api, meta=None, now=None):
    m = {"access_token": "act.x", "refresh_token": "rt.x", "access_expires_at": time.time() + 10 * 86400,
         "refresh_expires_at": time.time() + 25 * 86400, "renew_count": 0}
    m.update(meta or {})
    return dy.DouyinOfficialWorker({"account_id": BIZ, "meta": m}, CFG, api=api, state=st,
                                   registry=_Reg(), now=now)


class _Reg:
    def __init__(self):
        self.upserts = []

    def upsert(self, platform, account_id, **kw):
        self.upserts.append((platform, account_id, kw))

    def get(self, platform, account_id):
        return None


async def test_worker_send_requires_inbound_then_uses_reply_scene(env):
    store, st, tr, api = env
    w = _worker(st, api)
    r = await w.send(f"douyin:user:{CUST}", "你好")
    assert r == {"delivered": False, "blocked": "policy_window_no_inbound"} and tr.calls == []
    st.record_inbound(BIZ, CUST, conversation_id="@conv-1", msg_id="@srv-in-1", ts=time.time() - 60)
    r = await w.send(f"douyin:user:{CUST}", "亲，99 元")
    assert r["delivered"] is True and r["message_id"] == "@srv-out-1" and r["scene"] == dy.SCENE_REPLY
    method, url, kw = tr.calls[-1]
    assert (method, url) == ("POST", dy.SEND_MSG_URL)
    assert kw["params"] == {"open_id": BIZ} and kw["headers"]["access-token"] == "act.x"
    body = kw["json_body"]
    assert body["scene"] == dy.SCENE_REPLY and body["to_user_id"] == CUST
    assert body["msg_id"] == "@srv-in-1" and body["conversation_id"] == "@conv-1"
    assert body["content"] == {"msg_type": 1, "text": {"text": "亲，99 元"}}
    assert (await w.send(f"douyin:user:{CUST}", "字" * 1001))["blocked"].startswith("policy_text_too_long")


async def test_worker_send_maps_platform_errors(env):
    store, st, tr, api = env
    w = _worker(st, api)
    st.record_inbound(BIZ, CUST, conversation_id="@c", msg_id="@m", ts=time.time() - 60)
    tr.queue.append((200, {"extra": {"error_code": 28003081, "description": "上条消息已经超过24小时"}}))
    r = await w.send(f"douyin:user:{CUST}", "在吗")
    assert r["delivered"] is False and r["blocked"] == "policy_window_expired"
    tr.queue.append((200, {"extra": {"error_code": 28003070, "description": "超出频控限制次数"}}))
    assert (await w.send(f"douyin:user:{CUST}", "在吗"))["blocked"] == "policy_window_exhausted"
    tr.queue.append((200, {"extra": {"error_code": 28003095, "description": "线上已有生效消息策略"}}))
    assert (await w.send(f"douyin:user:{CUST}", "在吗"))["blocked"] == "douyin_workbench_policy_active"
    tr.queue.append((200, {"extra": {"error_code": 2190008, "description": "access_token过期"}}))
    r = await w.send(f"douyin:user:{CUST}", "在吗")
    assert r["blocked"] == "douyin_token_expired" and w.meta["access_token"] == ""
    # token 清空后再发 → 直接 needs_reauth，不打接口
    n = len(tr.calls)
    r = await w.send(f"douyin:user:{CUST}", "在吗")
    assert r["blocked"] == "douyin_needs_reauth" and len(tr.calls) == n


async def test_worker_send_media_image_and_reject_video(env, tmp_path):
    store, st, tr, api = env
    w = _worker(st, api)
    st.record_inbound(BIZ, CUST, conversation_id="@c", msg_id="@m", ts=time.time() - 60)
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    tr.responses[dy.CLIENT_TOKEN_URL] = (200, {"data": {"error_code": 0, "access_token": "clt.1", "expires_in": 7200}})
    tr.responses[dy.IMAGE_UPLOAD_URL] = (200, {"data": {"error_code": 0, "image_id": "@img-id"}})
    r = await w.send_media(f"douyin:user:{CUST}", media_path=str(img), media_type="image", caption="看这张")
    assert r["delivered"] is True and r["kind"] == "image" and r["caption_delivered"] is True
    urls = [c[1] for c in tr.calls]
    assert urls == [dy.CLIENT_TOKEN_URL, dy.IMAGE_UPLOAD_URL, dy.SEND_MSG_URL, dy.SEND_MSG_URL]
    assert tr.calls[1][2]["headers"]["access-token"] == "clt.1" and tr.calls[1][2]["file_field"] == ("image", str(img))
    assert tr.calls[2][2]["json_body"]["content"] == {"msg_type": 2, "image": {"media_id": "@img-id"}}
    r = await w.send_media(f"douyin:user:{CUST}", media_path=str(img), media_type="video")
    assert r == {"delivered": False, "blocked": "policy_media_type_denied:video"}
    # client_token 复用（2h 内不重取）
    n = len(tr.calls)
    await w.send_media(f"douyin:user:{CUST}", media_path=str(img), media_type="image")
    assert [c[1] for c in tr.calls[n:]] == [dy.IMAGE_UPLOAD_URL, dy.SEND_MSG_URL]


async def test_worker_send_card(env):
    store, st, tr, api = env
    w = _worker(st, api)
    st.record_inbound(BIZ, CUST, conversation_id="@c", msg_id="@m", ts=time.time() - 60)
    r = await w.send_card(f"douyin:user:{CUST}", {"type": "question", "title": "想了解？",
                                                 "questions": ["价格", "怎么预约", "有优惠吗"]})
    assert r["delivered"] is True
    content = tr.calls[-1][2]["json_body"]["content"]
    assert content["msg_type"] == 204 and len(content["question_guide_msg_card"]["question_list"]) == 3
    r = await w.send_card(f"douyin:user:{CUST}", {"type": "retain", "card_id": "@card"})
    assert tr.calls[-1][2]["json_body"]["content"] == {"msg_type": 8, "retain_consult_card": {"card_id": "@card"}}
    assert (await w.send_card(f"douyin:user:{CUST}", {"type": "nope"}))["blocked"] == "douyin_unknown_card"


# ── 令牌生命周期 ────────────────────────────────────────────────────────────────

def test_token_state_matrix():
    now = 1_700_000_000.0
    d = 86400.0
    base = {"access_token": "a", "refresh_token": "r", "access_expires_at": now + 10 * d,
            "refresh_expires_at": now + 25 * d, "renew_count": 0}
    assert dy.token_state(base, now) == "ok"
    assert dy.token_state({**base, "access_expires_at": now + 0.5 * d}, now) == "refresh_due"
    assert dy.token_state({**base, "refresh_expires_at": now + 2 * d}, now) == "renew_due"
    assert dy.token_state({**base, "refresh_expires_at": now + 2 * d, "renew_count": 5}, now) == "ok"
    # 续满 5 次但 refresh 仍在有效期：access 过期仍可用它刷 access（真正的 reauth 是 refresh 死了）
    assert dy.token_state({**base, "refresh_expires_at": now + 2 * d, "renew_count": 5,
                           "access_expires_at": now - 1}, now) == "refresh_due"
    assert dy.token_state({**base, "refresh_expires_at": now - 1, "access_expires_at": now - 1}, now) == "needs_reauth"
    assert dy.token_state({}, now) == "needs_reauth"


async def test_worker_refreshes_and_renews_tokens(env):
    store, st, tr, api = env
    now = [1_700_000_000.0]
    d = 86400.0
    w = _worker(st, api, meta={"access_expires_at": now[0] + 0.5 * d, "refresh_expires_at": now[0] + 20 * d},
                now=lambda: now[0])
    tr.responses[dy.REFRESH_TOKEN_URL] = (200, {"data": {"error_code": 0, "access_token": "act.new",
                                                          "expires_in": 15 * d, "refresh_token": "rt.x",
                                                          "refresh_expires_in": 20 * d}})
    await w.start()
    assert w.meta["access_token"] == "act.new" and w.token_state == "ok"
    assert w._registry.upserts and w._registry.upserts[-1][2]["merge_meta"] is True
    assert tr.calls[-1][2]["form"]["grant_type"] == "refresh_token"
    # refresh 到期前 2 天（access 仍有效）→ 续期一次，计数 +1
    w.meta["refresh_expires_at"] = now[0] + 2 * d
    tr.responses[dy.RENEW_REFRESH_URL] = (200, {"data": {"error_code": 0, "refresh_token": "rt.new",
                                                         "expires_in": 30 * d}})
    assert await w.healthy() is True
    assert w.meta["refresh_token"] == "rt.new" and w.meta["renew_count"] == 1
    assert w.meta["refresh_expires_at"] == pytest.approx(now[0] + 30 * d)
    # 同一巡检内 access 到期 + refresh 快到期 → 两步都做（先刷后续）；刷新响应里的
    # refresh_expires_in 是**剩余**时长（刷 access 不延长 refresh），按平台语义给 2 天
    w.meta.update({"access_expires_at": now[0] + 0.5 * d, "refresh_expires_at": now[0] + 2 * d})
    tr.responses[dy.REFRESH_TOKEN_URL] = (200, {"data": {"error_code": 0, "access_token": "act.new2",
                                                          "expires_in": 15 * d, "refresh_token": "rt.new",
                                                          "refresh_expires_in": 2 * d}})
    n = len(tr.calls)
    assert await w.healthy() is True
    assert [c[1] for c in tr.calls[n:]] == [dy.REFRESH_TOKEN_URL, dy.RENEW_REFRESH_URL]
    assert w.meta["renew_count"] == 2
    # refresh 已过期且 access 也过期 → needs_reauth，healthy False（续满 5 次后不再能续，最终必到这里）
    w.meta.update({"renew_count": 5, "refresh_expires_at": now[0] - 1, "access_expires_at": now[0] - 1})
    n = len(tr.calls)
    assert await w.healthy() is False and w.token_state == "needs_reauth"
    assert len(tr.calls) == n  # 不再徒劳打接口


async def test_token_state_reports_to_session_health(env):
    """令牌到期 → 会话健康表记 expired（账号 chip「需重新登录」同一机制）；恢复记 authorized；同态不重报。"""
    from src.integrations.platform_session_health import get_platform_session_health
    store, st, tr, api = env
    h = get_platform_session_health()
    before = h.total_events
    now = [1_700_000_000.0]
    w = _worker(st, api, now=lambda: now[0])
    await w.start()                                   # ok → authorized
    assert h.total_events == before + 1
    await w.healthy()                                 # 同态 → 不重报
    assert h.total_events == before + 1
    w.meta.update({"refresh_expires_at": now[0] - 1, "access_expires_at": now[0] - 1})
    assert await w.healthy() is False                 # needs_reauth → expired
    assert h.total_events == before + 2
    sess = h.snapshot().get("sessions", {}) if hasattr(h, "snapshot") else {}
    row = None
    for k, v in (sess.items() if isinstance(sess, dict) else []):
        if k.startswith(f"douyin:{BIZ}"):
            row = v
    if row is not None:
        assert row.get("status") == "expired" and "重新扫码授权" in str(row.get("detail") or "")
    assert w.status()["token_state"] == "needs_reauth"


# ── 进私快路径 ──────────────────────────────────────────────────────────────────

async def test_enter_event_sends_greeting_within_fast_path(env):
    store, st, tr, api = env
    reg = _Reg()
    reg.get = lambda p, a: {"account_id": BIZ, "meta": {"access_token": "act.x", "refresh_token": "rt",
                                                          "access_expires_at": time.time() + 86400 * 10,
                                                          "refresh_expires_at": time.time() + 86400 * 20}}
    body = _event(dy.EVENT_ENTER, {"conversation_short_id": "@conv-1", "server_message_id": "@enter-1"})
    status, resp = await dy.handle_webhook(body, dy.sign_body(SECRET, body), config=CFG, state=st, api=api,
                                           registry=reg, auto_reply=_noop_auto_reply)
    assert (status, resp) == (200, {"ok": True, "greeted": True})
    sent = tr.calls[-1][2]["json_body"]
    assert sent["scene"] == dy.SCENE_ENTER and sent["msg_id"] == "@enter-1" and sent["conversation_id"] == "@conv-1"
    assert sent["content"]["text"]["text"] == CFG["douyin"]["enter_greeting"]
    msgs = store.list_recent_messages(f"douyin:{BIZ}:douyin:user:{CUST}", limit=5)
    assert [(m["direction"], m["text"]) for m in msgs] == [("out", CFG["douyin"]["enter_greeting"])]
    # 未配问候语 → 不发、不报错
    cfg2 = {"douyin": {**CFG["douyin"], "enter_greeting": ""}}
    body2 = _event(dy.EVENT_ENTER, {"conversation_short_id": "@conv-1", "server_message_id": "@enter-2"})
    n = len(tr.calls)
    status, resp = await dy.handle_webhook(body2, dy.sign_body(SECRET, body2), config=cfg2, state=st, api=api,
                                           registry=reg, auto_reply=_noop_auto_reply)
    assert resp == {"ok": True, "greeted": False} and len(tr.calls) == n


# ── 注册门控 + 路由 ─────────────────────────────────────────────────────────────

def test_worker_factory_registration_is_gated():
    from src.integrations import account_orchestrator as ao
    ao._WORKER_FACTORIES.pop("douyin:official", None)   # pop 而非 monkeypatch.delitem（后者收尾会恢复）
    assert dy.register_douyin_official_worker({}) is False
    assert ao.get_worker_factory("douyin", "official") is None
    assert dy.register_douyin_official_worker({"platform_login": {"official": {"douyin": {"enabled": True}}}}) is True
    assert ao.worker_supported("douyin", "official") is True
    w = ao.get_worker_factory("douyin", "official")({"account_id": BIZ, "meta": {}}, CFG)
    assert isinstance(w, dy.DouyinOfficialWorker) and w.token_state == "needs_reauth"
    from src.integrations.platform_capabilities import worker_capabilities
    assert worker_capabilities(w) == {"send_text": True, "send_media": True, "mark_read": False, "typing": False}
    ao._WORKER_FACTORIES.pop("douyin:official", None)


def test_routes_mount_only_when_enabled(env):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    store, st, tr, api = env
    app0 = FastAPI()
    dy.register_douyin_routes(app0, SimpleNamespace(config={}))
    assert all(getattr(r, "path", "") != dy.DEFAULT_WEBHOOK_PATH for r in app0.routes)
    app = FastAPI()
    dy.register_douyin_routes(app, SimpleNamespace(config=CFG))
    c = TestClient(app)
    body = _event(dy.EVENT_VERIFY, {"challenge": 7}, frm="", to="")
    r = c.post(dy.DEFAULT_WEBHOOK_PATH, content=body, headers={dy.SIGNATURE_HEADER: dy.sign_body(SECRET, body)})
    assert r.status_code == 200 and r.json() == {"challenge": 7}
    r = c.post(dy.DEFAULT_WEBHOOK_PATH, content=body, headers={dy.SIGNATURE_HEADER: "bad"})
    assert r.status_code == 401
