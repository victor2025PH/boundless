"""group_members_routes 路由层测试（开关门控 / 参数校验 / 无在线号 503 / CSV 导出）。

不依赖真实 Telegram：``_get_tg_pyro_for_account`` 在测试 app 里取不到 client → 提取端点
如实 503（而非静默假成功）。读端点用 :memory: store 直接验数据形状。
"""
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.companion.group_members_store import (
    configure_group_members_store,
    get_group_members_store,
    reset_group_members_store,
)
from src.web.routes.group_members_routes import register_group_members_routes


class _CM:
    def __init__(self, enabled=True):
        self.config = {"companion": {"group_members": {
            "enabled": enabled, "daily_cap_per_account": 50, "scan_limit": 100}}}


@pytest.fixture()
def client_cm():
    reset_group_members_store()
    st = configure_group_members_store(":memory:")
    st.record_members([{
        "group_id": "-100", "user_id": "1", "username": "alice", "first_name": "A",
        "spoke": True, "is_admin": False, "group_title": "G",
        "source_account_id": "accA", "extracted_at": time.time(),
    }])
    cm = _CM(enabled=True)
    app = FastAPI()

    async def _auth():
        return True

    register_group_members_routes(app, auth_dep=_auth, audit_store=None, config_manager=cm)
    with TestClient(app) as c:
        yield c, cm
    reset_group_members_store()


def test_disabled_gate_403(client_cm):
    c, cm = client_cm
    cm.config["companion"]["group_members"]["enabled"] = False
    assert c.get("/api/tg-members/quota?account_id=accA").status_code == 403
    assert c.get("/api/tg-members/members?group_id=-100").status_code == 403


def test_quota_shape(client_cm):
    c, _ = client_cm
    r = c.get("/api/tg-members/quota?account_id=accA")
    assert r.status_code == 200
    d = r.json()
    assert d["cap"] == 50 and d["used_today"] == 1 and d["remaining"] == 49


def test_members_list_and_missing_group(client_cm):
    c, _ = client_cm
    assert c.get("/api/tg-members/members").status_code == 400  # 缺 group_id
    r = c.get("/api/tg-members/members?group_id=-100")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1 and d["spoke"] == 1 and len(d["items"]) == 1


def test_groups_and_jobs_list(client_cm):
    c, _ = client_cm
    assert c.get("/api/tg-members/groups").json()["groups"][0]["group_id"] == "-100"
    assert c.get("/api/tg-members/jobs").json()["jobs"] == []


def test_create_job_validation(client_cm):
    c, _ = client_cm
    assert c.post("/api/tg-members/jobs", json={}).status_code == 400          # 缺参
    assert c.post("/api/tg-members/jobs",
                  json={"account_id": "accA", "group": "-100",
                        "filter": "bogus"}).status_code == 400                  # 过滤器非法


def test_create_job_no_live_client_503(client_cm):
    c, _ = client_cm
    # 测试 app 无 orchestrator / app.state.telegram_client → 取不到活体 client → 503
    r = c.post("/api/tg-members/jobs",
               json={"account_id": "accA", "group": "-100", "filter": "spoke_no_admin"})
    assert r.status_code == 503


def test_export_csv(client_cm):
    c, _ = client_cm
    r = c.get("/api/tg-members/members/export?group_id=-100")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    body = r.text
    assert body.splitlines()[0].startswith("user_id,username")
    assert "alice" in body
