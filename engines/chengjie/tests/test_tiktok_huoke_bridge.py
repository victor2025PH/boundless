"""TikTok × huoke 桥——假 huoke，零网络。

E 段（评论通道，2026-09-10）：线索进线 / 回传队列闸门 / 路由默认关 + 挂载契约 / e2e。
TK-3 ①-B（私信通道，同日）：私信进线（关系态 / 回显 / 幂等）/ 消息请求·对方沉默·夜间静默·账号时区日上限·真机离线
闸门 / 认领节奏 + 回执镜像升 sent + 失败留痕 / 设备绑定 + 心跳健康三态 / 路由 / e2e。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pytest

from src.integrations import protocol_bridge as pb
from src.integrations import tiktok_huoke_bridge as hb
from src.inbox.store import InboxStore

ACC = "dev-acc-1"
DEV = "device-A"
CFG = {"tiktok": {"huoke_bridge": {"enabled": True, "min_intent": 0.6, "daily_cap": 2, "dm_daily_cap": 3, "min_gap_sec": 30}}}
T0 = 1_800_000_000.0


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def upsert(self, platform, account_id, **kw):
        self.rows[f"{platform}:{account_id}"] = dict(kw)


class FakeHealth:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def record(self, platform, account_id, status, *, detail="", login_id=""):
        self.events.append((platform, account_id, status, detail))
        return {"changed": True}


def _lead(cid: str, uid: str = "u1", text: str = "how much is this? ship to Manila?", intent: float = 0.9, **kw) -> Dict[str, Any]:
    d = {"comment_id": cid, "video_id": "v-100", "user_id": uid, "username": f"user_{uid}", "name": f"User {uid}",
         "text": text, "intent_score": intent, "ts": T0, "lang": "en"}
    d.update(kw)
    return d


def _dm(mid: str, text: str = "hi, is this still available?", *, peer: str = "buyer_1", direction: str = "in", ts: float = T0,
        mutual: Any = None, follower: Any = None, thread_type: str = "", **kw) -> Dict[str, Any]:
    d: Dict[str, Any] = {"msg_id": mid, "peer_username": peer, "peer_name": peer.title(), "text": text, "ts": ts, "direction": direction}
    rel: Dict[str, Any] = {}
    if mutual is not None:
        rel["is_mutual"] = mutual
    if follower is not None:
        rel["is_follower"] = follower
    if rel:
        d["relation"] = rel
    if thread_type:
        d["thread_type"] = thread_type
    d.update(kw)
    return d


async def _noop(_m):
    return None


@pytest.fixture()
def env(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = hb.TikTokHuokeStateStore(":memory:")
    yield store, st
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


# ═══ E 段：评论通道 ═══════════════════════════════════════════════════════════════════════

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
    from src.integrations.leadbus_account import PERSONAL_RPA_MODE, is_lead_capture_text
    from src.integrations.account_orchestrator import ORCHESTRATED_MODES
    assert reg.rows[f"tiktok:{ACC}"]["mode"] == PERSONAL_RPA_MODE and PERSONAL_RPA_MODE not in ORCHESTRATED_MODES
    assert st.account(ACC)["device_id"] == DEV and st.account(ACC)["leads_total"] == 1
    m = emitted[0]
    assert m["platform"] == "tiktok" and m["account_id"] == ACC and m["chat_key"] == "tiktok:comment:u1"
    assert m["direction"] == "in" and m["msg_id"] == "cmt:c1" and not is_lead_capture_text(m["text"])
    assert m["source"]["source"] == "huoke" and m["source"]["kind"] == "comment" and m["source"]["mode"] == "personal_rpa"
    assert drafted and drafted[0] is m
    status, res = await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c1")]},
                                        config=CFG, state=st, now=T0 + 1, emit=emit, auto_reply=draft, registry=reg)
    assert res["accepted"] == 0 and res["dup"] == 1 and len(drafted) == 1
    assert (await hb.ingest_leads({"leads": []}, config=CFG, state=st, registry=reg))[0] == 400
    assert (await hb.ingest_leads("x", config=CFG, state=st, registry=reg))[0] == 400
    ctx = st.ctx(ACC, "tiktok:comment:u1")
    assert ctx["last_comment_id"] == "c1" and ctx["out_since_inbound"] == 0 and ctx["last_inbound_ts"] == T0 and ctx["kind"] == "comment"


async def test_comment_handback_gates_claim_ack_and_echo(env):
    store, st = env
    reg = FakeRegistry()
    chat = "tiktok:comment:u1"
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c1")]}, config=CFG, state=st, now=T0,
                          emit=lambda m: None, auto_reply=_noop, registry=reg)
    assert hb.enqueue_reply("other", chat, "hi", config=CFG, state=st)["reason"] == hb.REASON_NOT_BRIDGED
    assert hb.enqueue_reply(ACC, "tiktok:shop:x", "hi", config=CFG, state=st)["reason"] == hb.REASON_NOT_BRIDGED
    r = hb.enqueue_reply(ACC, chat, "x" * 151, config=CFG, state=st, now=T0)
    assert r["ok"] is False and r["reason"].startswith(hb.REASON_TOO_LONG) and r["status"] == 409
    r1 = hb.enqueue_reply(ACC, chat, "Hi! Yes we ship to Manila, DM us for a quote.", config=CFG, state=st, now=T0 + 10)
    assert r1["ok"] is True and r1["kind"] == "comment"
    r2 = hb.enqueue_reply(ACC, chat, "second", config=CFG, state=st, now=T0 + 11)
    assert r2["ok"] is False and r2["reason"] == hb.REASON_ONE_REPLY and r2["status"] == 409
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c2", text="ok how to order")]},
                          config=CFG, state=st, now=T0 + 20, emit=lambda m: None, auto_reply=_noop, registry=reg)
    r3 = hb.enqueue_reply(ACC, chat, "Link in bio", config=CFG, state=st, now=T0 + 30)
    assert r3["ok"] is True
    await hb.ingest_leads({"device_id": DEV, "account_id": ACC, "leads": [_lead("c3", text="thanks")]},
                          config=CFG, state=st, now=T0 + 40, emit=lambda m: None, auto_reply=_noop, registry=reg)
    r4 = hb.enqueue_reply(ACC, chat, "np", config=CFG, state=st, now=T0 + 50)
    assert r4["ok"] is False and r4["reason"].startswith(hb.REASON_DAILY_CAP) and r4["status"] == 429
    items = st.claim(DEV, limit=10, now=T0 + 60)
    assert [i["id"] for i in items] == [r1["item_id"], r3["item_id"]] and items[0]["comment_id"] == "c1" and items[1]["comment_id"] == "c2"
    assert items[0]["status"] == "claimed" and items[0]["claimed_by"] == DEV and items[0]["kind"] == "comment"
    assert st.claim(DEV, limit=10, now=T0 + 61) == []
    assert st.claim("device-B", limit=10, now=T0 + 61) == []
    assert [i["id"] for i in st.claim(DEV, limit=10, now=T0 + 60 + 601)] == [r1["item_id"], r3["item_id"]]
    # 回执成功：老队列项无 hb 镜像 → 补一条 out 回显（status_reporter 返回 False）；重复回执幂等
    echoed: List[Dict[str, Any]] = []
    status, res = hb.ack_handback({"item_id": r1["item_id"], "ok": True, "external_id": "reply-777"}, config=CFG, state=st,
                                  now=T0 + 100, emit=echoed.append, status_reporter=lambda *a: False)
    assert status == 200 and res["status"] == "sent" and res["dup"] is False
    assert len(echoed) == 1 and echoed[0]["direction"] == "out" and echoed[0]["msg_id"] == "reply-777"
    status, res = hb.ack_handback({"item_id": r1["item_id"], "ok": True}, config=CFG, state=st, emit=echoed.append,
                                  status_reporter=lambda *a: False)
    assert res["dup"] is True and len(echoed) == 1
    # 回执失败 → 收件箱失败留痕（原因随行）、额度退回
    class _Store:
        calls: List[tuple] = []

        def record_failed_outbound(self, conv, text, *, reason=""):
            self.calls.append((conv, text, reason))
            return "m-fail"
    fs = _Store()
    status, res = hb.ack_handback({"item_id": r3["item_id"], "ok": False, "error": "comment_deleted"}, config=CFG, state=st,
                                  emit=echoed.append, store=fs)
    assert res["status"] == "failed" and len(echoed) == 1
    assert fs.calls == [(f"tiktok:{ACC}:{chat}", "Link in bio", "huoke:comment_deleted")]
    assert st.ctx(ACC, chat)["out_since_inbound"] == 0
    assert hb.ack_handback({"item_id": 9999, "ok": True}, config=CFG, state=st)[0] == 404
    assert hb.ack_handback({"ok": True}, config=CFG, state=st)[0] == 400
    s = st.summary()
    assert (s["sent"], s["failed"], s["queued"], s["accounts"]) == (1, 1, 0, 1)


# ═══ TK-3：私信通道 ═══════════════════════════════════════════════════════════════════════

async def test_dm_ingest_relation_ctx_echo_dedupe_and_draft(env):
    store, st = env
    reg, health = FakeRegistry(), FakeHealth()
    emitted: List[Dict[str, Any]] = []
    drafted: List[Dict[str, Any]] = []

    def emit(m):
        emitted.append(m)
        pb.ingest_incoming(store, **m)

    async def draft(m):
        drafted.append(m)

    payload = {"device_id": DEV, "account_id": ACC, "username": "myshop_ph", "timezone": "Asia/Manila", "messages": [
        _dm("o1", "Hi! Saw you liked our video 😊", direction="out", ts=T0 - 600, mutual=False, follower=True, thread_type="request"),
        _dm("i1", "hi, is this still available?", ts=T0, mutual=False, follower=True, thread_type="request"),
        _dm("i1"),                                        # dup
        _dm("i2", "", media_type="image", ts=T0 + 1),     # 纯媒体 → 占位文本
        {"msg_id": "bad", "text": "no peer"},              # invalid
    ]}
    orig_report = hb.report_health
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hb, "report_health", lambda st_, acc, **kw: orig_report(st_, acc, health_sink=health, **kw))
        status, res = await hb.ingest_dm(payload, config=CFG, state=st, now=T0 + 2, emit=emit, auto_reply=draft, registry=reg)
    assert status == 200 and res == {"ok": True, "accepted": 2, "echo": 1, "dup": 1, "invalid": 1, "drafted": 2}
    # 账号：personal_rpa 登记、时区 / 用户名 / 心跳
    acc = st.account(ACC)
    assert reg.rows[f"tiktok:{ACC}"]["mode"] == "personal_rpa" and acc["timezone"] == "Asia/Manila" and acc["username"] == "myshop_ph"
    assert acc["last_seen_ts"] == T0 + 2 and health.events[-1][2] == "authorized"
    # 消息形状：同键 tiktok:user:<username>（与官方私信 worker 一致），huoke 首触 = out 回显不起草
    chat = "tiktok:user:buyer_1"
    assert [m["direction"] for m in emitted] == ["out", "in", "in"] and all(m["chat_key"] == chat for m in emitted)
    assert emitted[0]["source"]["echo"] is True and emitted[0]["source"]["origin"] == "huoke" and emitted[0]["msg_id"] == "o1"
    assert emitted[1]["source"] == {"source": "huoke", "kind": "dm", "mode": "personal_rpa", "reply_engine": "chengjie",
                                    "account_id": ACC, "device_id": DEV, "lang": "", "thread_type": "request",
                                    "is_mutual": False, "is_follower": True}
    assert emitted[2]["text"] == "[image]" and emitted[2]["media_type"] == "image"
    assert [d["msg_id"] for d in drafted] == ["i1", "i2"]
    ctx = st.ctx(ACC, chat)
    assert ctx["kind"] == "dm" and ctx["is_mutual"] == 0 and ctx["is_follower"] == 1 and ctx["thread_type"] == "request"
    assert ctx["last_inbound_ts"] == T0 + 1 and ctx["last_out_ts"] == T0 - 600 and ctx["out_since_inbound"] == 0
    assert ctx["username"] == "buyer_1" and ctx["peer_name"] == "Buyer_1"
    # 坏载荷
    assert (await hb.ingest_dm({"account_id": ACC}, config=CFG, state=st))[0] == 400
    assert (await hb.ingest_dm([], config=CFG, state=st))[0] == 400


async def test_dm_gates_request_pending_peer_silent_quiet_hours_daily_cap_tz_and_offline(env):
    store, st = env
    reg = FakeRegistry()
    chat = "tiktok:user:buyer_1"
    manila = ZoneInfo("Asia/Manila")
    # 22:30 马尼拉
    t_night = datetime(2027, 1, 10, 22, 30, tzinfo=manila).timestamp()
    hb.bind_device({"device_id": DEV, "account_id": ACC, "timezone": "Asia/Manila"}, config=CFG, state=st, now=t_night - 60, registry=reg)
    # 只有 huoke 首触、对方未回 → 消息请求未回，一条都不发（自动 / 人工同判）
    await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [_dm("o1", "hello", direction="out", ts=t_night - 50)]},
                       config=CFG, state=st, now=t_night - 50, emit=lambda m: None, auto_reply=_noop, registry=reg)
    for origin in ("auto", "manual"):
        r = hb.enqueue_reply(ACC, chat, "still there?", config=CFG, state=st, now=t_night - 40, origin=origin)
        assert r["ok"] is False and r["reason"] == hb.REASON_REQUEST_PENDING and r["status"] == 409
    # 对方回了 → 可发；私信不受「一入站一条」（对话中多条正常）
    await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [_dm("i1", "yes?", ts=t_night - 30, mutual=False)]},
                       config=CFG, state=st, now=t_night - 30, emit=lambda m: None, auto_reply=_noop, registry=reg)
    # 首条禁链（channel_policy tiktok）与超长（dm_max_len 1000）
    r = hb.enqueue_reply(ACC, chat, "x" * 1001, config=CFG, state=st, now=t_night)
    assert r["ok"] is False and r["reason"].startswith("policy_text_too_long")
    r1 = hb.enqueue_reply(ACC, chat, "Yes! 350 pesos, free shipping in Metro Manila.", config=CFG, state=st, now=t_night, origin="auto")
    r2 = hb.enqueue_reply(ACC, chat, "Want me to reserve one?", config=CFG, state=st, now=t_night + 1, origin="auto")
    assert r1["ok"] is True and r2["ok"] is True and r1["kind"] == "dm"
    # 夜间静默：班表 09:00-21:00 马尼拉 → 22:30 自动链拦、人工放行
    cfg_q = {**CFG, "inbox": {"work_schedule": {"enabled": True, "timezone": "Asia/Manila", "edge_jitter_min": 0,
                                                "default": {"start": "09:00", "end": "21:00"}}}}
    r = hb.enqueue_reply(ACC, chat, "auto at night", config=cfg_q, state=st, now=t_night + 2, origin="auto")
    assert r["ok"] is False and r["reason"] == hb.REASON_QUIET_HOURS
    r3 = hb.enqueue_reply(ACC, chat, "manual at night", config=cfg_q, state=st, now=t_night + 2, origin="manual")
    assert r3["ok"] is True
    # 日上限 3（账号时区切日）：第 4 条 429；过了马尼拉午夜（UTC 仍是同一天 16:00）→ 归零可发
    r = hb.enqueue_reply(ACC, chat, "4th", config=CFG, state=st, now=t_night + 3, origin="manual")
    assert r["ok"] is False and r["reason"] == f"{hb.REASON_DAILY_CAP}:3" and r["status"] == 429
    t_after_midnight = datetime(2027, 1, 11, 0, 10, tzinfo=manila).timestamp()
    st.touch_account(ACC, DEV, now=t_after_midnight)   # 心跳，免得判离线
    assert datetime.fromtimestamp(t_after_midnight, ZoneInfo("UTC")).day == 10       # UTC 视角仍是 10 号
    r = hb.enqueue_reply(ACC, chat, "new day", config=CFG, state=st, now=t_after_midnight, origin="manual")
    assert r["ok"] is True
    # 对方沉默 >72h：自动链停；人工只可补 1 条（本轮入站后已发过 → 也拦）
    t_silent = t_night - 30 + 73 * 3600
    st.touch_account(ACC, DEV, now=t_silent)
    r = hb.enqueue_reply(ACC, chat, "auto follow-up", config=CFG, state=st, now=t_silent, origin="auto")
    assert r["ok"] is False and r["reason"] == hb.REASON_PEER_SILENT
    r = hb.enqueue_reply(ACC, chat, "manual follow-up", config=CFG, state=st, now=t_silent, origin="manual")
    assert r["ok"] is False and r["reason"] == hb.REASON_PEER_SILENT      # 本轮已发过 4 条
    # 新会话：对方问过一次、我方没回、然后沉默 73h → 人工可补 1 条，第二条拦
    chat2 = "tiktok:user:buyer_2"
    await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [_dm("j1", "price?", peer="buyer_2", ts=t_silent - 74 * 3600)]},
                       config=CFG, state=st, now=t_silent, emit=lambda m: None, auto_reply=_noop, registry=reg)
    assert hb.enqueue_reply(ACC, chat2, "auto", config=CFG, state=st, now=t_silent, origin="auto")["reason"] == hb.REASON_PEER_SILENT
    assert hb.enqueue_reply(ACC, chat2, "sorry for the late reply!", config=CFG, state=st, now=t_silent, origin="manual")["ok"] is True
    assert hb.enqueue_reply(ACC, chat2, "again", config=CFG, state=st, now=t_silent, origin="manual")["reason"] == hb.REASON_PEER_SILENT
    # 真机离线 >2h 无心跳 → 503 device_offline
    await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [_dm("k1", "hey", peer="buyer_3", ts=t_silent + 10)]},
                       config=CFG, state=st, now=t_silent + 10, emit=lambda m: None, auto_reply=_noop, registry=reg)
    r = hb.enqueue_reply(ACC, "tiktok:user:buyer_3", "hi", config=CFG, state=st, now=t_silent + 10 + 3 * 3600, origin="manual")
    assert r["ok"] is False and r["reason"] == hb.REASON_DEVICE_OFFLINE and r["status"] == 503


async def test_dm_claim_pacing_ack_mirror_status_and_failure_trace(env):
    store, st = env
    reg = FakeRegistry()
    chat = "tiktok:user:buyer_1"
    hb.bind_device({"device_id": DEV, "account_id": ACC}, config=CFG, state=st, now=T0, registry=reg)
    await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [_dm("i1", ts=T0)]}, config=CFG, state=st, now=T0,
                       emit=lambda m: None, auto_reply=_noop, registry=reg)
    r1 = hb.enqueue_reply(ACC, chat, "first", config=CFG, state=st, now=T0 + 1)
    r2 = hb.enqueue_reply(ACC, chat, "second", config=CFG, state=st, now=T0 + 2)
    r3 = hb.enqueue_reply(ACC, chat, "third", config=CFG, state=st, now=T0 + 3)
    assert all(r["ok"] for r in (r1, r2, r3))
    # 认领节奏：同一账号一次只给 1 条私信；上一条送达后 min_gap 30s 内不给下一条
    items = st.claim(DEV, limit=10, now=T0 + 10, min_gap_sec=30)
    assert [i["id"] for i in items] == [r1["item_id"]] and items[0]["kind"] == "dm"
    assert st.claim(DEV, limit=10, now=T0 + 11, min_gap_sec=30) == []               # r1 仍 claimed，r2 等 r1 回执
    rep: List[tuple] = []
    status, res = hb.ack_handback({"item_id": r1["item_id"], "ok": True, "external_id": "tt-1", "device_id": DEV}, config=CFG,
                                  state=st, now=T0 + 20, emit=lambda m: pytest.fail("有镜像时不该另起回显"),
                                  status_reporter=lambda *a: rep.append(a) or True)
    assert status == 200 and res["status"] == "sent"
    assert rep == [("tiktok", ACC, chat, f"hb:{r1['item_id']}", "sent")]           # 入队镜像升 sent
    assert st.claim(DEV, limit=10, now=T0 + 25, min_gap_sec=30) == []               # 距上条送达 5s < 30s
    items = st.claim(DEV, limit=10, now=T0 + 51, min_gap_sec=30)
    assert [i["id"] for i in items] == [r2["item_id"]]
    # 失败 → 留痕 + 该条不再重试（huoke 已判定终局）；r3 继续
    class _Store:
        calls: List[tuple] = []

        def record_failed_outbound(self, conv, text, *, reason=""):
            self.calls.append((conv, text, reason))
            return "m-fail"
    fs = _Store()
    status, res = hb.ack_handback({"item_id": r2["item_id"], "ok": False, "error": "dm_disabled_by_peer"}, config=CFG, state=st,
                                  now=T0 + 60, store=fs)
    assert res["status"] == "failed" and fs.calls == [(f"tiktok:{ACC}:{chat}", "second", "huoke:dm_disabled_by_peer")]
    assert [i["id"] for i in st.claim(DEV, limit=10, now=T0 + 61, min_gap_sec=30)] == [r3["item_id"]]
    s = st.summary()
    assert (s["sent"], s["failed"], s["claimed"], s["pending_dm"]) == (1, 1, 1, 1)


def test_devices_bind_heartbeat_health_transitions_and_sweep(env):
    store, st = env
    reg, health = FakeRegistry(), FakeHealth()
    assert hb.bind_device({"device_id": DEV}, config=CFG, state=st, registry=reg)[0] == 400
    assert hb.bind_device({"device_id": DEV, "account_id": ACC, "timezone": "Mars/Olympus"}, config=CFG, state=st, registry=reg)[1]["error"].startswith("bad_timezone")
    status, res = hb.bind_device({"device_id": DEV, "account_id": ACC, "username": "myshop", "timezone": "Asia/Manila", "dm_daily_cap": 99},
                                 config=CFG, state=st, now=T0, registry=reg, health_sink=health)
    assert status == 200 and res["dm_daily_cap"] == 3 and res["health"] == "authorized" and res["timezone"] == "Asia/Manila"
    assert reg.rows[f"tiktok:{ACC}"]["mode"] == "personal_rpa"
    assert health.events == [("tiktok", ACC, "authorized", "")]
    # 心跳三态：15 分钟 → reconnecting；2 小时 → expired；再心跳 → authorized；同态不重报
    assert hb.heartbeat_state(T0, T0 + 60) == "authorized" and hb.heartbeat_state(T0, T0 + 16 * 60) == "reconnecting"
    assert hb.heartbeat_state(T0, T0 + 2 * 3600 + 1) == "expired" and hb.heartbeat_state(0, T0) == "unknown"
    assert hb.report_health(st, ACC, now=T0 + 60, health_sink=health) == "authorized" and len(health.events) == 1
    assert hb.report_health(st, ACC, now=T0 + 16 * 60, health_sink=health) == "reconnecting" and health.events[-1][2] == "reconnecting"
    assert hb.sweep_health(state=st, now=T0 + 3 * 3600, health_sink=health) == {ACC: "expired"}
    assert health.events[-1][2] == "expired" and "2 小时" in health.events[-1][3]
    assert st.heartbeat(DEV, now=T0 + 3 * 3600 + 5) == [ACC]
    assert hb.report_health(st, ACC, now=T0 + 3 * 3600 + 6, health_sink=health) == "authorized"
    assert [e[2] for e in health.events] == ["authorized", "reconnecting", "expired", "authorized"]
    assert hb.sweep_health(config={}, state=None) == {}    # 桥未开 → 不扫


# ═══ 路由 ═══════════════════════════════════════════════════════════════════════════════════

def test_routes_default_off_then_mounted_with_window_field_normalization(env, monkeypatch):
    store, st = env
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.integrations import tiktok_official as tk
    routes = (hb.LEADS_ROUTE, hb.DM_ROUTE, hb.DEVICES_ROUTE, hb.HANDBACK_ROUTE, hb.HANDBACK_ACK_ROUTE, hb.HANDBACK_PENDING_ROUTE,
              hb.STATUS_ROUTE)
    app0 = FastAPI()
    tk.register_tiktok_routes(app0, SimpleNamespace(config={}))
    assert not any(getattr(r_, "path", "") in routes for r_ in app0.routes)
    assert hb.bridge_cfg({})["enabled"] is False and hb.bridge_cfg({})["dm_daily_cap"] == hb.DEFAULT_DM_DAILY_CAP
    assert hb.bridge_cfg({"tiktok": {"huoke_bridge": {"max_reply_len": 999, "dm_max_len": 5000}}})["max_reply_len"] == hb.COMMENT_MAX_LEN
    assert hb.bridge_cfg({"tiktok": {"huoke_bridge": {"dm_max_len": 5000}}})["dm_max_len"] == hb.DM_MAX_LEN
    app = FastAPI()
    adapters: List[Any] = []
    monkeypatch.setattr("src.web.routes.unified_inbox_aggregate._INBOX_ADAPTERS", adapters)
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    tk.register_tiktok_routes(app, SimpleNamespace(config=CFG))
    paths = [getattr(r_, "path", "") for r_ in app.routes]
    assert all(paths.count(p) >= 1 for p in routes) and paths.count(hb.LEADS_ROUTE) == 1 and tk.DEFAULT_WEBHOOK_PATH not in paths
    assert len(adapters) == 1 and adapters[0].platform == "tiktok"
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    monkeypatch.setattr(pb, "maybe_auto_reply", _noop)
    reg = FakeRegistry()
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry", lambda: reg)
    c = TestClient(app)
    # 设备绑定 → 私信进线 → 认领 → 回执（带旧窗口字段名 → 归一）→ status
    r = c.post(hb.DEVICES_ROUTE, json={"device_id": DEV, "account_id": ACC, "timezone": "Asia/Manila"})
    assert r.status_code == 200 and r.json()["health"] == "authorized"
    r = c.post(hb.DM_ROUTE, json={"device_id": DEV, "account_id": ACC, "messages": [_dm("i1")]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert c.post(hb.DM_ROUTE, content=b"{bad").status_code == 400
    r = c.post(hb.LEADS_ROUTE, json={"device_id": DEV, "account_id": ACC, "leads": [_lead("c1"), _lead("c9", intent=0.1)]})
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["low_intent"] == 1
    ok = hb.enqueue_reply(ACC, "tiktok:user:buyer_1", "Yes! DM us", config=CFG, state=st)
    assert ok["ok"] is True
    # TK-3 轮询器只看不认领：pending 报 queued=1 且不改状态；认领后 queued=0/claimed=1；缺参 400
    r = c.get(hb.HANDBACK_PENDING_ROUTE, params={"device_id": DEV})
    assert r.status_code == 200 and r.json()["queued"] == 1 and r.json()["queued_dm"] == 1 and r.json()["claimed"] == 0
    assert st.item(ok["item_id"])["status"] == "queued"
    assert c.get(hb.HANDBACK_PENDING_ROUTE).status_code == 400
    r = c.get(hb.HANDBACK_ROUTE, params={"device_id": DEV})
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [ok["item_id"]]
    assert r.json()["policy"]["dm_daily_cap"] == 3 and r.json()["policy"]["min_gap_sec"] == 30
    r = c.get(hb.HANDBACK_PENDING_ROUTE, params={"account_id": ACC})
    assert r.status_code == 200 and r.json()["queued"] == 0 and r.json()["claimed"] == 1
    assert c.get(hb.HANDBACK_ROUTE).status_code == 400
    r = c.post(hb.HANDBACK_ACK_ROUTE, json={"item_id": ok["item_id"], "ok": True, "external_id": "rp-1",
                                            "window": {"reply_window_deadline": T0 + 100, "window_sent_count": 1, "cap": 20}})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert r.json()["window"] == {"deadline_ts": T0 + 100, "sent": 1, "cap": 20}
    assert st.account(ACC)["last_window"] == {"deadline_ts": T0 + 100, "sent": 1, "cap": 20}
    r = c.get(hb.DEVICES_ROUTE)
    assert r.status_code == 200 and r.json()["accounts"][0]["account_id"] == ACC and r.json()["accounts"][0]["sent_today_dm"] == 1
    r = c.get(hb.STATUS_ROUTE)
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["sent"] == 1 and r.json()["health"] == {ACC: "authorized"}
    assert r.json()["ops"]["light"] == "green" and r.json()["ops"]["sent_24h"] == 1 and r.json()["ops"]["accounts"]["authorized"] == 1
    assert getattr(app.state, "tiktok_huoke_sweeper_armed", False) is True   # E5 巡检随路由挂载（startup 钩子）


# ═══ TK-3 E5 运维卡 ══════════════════════════════════════════════════════════════════════════

def test_report_health_uses_session_transition_so_offline_reaches_ops(env, monkeypatch):
    """默认（无注入 sink）走 report_session_transition：健康表 + 注册表 offline + platform_session_alert 一次到位。"""
    store, st = env
    calls: List[tuple] = []
    monkeypatch.setattr("src.integrations.platform_session_health.report_session_transition",
                        lambda plat, acc, status, *, detail="", login_id="": calls.append((plat, acc, status, detail)) or {"changed": True})
    hb.bind_device({"device_id": DEV, "account_id": ACC}, config=CFG, state=st, now=T0, registry=FakeRegistry())
    assert calls == [("tiktok", ACC, "authorized", "")]
    assert hb.report_health(st, ACC, now=T0 + 16 * 60) == "reconnecting" and calls[-1][2] == "reconnecting"
    assert hb.report_health(st, ACC, now=T0 + 3 * 3600) == "expired" and "2 小时" in calls[-1][3]
    assert hb.report_health(st, ACC, now=T0 + 3 * 3600 + 1) == "expired" and len(calls) == 3   # 同态不重报
    st.heartbeat(DEV, now=T0 + 4 * 3600)
    assert hb.report_health(st, ACC, now=T0 + 4 * 3600 + 1) == "authorized" and calls[-1][2] == "authorized"


def test_ops_snapshot_and_backlog_alert_default_off(env):
    store, st = env
    reg = FakeRegistry()
    hb.bind_device({"device_id": DEV, "account_id": ACC, "timezone": "Asia/Manila"}, config=CFG, state=st, now=T0, registry=reg,
                   health_sink=FakeHealth())
    hb.bind_device({"device_id": "device-B", "account_id": "acc-b"}, config=CFG, state=st, now=T0, registry=reg, health_sink=FakeHealth())
    for i in range(6):
        st.record_inbound(ACC, f"tiktok:user:p{i}", username=f"p{i}", ts=T0, kind=hb.KIND_DM)
    ids = [st.enqueue(ACC, f"tiktok:user:p{i}", f"r{i}", ctx={"username": f"p{i}"}, now=T0 + i, kind=hb.KIND_DM) for i in range(6)]
    # 5 条回执：2 sent 3 failed（近 24h 失败率 60%）；第 6 条一直 queued（积压 40 分钟）
    for i, ok in enumerate([True, True, False, False, False]):
        st.ack(ids[i], ok=ok, error="" if ok else ("account_restricted" if i < 4 else "ui_gone"), now=T0 + 100 + i)
    now = T0 + 40 * 60
    st.heartbeat(DEV, now=now - 60)
    st.heartbeat("device-B", now=now - 60)   # 两台都在线 → 积压只能是「没人来认领」
    w = st.window_stats(since=now - 86400, until=now)
    assert (w["sent"], w["failed"], w["fail_rate"]) == (2, 3, 0.6) and w["top_errors"][0] == {"error": "account_restricted", "n": 2}
    snap = hb.ops_snapshot(config=CFG, state=st, now=now)
    keys = [p["key"] for p in snap["problems"]]
    assert snap["queued"] == 1 and snap["oldest_wait_sec"] >= 39 * 60 and snap["fail_rate_24h"] == 0.6
    assert keys == ["tiktok_huoke_backlog", "tiktok_huoke_fail_rate"] and snap["light"] == "yellow"
    assert "巡检没来认领" in snap["problems"][0]["detail"] and "account_restricted" in snap["problems"][1]["detail"]
    assert snap["accounts"] == {"total": 2, "authorized": 2, "reconnecting": 0, "expired": 0, "expired_ids": [], "reconnecting_ids": []}
    # 3 小时后两台都没心跳 → 离线 red，积压解释改成「真机离线」
    snap2 = hb.ops_snapshot(config=CFG, state=st, now=T0 + 3 * 3600)
    assert snap2["light"] == "red" and snap2["problems"][0]["key"] == "tiktok_huoke_offline"
    assert snap2["accounts"]["expired_ids"] == ["acc-b", ACC] and "真机离线" in snap2["problems"][1]["detail"]
    # 积压实时卡默认关：不发；开了 → 发一次 health_alert（只带积压/失败率，不带离线）→ 4h 内不重发 → 清了不发恢复
    published: List[tuple] = []
    pub = lambda name, payload: published.append((name, payload))
    hold: Dict[str, Any] = {}
    assert hb.maybe_alert_backlog(snap2, config=CFG, now=now, publish=pub, state_holder=hold) is False and published == []
    cfg_on = {"tiktok": {"huoke_bridge": {**CFG["tiktok"]["huoke_bridge"], "backlog_alert_min": 10}}}
    assert hb.maybe_alert_backlog(snap2, config=cfg_on, now=now, publish=pub, state_holder=hold) is True
    assert published[0][0] == "health_alert" and [p["key"] for p in published[0][1]["problems"]] == ["tiktok_huoke_backlog", "tiktok_huoke_fail_rate"]
    assert published[0][1]["light"] == "red" and published[0][1]["recovered"] is False and published[0][1]["rate_key"] == "tiktok_huoke:backlog"
    assert hb.maybe_alert_backlog(snap2, config=cfg_on, now=now + 3600, publish=pub, state_holder=hold) is False and len(published) == 1
    assert hb.maybe_alert_backlog(snap2, config=cfg_on, now=now + 5 * 3600, publish=pub, state_holder=hold) is True and len(published) == 2
    # 积压阈值高于快照黄线：40 分钟积压、alert_min=60 → 只有失败率单独成立
    cfg_hi = {"tiktok": {"huoke_bridge": {**CFG["tiktok"]["huoke_bridge"], "backlog_alert_min": 60}}}
    assert hb.maybe_alert_backlog(snap, config=cfg_hi, now=now, publish=pub, state_holder={}) is True
    assert [p["key"] for p in published[-1][1]["problems"]] == ["tiktok_huoke_fail_rate"]
    assert hb.maybe_alert_backlog({"problems": [], "oldest_wait_sec": 0}, config=cfg_on, now=now, publish=pub, state_holder=hold) is False
    assert hold["active"] is False and len(published) == 3
    # sweep_ops：桥未开 → {}；开 → 心跳过账 + 快照 + alerted 标
    assert hb.sweep_ops(config={}, state=None) == {}
    out = hb.sweep_ops(config=CFG, state=st, now=now, publish=pub)
    assert out["health"] == {ACC: "authorized", "acc-b": "authorized"} and out["alerted"] is False and out["light"] == "yellow"


def test_ensure_ops_sweeper_is_idempotent_and_hooks_startup():
    from fastapi import FastAPI
    app = FastAPI()
    assert hb.ensure_ops_sweeper(app, lambda: CFG, interval_sec=5) is True
    assert hb.ensure_ops_sweeper(app, lambda: CFG) is False   # 已挂
    assert app.state.tiktok_huoke_sweeper_armed is True
    assert any(getattr(h, "__name__", "") == "_start" for h in app.router.on_startup)   # 未启动 → startup 钩子
    assert hb.bridge_cfg({})["ops_sweep_sec"] == hb.DEFAULT_OPS_SWEEP_SEC and hb.bridge_cfg({})["backlog_alert_min"] == 0.0
    assert hb.bridge_cfg({"tiktok": {"huoke_bridge": {"ops_sweep_sec": 5}}})["ops_sweep_sec"] == 30.0


# ═══ e2e（conftest 完整 app）═══════════════════════════════════════════════════════════════

async def test_e2e_comment_manual_send_falls_back_to_handback_adapter(auth_client, app, tmp_path, monkeypatch):
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
    r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                        "text": "Yes we ship to Manila", "skip_translate": True})
    assert r.status_code == 400, r.text
    added = hb.install_adapter(agg._INBOX_ADAPTERS, lambda: CFG)
    try:
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "Yes we ship to Manila", "skip_translate": True})
        assert r.status_code == 200, r.text
        items = st.claim(DEV, limit=10, now=T0 + 5)
        assert len(items) == 1 and items[0]["text"] == "Yes we ship to Manila" and items[0]["comment_id"] == "c1"
        assert items[0]["origin"] == "manual"
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "and Cebu", "skip_translate": True})
        assert r.status_code == 409 and hb.REASON_ONE_REPLY in r.text, r.text
        # 入队镜像已在线程（无勾 = 待真机）；被拦的两次发送各留一条 failed 痕（既有行为，原因随行）；回执后镜像升 sent
        r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
        all_outs = [m for m in r.json().get("messages") or [] if m.get("direction") == "out"]
        assert [str(m.get("fail_reason") or "") for m in all_outs if m.get("status") == "failed"][-1] == hb.REASON_ONE_REPLY
        outs = [m for m in all_outs if m.get("status") != "failed"]
        assert len(outs) == 1 and "Manila" in str(outs[-1].get("text")) and str(outs[-1].get("status") or "") == ""
        pb.register_inbox_store_getter(lambda: store)
        try:
            status, res = hb.ack_handback({"item_id": items[0]["id"], "ok": True, "external_id": "tt-reply-1"}, config=CFG, state=st)
        finally:
            pb.register_inbox_store_getter(None)
        assert status == 200 and res["status"] == "sent"
        r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
        outs = [m for m in r.json().get("messages") or [] if m.get("direction") == "out" and m.get("status") != "failed"]
        assert len(outs) == 1 and outs[-1].get("status") == "sent"
    finally:
        if added:
            agg._INBOX_ADAPTERS[:] = [a for a in agg._INBOX_ADAPTERS if not getattr(a, "_tiktok_huoke_bridge", False)]


async def test_e2e_dm_manual_send_queue_ack_and_failure_trace(auth_client, app, tmp_path, monkeypatch):
    from src.web.routes import unified_inbox_aggregate as agg
    store = InboxStore(tmp_path / "inbox_huoke_dm_e2e.db")
    app.state.inbox_store = store
    st = hb.TikTokHuokeStateStore(":memory:")
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    reg = FakeRegistry()
    chat = "tiktok:user:buyer_1"
    hb.bind_device({"device_id": DEV, "account_id": ACC, "timezone": "Asia/Manila"}, config=CFG, state=st, now=T0, registry=reg)
    status, res = await hb.ingest_dm({"device_id": DEV, "account_id": ACC, "messages": [
        _dm("o1", "Hi there!", direction="out", ts=T0 - 100), _dm("i1", "how much for the blue one?", ts=T0, mutual=False, follower=True)]},
        config=CFG, state=st, now=T0, emit=lambda m: pb.ingest_incoming(store, **m), auto_reply=_noop, registry=reg)
    assert status == 200 and res["accepted"] == 1 and res["echo"] == 1
    r = auth_client.get("/api/unified-inbox/chats", params={"platform": "tiktok", "limit": 30})
    mine = [c for c in (r.json().get("chats") or []) if c.get("chat_key") == chat]
    assert mine and mine[0]["account_id"] == ACC
    r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
    msgs = r.json().get("messages") or []
    assert [m.get("direction") for m in msgs if "blue" in str(m.get("text")) or "Hi there" in str(m.get("text"))] == ["out", "in"]
    added = hb.install_adapter(agg._INBOX_ADAPTERS, lambda: CFG)
    pb.register_inbox_store_getter(lambda: store)
    try:
        # 人工发送 → 队列（origin=manual）+ 入队镜像
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "350 pesos, free shipping!", "skip_translate": True})
        assert r.status_code == 200, r.text
        items = st.claim(DEV, limit=10, now=T0 + 5)
        assert len(items) == 1 and items[0]["kind"] == "dm" and items[0]["origin"] == "manual" and items[0]["username"] == "buyer_1"
        # 真机失败 → 失败留痕行（status=failed + 原因）出现在线程
        status, res = hb.ack_handback({"item_id": items[0]["id"], "ok": False, "error": "account_restricted"}, config=CFG, state=st)
        assert status == 200 and res["status"] == "failed"
        r = auth_client.get("/api/unified-inbox/thread", params={"platform": "tiktok", "account_id": ACC, "chat_key": chat})
        failed = [m for m in r.json().get("messages") or [] if m.get("status") == "failed"]
        assert failed and "350 pesos" in str(failed[-1].get("text")) and "account_restricted" in str(failed[-1].get("fail_reason") or "")
        # 真机离线 >2h → 人工发送 503 device_offline（reason_code 透出）
        monkeypatch.setattr(hb.time, "time", lambda: T0 + 3 * 3600)
        r = auth_client.post("/api/unified-inbox/send", json={"platform": "tiktok", "account_id": ACC, "chat_key": chat,
                                                            "text": "are you there", "skip_translate": True})
        assert r.status_code == 503 and hb.REASON_DEVICE_OFFLINE in r.text, r.text
    finally:
        pb.register_inbox_store_getter(None)
        if added:
            agg._INBOX_ADAPTERS[:] = [a for a in agg._INBOX_ADAPTERS if not getattr(a, "_tiktok_huoke_bridge", False)]
