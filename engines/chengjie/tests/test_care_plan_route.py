"""P7 2026-08-03：/api/care/plan 聚合方案 + /api/care/schedule/{sid}/reschedule 路由门禁。

仿 test_care_p2.py 模式：裸 FastAPI() + register_care_routes + TestClient。
重点：方案聚合的形状契约（engine/digest/groups/recent/advice 齐件）、联系人显示名
服务端 join、dry/live 两种 recent 叙事、改期端点的时间校验与状态约束。
metrics 单例的样本计数刻意**不**在这里断言精确值（跨测试进程级共享，-n auto 下
不可靠）——建议规则的精确语义已由 test_care_advisor.py 纯函数门禁钉死。
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_schedule import CareScheduleStore
from src.web.routes.care_routes import register_care_routes


def _auth():
    # 零参依赖=恒放行（*args/**kwargs 会被 FastAPI 解析成必填 query 参数）
    return True


class _CM:
    def __init__(self, cfg):
        self.config = cfg
        self.config_path = ""


def _mk(cfg=None, store=None):
    app = FastAPI()
    cm = _CM(cfg if cfg is not None else {"companion": {}})
    register_care_routes(app, api_auth=_auth, config_manager=cm)
    app.state.care_schedule_store = store or CareScheduleStore(":memory:")
    app.state.config_manager = cm
    return TestClient(app), app


def _add_pending(store, *, topic="面试", contact="telegram:a:1", due_in=3600.0,
                 source="明天面试好紧张"):
    now = time.time()
    rid = store.add_commitment(
        CareCommitment(due_at=now + due_in, event_at=now + due_in, topic=topic,
                       sentiment="neutral", anchor_text="manual",
                       source_text=source, confidence=1.0),
        contact_key=contact, platform="telegram", account_id="a", chat_key="1",
        min_confidence=0.0, dedup_window_days=0.0)
    assert rid
    return rid


class _InboxNames:
    """最小 inbox stub：只提供批量名称 join 用的 get_conversations_for_ids。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.asked = []

    def get_conversations_for_ids(self, ids):
        self.asked.append(list(ids))
        return {k: {"display_name": v} for k, v in self.mapping.items() if k in ids}


# ── /api/care/plan ──────────────────────────────────────────────────────
def test_plan_shape_when_disabled_and_empty():
    client, _ = _mk(cfg={"companion": {}})
    d = client.get("/api/care/plan").json()
    assert d["ok"] is True
    assert d["engine"]["enabled"] is False and d["engine"]["overall"] == "off"
    assert set(d["engine"]["lights"]) == {"engine", "capture", "dispatch", "delivery"}
    assert d["digest"]["pending_total"] == 0 and d["digest"]["summary"]["pending"] == 0
    assert d["groups"] == [] and d["advice"] == []
    assert d["recent"]["mode"] == "live" and d["recent"]["items"] == []
    assert "effect" in d["digest"] and "samples" in d["digest"]


def test_plan_groups_and_server_side_name_join():
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": False},
                         "multiplatform_deferred": {"enabled": True}}}
    client, app = _mk(cfg=cfg)
    store = app.state.care_schedule_store
    _add_pending(store, topic="面试", contact="cid-1", due_in=600.0)
    _add_pending(store, topic="复查", contact="cid-2", due_in=10 * 86400.0)
    app.state.inbox_store = _InboxNames({"cid-1": "Shane", "cid-2": "阿龙"})

    d = client.get("/api/care/plan").json()
    keys = [g["key"] for g in d["groups"]]
    assert keys[0] in ("overdue", "today") and keys[-1] == "later"
    flat = {it["contact_key"]: it for g in d["groups"] for it in g["items"]}
    assert flat["cid-1"]["display_name"] == "Shane"
    assert flat["cid-2"]["display_name"] == "阿龙"
    assert flat["cid-1"]["topic"] == "面试"
    assert flat["cid-1"]["source_text"]  # 原话摘要透传
    assert d["digest"]["pending_total"] == 2


def test_plan_engine_overall_and_recent_mode_dry():
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": True}}}
    client, app = _mk(cfg=cfg)

    class _Disp:
        def health_snapshot(self):
            return {"running": True, "last_tick_ts": 123.0}

    app.state.care_engine = {"capture_wired": True, "dispatcher": _Disp(),
                             "dispatcher_skip": "", "messenger_rpa": False}
    d = client.get("/api/care/plan").json()
    assert d["engine"]["enabled"] is True and d["engine"]["dry_run"] is True
    # 试运行：capture ok + dispatch ok + delivery dry → 整体 ok
    assert d["engine"]["lights"]["delivery"] == "dry"
    assert d["engine"]["overall"] == "ok"
    assert d["recent"]["mode"] == "dry"
    assert d["engine"]["last_tick_ts"] == 123.0


def test_plan_warn_when_capture_broken():
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": True}}}
    client, app = _mk(cfg=cfg)
    app.state.care_engine = {"capture_wired": False, "dispatcher": None,
                             "dispatcher_skip": "ai_missing", "messenger_rpa": False}
    d = client.get("/api/care/plan").json()
    assert d["engine"]["lights"]["capture"] == "broken"
    assert d["engine"]["lights"]["dispatch"] == "ai_missing"
    assert d["engine"]["overall"] == "warn"


def test_plan_recent_live_lists_recent_sent_first():
    from src.monitoring.metrics_store import get_metrics_store
    get_metrics_store()._care_dry_samples.clear()  # 单例隔离：防他测残留拟稿混入 recent
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": False}}}
    client, app = _mk(cfg=cfg)
    store = app.state.care_schedule_store
    r1 = _add_pending(store, topic="早发", contact="c1", due_in=100.0)
    r2 = _add_pending(store, topic="晚发", contact="c2", due_in=200.0)
    assert store.mark_sent(r1, note="deferred:1")
    time.sleep(0.02)  # sent_at 分秒
    assert store.mark_sent(r2, note="deferred:2")
    d = client.get("/api/care/plan").json()
    items = d["recent"]["items"]
    assert d["recent"]["mode"] == "live" and len(items) == 2
    assert items[0]["topic"] == "晚发"  # 最新在前
    assert items[0]["kind"] == "sent" and items[0]["note"].startswith("deferred:")


# ── /api/care/schedule/{sid}/reschedule ────────────────────────────────
def test_reschedule_moves_due_and_requires_pending():
    client, app = _mk()
    store = app.state.care_schedule_store
    rid = _add_pending(store, due_in=3600.0)
    target = time.time() + 48 * 3600.0
    d = client.post(f"/api/care/schedule/{rid}/reschedule",
                    json={"due_at": target}).json()
    assert d["ok"] is True and abs(d["due_at"] - target) < 1.0
    row = store.get(rid)
    assert abs(float(row["due_at"]) - target) < 1.0
    # 非 pending → 拒绝
    assert store.cancel(rid)
    d2 = client.post(f"/api/care/schedule/{rid}/reschedule",
                     json={"due_in_hours": 24}).json()
    assert d2["ok"] is False and d2["reason"] == "not_pending"


def test_reschedule_rejects_past_and_bad_due():
    client, app = _mk()
    store = app.state.care_schedule_store
    rid = _add_pending(store, due_in=3600.0)
    past = client.post(f"/api/care/schedule/{rid}/reschedule",
                       json={"due_at": time.time() - 60}).json()
    assert past["ok"] is False and past["reason"] == "due_in_past"
    bad = client.post(f"/api/care/schedule/{rid}/reschedule",
                      json={"due_at": "nonsense"}).json()
    assert bad["ok"] is False and bad["reason"] == "bad_due"
    # due_in_hours 语义与手动添加一致
    ok = client.post(f"/api/care/schedule/{rid}/reschedule",
                     json={"due_in_hours": 1}).json()
    assert ok["ok"] is True


def test_reschedule_store_only_pending():
    s = CareScheduleStore(":memory:")
    now = time.time()
    rid = s.add_commitment(
        CareCommitment(due_at=now + 3600, event_at=now + 3600, topic="面试",
                       sentiment="neutral", anchor_text="x", source_text="s",
                       confidence=0.9),
        contact_key="c1", platform="telegram", chat_key="1")
    assert s.reschedule(rid, now + 7200) is True
    assert abs(float(s.get(rid)["due_at"]) - (now + 7200)) < 1.0
    assert s.mark_sent(rid)
    assert s.reschedule(rid, now + 9999) is False  # 已 sent 不可改
