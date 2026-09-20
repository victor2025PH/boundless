"""坐席可读「AI 本周替你完成」轻端点（GET /api/workspace/ai-weekly-brief）。

背景（2026-08-14）：/api/report/weekly 是主管专属重报表，普通坐席 403 → 收件箱
空态 ROI 的激励行对最该看到的人反而不显示。本端点只出 drafts.sent 一个数字。

守：
1. 有 inbox_store 时回 build_weekly_value 的 this_week.drafts.sent（available=true）；
2. 进程级 1h TTL 缓存——窗口内绝不重算（重聚合不随前端轮询放大）；
3. 无 inbox_store / 聚合异常 → available=false + sent=0（不 500），且失败同样进冷却。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_setup_routes import register_setup_routes


def _build_client(*, inbox_store=None):
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None, config_manager=None)
    if inbox_store is not None:
        app.state.inbox_store = inbox_store
    return TestClient(app)


def test_brief_returns_sent_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_build(store, **kw):
        calls["n"] += 1
        return {"this_week": {"drafts": {"sent": 42}}}

    monkeypatch.setattr("src.ops.value_report.build_weekly_value", fake_build)
    client = _build_client(inbox_store=object())

    r1 = client.get("/api/workspace/ai-weekly-brief")
    assert r1.status_code == 200
    assert r1.json() == {"ok": True, "available": True, "sent": 42}

    # TTL 窗口内第二次请求复用缓存，不重算（重聚合不随轮询放大）
    r2 = client.get("/api/workspace/ai-weekly-brief")
    assert r2.json()["sent"] == 42
    assert calls["n"] == 1


def test_brief_without_store_is_unavailable_not_500():
    client = _build_client(inbox_store=None)
    r = client.get("/api/workspace/ai-weekly-brief")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "available": False, "sent": 0}


def test_brief_aggregation_failure_soft_and_cooldown(monkeypatch):
    calls = {"n": 0}

    def boom(store, **kw):
        calls["n"] += 1
        raise RuntimeError("db gone")

    monkeypatch.setattr("src.ops.value_report.build_weekly_value", boom)
    client = _build_client(inbox_store=object())

    r1 = client.get("/api/workspace/ai-weekly-brief")
    assert r1.status_code == 200
    assert r1.json()["available"] is False

    # 失败也进 TTL 冷却：窗口内不对故障聚合连环重试
    client.get("/api/workspace/ai-weekly-brief")
    assert calls["n"] == 1
