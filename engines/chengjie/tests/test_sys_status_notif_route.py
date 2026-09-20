# -*- coding: utf-8 -*-
"""实施75 batch4：系统状态留痕端点（POST /api/workspace/notifications/sys-status）。

契约：写进程级通知队列（app.state.notif_queue）→ GET /api/workspace/notifications
回放；按 data.id 合并（同一状态只保留最新一条——一天 N 次维护不堆 N 行）；
空 id/text 拒绝；长度截断（id 64 / text 300）防灌爆。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


def _noauth(request: Request) -> None:
    """必须带类型注解——Depends 会内省签名，裸 lambda 的 request 参数
    会被当成必填 query 字段（422）。"""
    return None


def _client():
    from src.web.routes.unified_inbox_batch_notif_routes import (
        register_batch_notif_routes,
    )
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_batch_notif_routes(app, api_auth=_noauth)
    return app, TestClient(app)


def test_post_then_replay_via_history():
    app, c = _client()
    r = c.post("/api/workspace/notifications/sys-status",
               json={"id": "restartcool", "text": "维护预热中"})
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = c.get("/api/workspace/notifications").json()["notifications"]
    hits = [n for n in rows if n.get("type") == "sys_status"]
    assert len(hits) == 1
    assert hits[0]["data"] == {"id": "restartcool", "text": "维护预热中"}
    assert hits[0]["_notif_ts"] > 0


def test_same_id_coalesces_to_latest():
    app, c = _client()
    c.post("/api/workspace/notifications/sys-status",
           json={"id": "restartcool", "text": "第一次维护"})
    c.post("/api/workspace/notifications/sys-status",
           json={"id": "restartcool", "text": "第二次维护"})
    c.post("/api/workspace/notifications/sys-status",
           json={"id": "aidegrade", "text": "AI 降级"})
    rows = c.get("/api/workspace/notifications").json()["notifications"]
    ss = [n for n in rows if n.get("type") == "sys_status"]
    assert len(ss) == 2, "同 id 必须合并、异 id 各留一条"
    by_id = {n["data"]["id"]: n["data"]["text"] for n in ss}
    assert by_id["restartcool"] == "第二次维护"
    assert by_id["aidegrade"] == "AI 降级"


def test_rejects_empty_and_truncates_long():
    app, c = _client()
    assert c.post("/api/workspace/notifications/sys-status",
                  json={"id": "", "text": "x"}).json()["ok"] is False
    assert c.post("/api/workspace/notifications/sys-status",
                  json={"id": "x", "text": ""}).json()["ok"] is False
    c.post("/api/workspace/notifications/sys-status",
           json={"id": "a" * 200, "text": "b" * 999})
    rows = c.get("/api/workspace/notifications").json()["notifications"]
    d = [n for n in rows if n.get("type") == "sys_status"][0]["data"]
    assert len(d["id"]) == 64 and len(d["text"]) == 300


def test_queue_capped_at_200():
    app, c = _client()
    for i in range(230):
        c.post("/api/workspace/notifications/sys-status",
               json={"id": f"s{i}", "text": f"t{i}"})
    queue = app.state.notif_queue
    assert len(queue) == 200
    # 最老的被挤出、最新的保留
    ids = {n["data"]["id"] for n in queue}
    assert "s229" in ids and "s0" not in ids
