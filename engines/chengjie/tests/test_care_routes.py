"""Phase O4：/api/care/schedule* 路由契约 + 入站捕获回调。

覆盖：手动加→列表/计数→到期预览→取消；非法入参软失败；
make_care_inbound_cb 在 enabled/capture 开关下的 gated 行为。
"""
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.care_schedule import CareScheduleStore
from src.web.routes.care_routes import register_care_routes


def _client():
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_care_routes(app, api_auth=_auth, config_manager=None)
    app.state.care_schedule_store = CareScheduleStore(":memory:")
    return TestClient(app), app.state.care_schedule_store


def test_manual_add_then_list_and_summary():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={
        "contact_key": "c1", "platform": "messenger", "chat_key": "fb:1",
        "topic": "面试", "due_in_hours": 48, "source_text": "周五面试",
    })
    assert r.json()["ok"] is True
    rid = r.json()["id"]

    r2 = client.get("/api/care/schedule")
    body = r2.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["items"][0]["id"] == rid
    assert body["summary"]["pending"] == 1


def test_manual_add_requires_fields():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={"contact_key": "c1"})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "missing"


def test_manual_add_rejects_past_due():
    client, _ = _client()
    r = client.post("/api/care/schedule", json={
        "contact_key": "c1", "topic": "x", "due_at": time.time() - 100,
    })
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "due_in_past"


def test_due_preview_and_cancel():
    client, store = _client()
    # 直接塞一条已到期 pending
    from src.contacts.care_commitment import CareCommitment
    c = CareCommitment(due_at=time.time() - 10, event_at=time.time() - 10,
                       topic="复查", sentiment="neutral", anchor_text="x",
                       source_text="s", confidence=1.0)
    sid = store.add_commitment(c, contact_key="c2", platform="messenger",
                               chat_key="fb:2", min_confidence=0.0, dedup_window_days=0.0)
    assert sid

    due = client.get("/api/care/schedule/due").json()
    assert due["count"] == 1 and due["items"][0]["id"] == sid

    cancel = client.post(f"/api/care/schedule/{sid}/cancel", json={"note": "no"})
    assert cancel.json()["ok"] is True
    assert client.get("/api/care/schedule/due").json()["count"] == 0
    assert client.get("/api/care/schedule").json()["summary"]["cancelled"] == 1


def test_cancel_unknown_returns_not_pending():
    client, _ = _client()
    r = client.post("/api/care/schedule/9999/cancel", json={})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "not_pending"


def test_send_now_brings_due_forward():
    client, store = _client()
    # 一条 48h 后到期的 pending → send-now 后立刻进 due 列表
    r = client.post("/api/care/schedule", json={
        "contact_key": "c3", "topic": "体检", "due_in_hours": 48,
        "platform": "messenger", "chat_key": "fb:3",
    })
    sid = r.json()["id"]
    assert client.get("/api/care/schedule/due").json()["count"] == 0
    sn = client.post(f"/api/care/schedule/{sid}/send-now", json={})
    assert sn.json()["ok"] is True
    assert client.get("/api/care/schedule/due").json()["count"] == 1


def test_send_now_unknown_returns_not_pending():
    client, _ = _client()
    r = client.post("/api/care/schedule/9999/send-now", json={})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "not_pending"


# ── Phase O 质量闭环：dry_run 样本审核端点 ──────────────────────────────
def test_care_dry_samples_empty_then_populated():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._care_dry_samples.clear()
    client, _ = _client()
    r = client.get("/api/care/dry-run-samples")
    assert r.json()["ok"] is True and r.json()["count"] == 0

    ms.record_care_dry_run(sample={
        "care_id": 1, "topic": "面试", "platform": "telegram",
        "reply_text": "你之前说的面试怎么样啦？",
    })
    r2 = client.get("/api/care/dry-run-samples").json()
    assert r2["count"] == 1 and r2["samples"][0]["topic"] == "面试"


def test_care_dry_feedback_dislike_adds_blacklist():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._care_dry_samples.clear()
    ms._reactivation_disliked_replies.clear()
    client, _ = _client()
    bad = "你之前说的体检结果出来了吗？"
    ms.record_care_dry_run(sample={"care_id": 9, "topic": "体检", "reply_text": bad})
    ts = ms.care_dry_samples(limit=1)[0]["ts"]

    r = client.post("/api/care/dry-run-feedback",
                    json={"verdict": "dislike", "sample_ts": ts})
    assert r.json()["ok"] is True
    is_sim, _ = ms.is_similar_to_disliked(bad, threshold=0.7)
    assert is_sim is True  # 已进共享黑名单


def test_care_dry_feedback_bad_verdict():
    client, _ = _client()
    r = client.post("/api/care/dry-run-feedback", json={"verdict": "meh"})
    assert r.json()["ok"] is False and r.json()["reason"] == "bad_verdict"


# ── 入站捕获回调 ────────────────────────────────────────────────────────

class _CM:
    def __init__(self, cfg):
        self.config = cfg


def test_capture_cb_gated_off_by_default():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cb = make_care_inbound_cb(store, _CM({}))  # 无 proactive_care 配置 → 关
    cb({"conversation_id": "c1", "platform": "messenger", "chat_key": "fb:1"}, "周五面试")
    assert store.count() == 0


def test_capture_cb_enabled_captures():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True, "capture": True}}})
    cb = make_care_inbound_cb(store, cm)
    cb({"conversation_id": "c1", "platform": "messenger", "chat_key": "fb:1"}, "我下周三要面试")
    assert store.count(status="pending") == 1


def test_capture_cb_capture_flag_off():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True, "capture": False}}})
    cb = make_care_inbound_cb(store, cm)
    cb({"conversation_id": "c1", "platform": "messenger"}, "我下周三要面试")
    assert store.count() == 0


def test_capture_cb_never_raises_on_bad_input():
    from src.contacts.care_capture import make_care_inbound_cb
    store = CareScheduleStore(":memory:")
    cm = _CM({"companion": {"proactive_care": {"enabled": True}}})
    cb = make_care_inbound_cb(store, cm)
    cb({}, "")          # 空 conv + 空文本
    cb({"conversation_id": ""}, "周五面试")  # 空 contact_key
    assert store.count() == 0


# ── P0 2026-08-03 历史可读性：名字 join / 最近在前 / 投递真相 / 回复徽标 ────
import json as _json


class _FakeInbox:
    def __init__(self, names=None, msgs=None):
        self._names = dict(names or {})
        self._msgs = dict(msgs or {})

    def get_conversations_for_ids(self, ids):
        return {k: {"display_name": v} for k, v in self._names.items() if k in ids}

    def list_recent_messages(self, cid, limit=100):
        return list(self._msgs.get(cid) or [])


class _FakeDeferred:
    def __init__(self, rows):
        self._rows = {int(r["id"]): dict(r) for r in rows}

    def get_by_ids(self, ids):
        return {int(i): dict(self._rows[int(i)]) for i in ids if int(i) in self._rows}


def _seed_row(store, *, contact="c1", topic="面试", due_at=None):
    from src.contacts.care_commitment import CareCommitment
    due = float(due_at if due_at is not None else time.time() + 3600)
    c = CareCommitment(due_at=due, event_at=due, topic=topic, sentiment="neutral",
                       anchor_text="x", source_text="原话", confidence=1.0)
    return store.add_commitment(c, contact_key=contact, platform="telegram",
                                chat_key="123", min_confidence=0.0,
                                dedup_window_days=0.0)


def _force_sent(store, sid, *, note, sent_at):
    """mark_sent 后把 sent_at/updated_at 钉到指定时刻（构造历史序）。"""
    store.mark_sent(sid, note=note)
    store._conn.execute(
        "UPDATE care_schedule SET sent_at=?, updated_at=? WHERE id=?",
        (float(sent_at), float(sent_at), int(sid)))
    store._conn.commit()


def test_store_list_history_recent_first():
    store = CareScheduleStore(":memory:")
    now = time.time()
    a = _seed_row(store, contact="h1", topic="面试", due_at=now - 7200)
    b = _seed_row(store, contact="h2", topic="复查", due_at=now - 3600)
    _force_sent(store, a, note="deferred:1", sent_at=now - 5000)
    _force_sent(store, b, note="deferred:2", sent_at=now - 100)
    # 历史视图：最近发送在前；对照 list_recent（待办语义）仍按 due 升序
    assert [r["id"] for r in store.list_history(status="sent", limit=10)] == [b, a]
    assert [r["id"] for r in store.list_recent(status="sent", limit=10)] == [a, b]


def test_history_order_and_pending_stays_due_asc():
    client, store = _client()
    now = time.time()
    a = _seed_row(store, contact="cA", topic="面试", due_at=now - 7200)
    b = _seed_row(store, contact="cB", topic="复查", due_at=now - 3600)
    _force_sent(store, a, note="deferred:11", sent_at=now - 5000)
    _force_sent(store, b, note="deferred:12", sent_at=now - 100)
    body = client.get("/api/care/schedule?status=sent").json()
    assert body["order"] == "recent_desc"
    assert [it["id"] for it in body["items"]] == [b, a]

    c = _seed_row(store, contact="cC", topic="生日", due_at=now + 7200)
    d = _seed_row(store, contact="cD", topic="旅行", due_at=now + 3600)
    pend = client.get("/api/care/schedule?status=pending").json()
    assert pend["order"] == "due_asc"
    assert [it["id"] for it in pend["items"]] == [d, c]


def test_history_display_name_join():
    client, store = _client()
    sid = _seed_row(store, contact="telegram:1:2", topic="面试")
    client.app.state.inbox_store = _FakeInbox(names={"telegram:1:2": "Bao 室"})
    body = client.get("/api/care/schedule").json()
    row = [it for it in body["items"] if it["id"] == sid][0]
    assert row["display_name"] == "Bao 室"


def test_history_delivery_truth_and_sent_text_real_store():
    """端到端走真实 DeferredOutboxStore：note=deferred:<id> 反查投递真相+话术。"""
    from src.integrations.shared.deferred_outbox import DeferredOutboxStore
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="cX", topic="面试", due_at=now - 10)
    dstore = DeferredOutboxStore(":memory:")
    did = dstore.enqueue(platform="telegram", account_id="a", chat_key="123",
                         reply_text="你之前说的面试怎么样啦？", defer_until=now,
                         extra={"care": True, "care_id": sid})
    assert did > 0
    dstore.mark_failed(did, "network boom")
    _force_sent(store, sid, note=f"deferred:{did}", sent_at=now - 200)
    client.app.state.deferred_outbox_store = dstore
    row = client.get("/api/care/schedule?status=sent").json()["items"][0]
    assert row["delivery"]["status"] == "failed"
    assert "boom" in row["delivery"]["error"]
    assert row["sent_text"] == "你之前说的面试怎么样啦？"
    # get_by_ids 契约：缺失 id 缺席、命中 id 全字段
    got = dstore.get_by_ids([did, 99999])
    assert set(got) == {did} and got[did]["reply_text"]


def test_history_text_denied_on_fingerprint_mismatch():
    """队列行 care 指纹对不上 → 宁可不显示（防串库挂错话术）。"""
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="cY", topic="复查", due_at=now - 10)
    _force_sent(store, sid, note="deferred:88", sent_at=now - 200)
    client.app.state.deferred_outbox_store = _FakeDeferred([{
        "id": 88, "status": "sent", "sent_at": now - 150, "error": "",
        "reply_text": "别人的队列文本",
        "extra": _json.dumps({"care": True, "care_id": 999999}),
    }])
    row = client.get("/api/care/schedule?status=sent").json()["items"][0]
    assert not row.get("sent_text") and "delivery" not in row


def test_history_reply_state_badges():
    client, store = _client()
    now = time.time()
    s1 = _seed_row(store, contact="r1", topic="面试", due_at=now - 10)
    _force_sent(store, s1, note="deferred:1", sent_at=now - 3600)      # 窗内已回
    s2 = _seed_row(store, contact="r2", topic="复查", due_at=now - 10)
    _force_sent(store, s2, note="deferred:2", sent_at=now - 60 * 3600)  # 窗口已关没回
    s3 = _seed_row(store, contact="r3", topic="体检", due_at=now - 10)
    _force_sent(store, s3, note="deferred:3", sent_at=now - 600)        # 刚发没回
    client.app.state.inbox_store = _FakeInbox(msgs={
        "r1": [{"direction": "in", "ts": now - 3000}],
    })
    items = {it["id"]: it
             for it in client.get("/api/care/schedule?status=sent").json()["items"]}
    assert items[s1]["reply_state"] == "replied"
    assert items[s2]["reply_state"] == "no_reply"
    assert items[s3]["reply_state"] == "window_open"


def test_history_dry_run_rows_have_no_enrichment():
    """dry_run 行没真发过：不给投递/话术/回复字段（前端自动回落旧渲染）。"""
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="d1", topic="面试", due_at=now - 10)
    _force_sent(store, sid, note="dry_run", sent_at=now - 100)
    client.app.state.inbox_store = _FakeInbox()
    client.app.state.deferred_outbox_store = _FakeDeferred([])
    row = client.get("/api/care/schedule?status=sent").json()["items"][0]
    assert ("reply_state" not in row and "delivery" not in row
            and not row.get("sent_text"))


# ── P2 2026-08-03：话术快照留档 / 联系人筛选 / 最近动态并存 ────────────────
def test_store_migration_adds_sent_text_column(tmp_path):
    """老库（无 sent_text 列）→ 打开即幂等补列；快照写入 + 2000 字截断。"""
    import sqlite3 as _sq
    db = tmp_path / "care_old.db"
    conn = _sq.connect(str(db))
    conn.executescript(
        "CREATE TABLE care_schedule ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, contact_key TEXT NOT NULL,"
        " platform TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '',"
        " chat_key TEXT NOT NULL DEFAULT '', due_at REAL NOT NULL,"
        " event_at REAL NOT NULL DEFAULT 0, topic TEXT NOT NULL DEFAULT '',"
        " topic_norm TEXT NOT NULL DEFAULT '', sentiment TEXT NOT NULL DEFAULT 'neutral',"
        " source_text TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, sent_at REAL, note TEXT NOT NULL DEFAULT '');")
    conn.execute(
        "INSERT INTO care_schedule (contact_key, due_at, topic, topic_norm, status,"
        " created_at, updated_at) VALUES ('legacy', 1, '面试', '面试', 'pending', 1, 1)")
    conn.commit()
    conn.close()
    store = CareScheduleStore(db)
    rows = store.list_recent()
    assert rows and rows[0]["contact_key"] == "legacy" and rows[0]["sent_text"] == ""
    assert store.mark_sent(rows[0]["id"], note="deferred:1", sent_text="x" * 3000)
    assert len(store.list_recent(status="sent")[0]["sent_text"]) == 2000
    store.close()


def test_history_contact_key_filter():
    client, store = _client()
    now = time.time()
    _seed_row(store, contact="ka", topic="面试", due_at=now + 100)
    _seed_row(store, contact="kb", topic="复查", due_at=now + 200)
    body = client.get("/api/care/schedule?contact_key=ka").json()
    assert body["order"] == "recent_desc"
    assert [it["contact_key"] for it in body["items"]] == ["ka"]


def test_history_snapshot_text_survives_queue_purge():
    """deferred 队列按保留期清理后：话术仍可读（快照），投递明细如实缺席。"""
    from src.integrations.shared.deferred_outbox import DeferredOutboxStore
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="pg", topic="面试", due_at=now - 10)
    dstore = DeferredOutboxStore(":memory:")
    did = dstore.enqueue(platform="telegram", account_id="a", chat_key="1",
                         reply_text="快照话术", defer_until=now - 8 * 86400,
                         extra={"care": True, "care_id": sid}, now=now - 8 * 86400)
    dstore.mark_sent(did, now=now - 8 * 86400)
    store.mark_sent(sid, note=f"deferred:{did}", sent_text="快照话术")
    assert dstore.purge_terminal(older_than_sec=7 * 86400.0, now=now) == 1
    client.app.state.deferred_outbox_store = dstore
    row = client.get("/api/care/schedule?status=sent").json()["items"][0]
    assert row["sent_text"] == "快照话术"
    assert "delivery" not in row


def test_plan_recent_coexists_dry_and_sent():
    """拟稿与已发并存：dry 模式下真发历史不消失（旧版二选一的修正）。"""
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._care_dry_samples.clear()
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="mx1", topic="面试", due_at=now - 10)
    assert store.mark_sent(sid, note="deferred:70", sent_text="真发话术")
    ms.record_care_dry_run(sample={"care_id": 99, "topic": "复查",
                                   "reply_text": "拟稿话术", "contact_key": "mx2"})
    client.app.state.config_manager = _CM({
        "companion": {"proactive_care": {"enabled": True, "dry_run": True}}})
    body = client.get("/api/care/plan").json()
    assert body["recent"]["mode"] == "dry"
    kinds = {r["kind"] for r in body["recent"]["items"]}
    assert kinds == {"dry", "sent"}
    sent_row = [r for r in body["recent"]["items"] if r["kind"] == "sent"][0]
    assert sent_row["sent_text"] == "真发话术"
    ms._care_dry_samples.clear()  # 不给后续用例留残样


# ── P3 2026-08-03：审核持久化闭环 + 主题分桶效果 ────────────────────────
def test_feedback_by_care_id_persists_review_and_recounts():
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="rv1", topic="面试", due_at=now - 10)
    store.mark_sent(sid, note="dry_run", sent_text="拟稿：面试顺利吗？")
    r = client.post("/api/care/dry-run-feedback",
                    json={"care_id": sid, "verdict": "like"}).json()
    assert r["ok"] is True and r["persisted"] is True
    st = store.review_stats()
    assert st["reviewed"] == 1 and st["like"] == 1
    # 改判：允许覆盖；持久口径重算，不产生双计
    r2 = client.post("/api/care/dry-run-feedback",
                     json={"care_id": sid, "verdict": "dislike"}).json()
    assert r2["ok"] is True
    st2 = store.review_stats()
    assert st2["reviewed"] == 1 and st2["dislike"] == 1 and st2["like"] == 0


def test_feedback_unknown_care_id_errors():
    client, _ = _client()
    r = client.post("/api/care/dry-run-feedback",
                    json={"care_id": 424242, "verdict": "like"}).json()
    assert r["ok"] is False and r["reason"] == "not_found"


def test_feedback_dislike_by_care_id_blacklists_snapshot():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="rv2", topic="复查", due_at=now - 10)
    bad = "这句拟稿千万别再用了编号" + str(sid)
    store.mark_sent(sid, note="dry_run", sent_text=bad)
    client.post("/api/care/dry-run-feedback",
                json={"care_id": sid, "verdict": "dislike"})
    is_sim, _ = ms.is_similar_to_disliked(bad, threshold=0.7)
    assert is_sim is True


def test_plan_dry_queue_is_durable_store_backed():
    """待审队列来自持久行：进程内样本清零（如重启后）队列仍在；审过即出队。"""
    from src.monitoring.metrics_store import get_metrics_store
    get_metrics_store()._care_dry_samples.clear()  # 模拟重启后 metrics 样本为空
    client, store = _client()
    now = time.time()
    s1 = _seed_row(store, contact="dq1", topic="面试", due_at=now - 10)
    store.mark_sent(s1, note="dry_run", sent_text="拟稿一")
    s2 = _seed_row(store, contact="dq2", topic="复查", due_at=now - 10)
    store.mark_sent(s2, note="dry_run", sent_text="拟稿二")
    client.app.state.config_manager = _CM({
        "companion": {"proactive_care": {"enabled": True, "dry_run": True}}})
    d = client.get("/api/care/plan").json()
    dry = [r for r in d["recent"]["items"] if r["kind"] == "dry"]
    assert {r["id"] for r in dry} == {s1, s2}
    assert all(r["text"] for r in dry)
    assert d["digest"]["samples"]["source"] == "store"
    # 审一条 → 队列出队一条、进度持久（重启不丢由 review 列保证）
    client.post("/api/care/dry-run-feedback", json={"care_id": s1, "verdict": "like"})
    d2 = client.get("/api/care/plan").json()
    dry2 = [r for r in d2["recent"]["items"] if r["kind"] == "dry"]
    assert {r["id"] for r in dry2} == {s2}
    assert d2["digest"]["samples"]["reviewed"] == 1
    assert d2["digest"]["samples"]["like_rate_pct"] == 100.0


def test_effect_by_topic_buckets():
    client, store = _client()
    now = time.time()
    a = _seed_row(store, contact="t1", topic="面试", due_at=now - 10)
    _force_sent(store, a, note="deferred:301", sent_at=now - 60 * 3600)   # 窗关已回
    b = _seed_row(store, contact="t2", topic="面试", due_at=now - 10)
    _force_sent(store, b, note="deferred:302", sent_at=now - 60 * 3600)   # 窗关未回
    c = _seed_row(store, contact="t3", topic="复查", due_at=now - 10)
    _force_sent(store, c, note="deferred:303", sent_at=now - 600)         # 窗未满
    client.app.state.inbox_store = _FakeInbox(msgs={
        "t1": [{"direction": "in", "ts": now - 59 * 3600}],
    })
    client.app.state.config_manager = _CM({
        "companion": {"proactive_care": {"enabled": True, "dry_run": False}}})
    eff = client.get("/api/care/plan").json()["digest"]["effect"]
    bt = {x["topic"]: x for x in eff.get("by_topic") or []}
    assert bt["面试"]["n"] == 2 and bt["面试"]["replied"] == 1
    assert "复查" not in bt  # 窗未满不进分母，与总回复率同口径


def test_plan_recent_live_includes_sent_text_delivery_and_reply():
    """/api/care/plan 的「最近已发」内联话术+投递真相+回复徽标（live 模式）。"""
    client, store = _client()
    now = time.time()
    sid = _seed_row(store, contact="p1", topic="面试", due_at=now - 10)
    _force_sent(store, sid, note="deferred:55", sent_at=now - 300)
    client.app.state.config_manager = _CM({
        "companion": {"proactive_care": {"enabled": True, "dry_run": False}}})
    client.app.state.deferred_outbox_store = _FakeDeferred([{
        "id": 55, "status": "sent", "sent_at": now - 250, "error": "",
        "reply_text": "面试顺利吗？一直惦记着",
        "extra": _json.dumps({"care": True, "care_id": sid}),
    }])
    client.app.state.inbox_store = _FakeInbox(msgs={
        "p1": [{"direction": "in", "ts": now - 100}]})
    body = client.get("/api/care/plan").json()
    assert body["ok"] is True and body["recent"]["mode"] == "live"
    row = [r for r in body["recent"]["items"] if r["id"] == sid][0]
    assert row["sent_text"] == "面试顺利吗？一直惦记着"
    assert row["delivery"]["status"] == "sent"
    assert row["reply_state"] == "replied"
