"""实施84 P0-5：/api/care/outreach-timeline 统一主动消息时间线门禁。

仿 test_care_plan_route.py 模式（裸 FastAPI + register_care_routes + TestClient）。
重点：来源分类（care/topic/ritual/reactivation/other——ritual 与 topic 同批次
前缀、靠 note 分桶）、显示名 join、旧后端缺方法时的特性探测退场、天数夹取。
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts.care_schedule import CareScheduleStore
from src.web.routes.care_routes import register_care_routes


def _auth():
    return True


class _CM:
    def __init__(self, cfg=None):
        self.config = cfg if cfg is not None else {"companion": {}}
        self.config_path = ""


class _Inbox:
    def __init__(self, rows, names=None):
        self._rows = rows
        self._names = names or {}
        self.asked = {}

    def list_outreach_recent(self, *, days=7.0, limit=200, now=None):
        self.asked = {"days": days, "limit": limit}
        return list(self._rows)

    def get_conversations_for_ids(self, ids):
        return {k: {"display_name": v} for k, v in self._names.items()
                if k in ids}


def _mk(inbox):
    app = FastAPI()
    cm = _CM()
    register_care_routes(app, api_auth=_auth, config_manager=cm)
    app.state.care_schedule_store = CareScheduleStore(":memory:")
    app.state.config_manager = cm
    if inbox is not None:
        app.state.inbox_store = inbox
    return TestClient(app)


def _row(bid, note="", cid="telegram:a:1", ts=None):
    return {"id": 1, "conversation_id": cid, "batch_id": bid,
            "platform": "telegram", "account_id": "a", "status": "sent",
            "note": note, "ts": float(ts if ts is not None else time.time())}


def test_timeline_classifies_sources_and_joins_names():
    rows = [
        _row("care:面试", note="care"),
        _row("proactive_topic:text", note="gentle_checkin"),
        _row("proactive_topic:text", note="daily_ritual"),
        _row("proactive_topic:voice", note="milestone_birthday"),
        _row("reactivation:silent_6d", note="reactivation"),
        _row("someday:x", note=""),
    ]
    c = _mk(_Inbox(rows, names={"telegram:a:1": "小李"}))
    d = c.get("/api/care/outreach-timeline").json()
    assert d["ok"] is True and d["count"] == 6
    assert d["by_source"] == {"care": 1, "topic": 1, "ritual": 2,
                              "reactivation": 1, "other": 1}
    srcs = [it["source"] for it in d["items"]]
    assert srcs.count("ritual") == 2  # daily_ritual + milestone 都归仪式桶
    assert all(it["display_name"] == "小李" for it in d["items"])


def test_timeline_days_clamped_and_passed_through():
    inbox = _Inbox([])
    c = _mk(inbox)
    d = c.get("/api/care/outreach-timeline?days=999&limit=9999").json()
    assert d["ok"] is True and d["count"] == 0
    assert inbox.asked["days"] == 30.0   # 上限 30 天
    assert inbox.asked["limit"] == 500   # 上限 500 条


def test_timeline_unavailable_without_inbox_or_method():
    # 无 inbox store
    c = _mk(None)
    d = c.get("/api/care/outreach-timeline").json()
    assert d["ok"] is False and d["reason"] == "unavailable"

    # 旧后端：inbox 存在但没有 list_outreach_recent（特性探测退场）
    class _Old:
        pass

    c2 = _mk(_Old())
    d2 = c2.get("/api/care/outreach-timeline").json()
    assert d2["ok"] is False and d2["reason"] == "unavailable"


def test_timeline_conversation_filter():
    """实施84 P0-5b：conversation_id 过滤——收件箱来源 chip 的单会话读口。

    过滤后 count/by_source/items 都只含该会话；不传参＝全量（向后兼容）。
    """
    rows = [
        _row("care:面试", note="care", cid="telegram:a:1"),
        _row("proactive_topic:text", note="gentle_checkin", cid="telegram:a:2"),
        _row("reactivation:silent_6d", note="reactivation", cid="telegram:a:1"),
    ]
    c = _mk(_Inbox(rows, names={"telegram:a:1": "小李", "telegram:a:2": "老王"}))
    d = c.get("/api/care/outreach-timeline?conversation_id=telegram:a:1").json()
    assert d["ok"] is True and d["count"] == 2
    assert d["by_source"] == {"care": 1, "reactivation": 1}
    assert all(it["conversation_id"] == "telegram:a:1" for it in d["items"])
    # 不传参仍是全量
    d2 = c.get("/api/care/outreach-timeline").json()
    assert d2["count"] == 3
    # 过滤不命中 → 空表而非报错
    d3 = c.get("/api/care/outreach-timeline?conversation_id=telegram:a:9").json()
    assert d3["ok"] is True and d3["count"] == 0 and d3["items"] == []


def test_timeline_name_join_failure_is_soft():
    class _NoNames(_Inbox):
        def get_conversations_for_ids(self, ids):
            raise RuntimeError("join down")

    c = _mk(_NoNames([_row("care:复查", note="care")]))
    d = c.get("/api/care/outreach-timeline").json()
    assert d["ok"] is True and d["count"] == 1
    assert d["items"][0]["display_name"] == ""  # join 失败回落空名
