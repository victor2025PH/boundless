# -*- coding: utf-8 -*-
"""WP-7 坐席新手任务·后端半件门禁（2026-08-18）。

钉住：flag 基线关（API 全 404）、老坐席豁免首读钉死（sticky）、完成幂等、
全✓/dismiss/veteran 三态都让 show=False、每坐席隔离、created_at 多格式解析
宁松勿断（解析失败按新人=有 dismiss 兜底）、条目上限 LRU 驱逐。
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.utils.agent_tasks import (
    TASK_IDS,
    agent_tasks_enabled,
    complete_task,
    dismiss,
    is_veteran_account,
    parse_created_at,
    user_status,
)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    yield tmp_path / "config"


# ── 模块语义 ─────────────────────────────────────────────────────────────────

def test_flag_default_off():
    for cfg in ({}, None, {"onboarding": None}, {"onboarding": {}}):
        assert agent_tasks_enabled(cfg) is False
    assert agent_tasks_enabled({"onboarding": {"agent_tasks": True}}) is True


def test_parse_created_at_formats():
    now = time.time()
    assert abs(parse_created_at(str(int(now))) - now) < 2
    assert abs(parse_created_at(str(int(now * 1000))) - now) < 2
    assert parse_created_at("2026-08-10 12:00:00") > 0
    assert parse_created_at("2026-08-10T12:00:00") > 0
    assert parse_created_at("2026-08-10T12:00:00.123+08:00") > 0
    assert parse_created_at("2026-08-10") > 0
    assert parse_created_at("garbage") == 0.0
    assert parse_created_at("") == 0.0


def test_veteran_semantics():
    now = time.time()
    old = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 30 * 86400))}"
    fresh = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 3600))}"
    assert is_veteran_account(old, now=now) is True
    assert is_veteran_account(fresh, now=now) is False
    assert is_veteran_account("unparseable", now=now) is False, \
        "解析失败按新人（宁多显示一次，dismiss 兜底）"


def test_first_read_pins_veteran_sticky(iso):
    now = time.time()
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 30 * 86400))
    st = user_status("alice", account_created_at=old, now=now)
    assert st["veteran"] is True and st["show"] is False
    # 第二次读换个「新账号」created_at——首读已钉死，不得翻转
    st2 = user_status("alice", account_created_at="2099-01-01 00:00:00", now=now)
    assert st2["veteran"] is True


def test_complete_idempotent_and_all_done(iso):
    now = time.time()
    st = user_status("bob", account_created_at="", now=now)
    assert st["show"] is True and st["done_count"] == 0
    s1 = complete_task("bob", "approve_draft", now=now + 1)
    s2 = complete_task("bob", "approve_draft", now=now + 99)
    t1 = next(t for t in s1["tasks"] if t["id"] == "approve_draft")["done_ts"]
    t2 = next(t for t in s2["tasks"] if t["id"] == "approve_draft")["done_ts"]
    assert t1 == t2, "幂等：重复完成不改首次时间戳"
    for tid in TASK_IDS:
        last = complete_task("bob", tid, now=now + 2)
    assert last["all_done"] is True and last["show"] is False


def test_dismiss_and_isolation(iso):
    now = time.time()
    dismiss("carol", now=now)
    assert user_status("carol", now=now)["show"] is False
    assert user_status("dave", now=now)["show"] is True, "每坐席隔离"


def test_unknown_task_raises(iso):
    with pytest.raises(ValueError):
        complete_task("bob", "nope")


def test_max_users_eviction(iso):
    from src.utils import agent_tasks as at
    now = time.time()
    for i in range(at._MAX_USERS + 5):
        complete_task(f"u{i}", "view_conversation", now=now + i)
    data = at._load_all()
    assert len(data) <= at._MAX_USERS
    assert "u0" not in data and f"u{at._MAX_USERS + 4}" in data


# ── 路由端到端 ───────────────────────────────────────────────────────────────

class _CfgMgr:
    def __init__(self, on: bool):
        self.config = {"onboarding": {"agent_tasks": on}}


class _Users:
    def __init__(self, created_at: str):
        self._c = created_at

    def get_user(self, username):
        return {"username": username, "created_at": self._c}


def _mk_client(*, on=True, created_at=""):
    from src.web.routes.agent_tasks_routes import register_agent_tasks_routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    @app.post("/_login")
    async def _login(request):  # type: ignore[no-untyped-def]
        request.session["username"] = "newbie"
        return {"ok": True}

    register_agent_tasks_routes(
        app, api_auth=lambda: True, config_manager=_CfgMgr(on),
        user_store=_Users(created_at))
    c = TestClient(app)
    c.post("/_login")
    return c


def test_route_flag_off_404(iso):
    c = _mk_client(on=False)
    assert c.get("/api/agent-tasks").status_code == 404
    assert c.post("/api/agent-tasks/complete", json={"task": "edit_send"}
                  ).status_code == 404


def test_route_status_complete_dismiss(iso):
    c = _mk_client(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    r = c.get("/api/agent-tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["show"] is True
    assert [t["id"] for t in body["tasks"]] == list(TASK_IDS)

    r = c.post("/api/agent-tasks/complete", json={"task": "send_voice"})
    assert r.status_code == 200 and r.json()["done_count"] == 1
    assert c.post("/api/agent-tasks/complete", json={"task": "bogus"}
                  ).status_code == 400

    r = c.post("/api/agent-tasks/dismiss")
    assert r.status_code == 200 and r.json()["show"] is False


def test_route_veteran_never_shows(iso):
    old = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time() - 60 * 86400))
    c = _mk_client(created_at=old)
    body = c.get("/api/agent-tasks").json()
    assert body["veteran"] is True and body["show"] is False
