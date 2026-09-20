"""P2：预览端点（与派发同 prompt 口径）+ 健康端点 effect/shadow 段。

仿 test_care_routes.py 模式：裸 FastAPI() + register_care_routes + TestClient。
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import build_care_prompt
from src.contacts.care_schedule import CRISIS_CARE_TOPIC, CareScheduleStore
from src.web.routes.care_routes import register_care_routes


def _auth():
    # ⚠ 不能写 *a/**k：FastAPI 会把依赖签名里的 *args/**kwargs 解析成必填
    # query 参数（a=.../k=...）→ 全路由 422。零参依赖才是「恒放行」。
    return True


def _mk(store=None):
    app = FastAPI()
    register_care_routes(app, api_auth=_auth, config_manager=None)
    app.state.care_schedule_store = store or CareScheduleStore(":memory:")
    return TestClient(app), app


def _add_pending(store, *, topic="面试", contact="telegram:a:1", due_in=3600.0):
    now = time.time()
    rid = store.add_commitment(
        CareCommitment(due_at=now + due_in, event_at=now + due_in, topic=topic,
                       sentiment="neutral", anchor_text="manual",
                       source_text="明天面试", confidence=1.0),
        contact_key=contact, platform="telegram", account_id="a", chat_key="1",
        min_confidence=0.0, dedup_window_days=0.0)
    assert rid
    return rid


class _FakeAI:
    def __init__(self, reply="面试怎么样啦？一直惦记着呢"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        return self.reply


class _FakeInbox:
    def __init__(self, msgs=None):
        self._msgs = msgs or {}

    def list_recent_messages(self, cid, *, limit=100, before_ts=None):
        return list(self._msgs.get(cid, []))[:limit]


# ── build_care_prompt 公共化：预览=派发同口径 ────────────────────────────
def test_build_care_prompt_normal_and_crisis():
    item = {"topic": "面试", "event_at": time.time(), "source_text": "明天终面"}
    p = build_care_prompt(item, context_block="聊过的要点", ai_name="小玉", lang="zh")
    assert "面试" in p and "明天终面" in p and "聊过的要点" in p and "小玉" in p
    pc = build_care_prompt({"topic": CRISIS_CARE_TOPIC}, ai_name="小玉")
    assert "不用急着回我" in pc or "陪" in pc
    assert "明天终面" not in pc      # 危机专线绝不引用具体事


# ── 预览端点 ─────────────────────────────────────────────────────────────
def test_preview_ok_uses_recent_context():
    store = CareScheduleStore(":memory:")
    rid = _add_pending(store)
    client, app = _mk(store)
    ai = _FakeAI()
    app.state.ai_client = ai
    app.state.inbox_store = _FakeInbox({
        "telegram:a:1": [{"text": "我明天下午终面", "ts": time.time(), "direction": "in"}],
    })
    r = client.post(f"/api/care/schedule/{rid}/preview").json()
    assert r["ok"] is True and r["preview"].startswith("面试怎么样")
    # prompt 里带上了最近上下文（预览=派发同口径的核心断言）
    assert "我明天下午终面" in ai.prompts[0]


def test_preview_not_pending_and_ai_missing():
    store = CareScheduleStore(":memory:")
    client, app = _mk(store)
    r = client.post("/api/care/schedule/999/preview").json()
    assert r["ok"] is False and r["reason"] == "not_pending"
    rid = _add_pending(store)
    r2 = client.post(f"/api/care/schedule/{rid}/preview").json()
    assert r2["ok"] is False and r2["reason"] == "ai_missing"


def test_preview_does_not_mutate_status():
    store = CareScheduleStore(":memory:")
    rid = _add_pending(store)
    client, app = _mk(store)
    app.state.ai_client = _FakeAI()
    client.post(f"/api/care/schedule/{rid}/preview")
    assert store.get(rid)["status"] == "pending"


def test_preview_llm_empty():
    store = CareScheduleStore(":memory:")
    rid = _add_pending(store)
    client, app = _mk(store)
    app.state.ai_client = _FakeAI(reply="")
    r = client.post(f"/api/care/schedule/{rid}/preview").json()
    assert r["ok"] is False and r["reason"] == "llm_empty"


# ── store.get ────────────────────────────────────────────────────────────
def test_store_get():
    store = CareScheduleStore(":memory:")
    rid = _add_pending(store, topic="复查")
    row = store.get(rid)
    assert row["id"] == rid and row["topic"] == "复查"
    assert store.get(12345) is None


# ── 健康端点 effect 段（48h 回复率） ─────────────────────────────────────
def test_health_effect_rate():
    store = CareScheduleStore(":memory:")
    now = time.time()
    # 两条真发（note=deferred:*）：一条 60h 前发且 3h 后有入站=已回；
    # 一条 60h 前发、无入站=未回；一条 dry_run 不计；一条 1h 前发未回=窗口未满。
    ids = [_add_pending(store, contact=f"c{i}", due_in=10 + i) for i in range(4)]
    for i, rid in enumerate(ids):
        note = "dry_run" if i == 2 else "deferred:9"
        assert store.mark_sent(rid, note=note)
    old = now - 60 * 3600.0
    with store._lock:  # noqa: SLF001（测试内直接铺 sent_at 时间线）
        for i, rid in enumerate(ids):
            ts = (now - 3600.0) if i == 3 else old
            store._conn.execute(
                "UPDATE care_schedule SET sent_at=? WHERE id=?", (ts, rid))
        store._conn.commit()
    client, app = _mk(store)
    app.state.inbox_store = _FakeInbox({
        "c0": [{"text": "回你啦", "ts": old + 3 * 3600.0, "direction": "in"}],
        "c1": [],
        "c3": [],
    })
    eff = client.get("/api/care/health").json()["effect"]
    assert eff["sent_7d"] == 3          # dry_run 那条不算
    assert eff["matured"] == 2 and eff["replied"] == 1
    assert eff["immature"] == 1
    assert eff["rate"] == 0.5


def test_health_effect_absent_without_inbox():
    client, app = _mk()
    body = client.get("/api/care/health").json()
    assert body["effect"] == {}


# ── 健康端点 shadow 段 ───────────────────────────────────────────────────
def test_health_shadow_snapshot():
    client, app = _mk()

    class _FakeScanner:
        def snapshot(self):
            return {"llm_calls": 5, "llm_only": 2, "queue": 0}

    app.state.care_engine = {"shadow_scanner": _FakeScanner()}
    body = client.get("/api/care/health").json()
    assert body["shadow"]["llm_calls"] == 5 and body["shadow"]["llm_only"] == 2


def test_health_shadow_empty_when_unwired():
    client, app = _mk()
    body = client.get("/api/care/health").json()
    assert body["shadow"] == {}
