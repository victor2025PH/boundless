# -*- coding: utf-8 -*-
"""API 集成测试 — 使用 FastAPI TestClient，mock 掉设备层。"""

import os
import pytest
from unittest.mock import patch, MagicMock

os.environ["OPENCLAW_API_KEY"] = ""

from fastapi.testclient import TestClient
from src.host.api import app
import src.host.database as db_mod


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    original = db_mod.DB_PATH
    db_mod.DB_PATH = tmp_path / "api_test.db"
    db_mod.init_db()
    yield
    db_mod.DB_PATH = original


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:

    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data
        cap = data.get("capabilities") or {}
        assert cap.get("post_batch_install_apk") is True
        assert cap.get("post_batch_install_apk_cluster") is True
        assert cap.get("post_cluster_batch_install_apk") is True


class TestOpenApiClusterApk:

    def test_openapi_lists_cluster_apk_post_routes(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths") or {}
        assert "/batch/install-apk-cluster" in paths
        assert "/cluster/batch/install-apk" in paths

    def test_openapi_dashboard_routes_have_distinct_operation_ids(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        ops = []
        for path_item in (spec.get("paths") or {}).values():
            for method, op in path_item.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                oid = (op or {}).get("operationId")
                if oid:
                    ops.append(oid)
        assert ops.count("get_dashboard_spa") == 1
        assert ops.count("get_dashboard_core_aggregate") == 1


class TestDashboardCoreAggregate:

    def test_dashboard_core_aggregate_json(self, client):
        r = client.get("/dashboard/core-aggregate")
        assert r.status_code == 200
        data = r.json()
        assert "timestamp" in data


class TestTaskEndpoints:

    @patch("src.host.api.get_device_manager")
    @patch("src.host.api.get_worker_pool")
    def test_create_task(self, mock_pool, mock_mgr, client):
        mock_mgr_inst = MagicMock()
        mock_mgr.return_value = mock_mgr_inst
        mock_mgr_inst.get_connected_devices.return_value = []

        pool_inst = MagicMock()
        pool_inst.submit.return_value = True
        mock_pool.return_value = pool_inst

        r = client.post("/tasks", json={
            "type": "telegram_send_message",
            "params": {"username": "@test", "message": "hello"},
        })
        assert r.status_code == 200
        data = r.json()
        assert "task_id" in data
        assert data["type"] == "telegram_send_message"
        # run_on_host=True (默认) 使 create_task_endpoint 立即 dispatch_after_create,
        # task 会从 pending 瞬时转 running。断言只验证"尚未进入终态", 不锁初始时机。
        assert data["status"] in ("pending", "running")

    def test_get_nonexistent_task(self, client):
        r = client.get("/tasks/nonexistent-id")
        assert r.status_code == 404

    def test_list_tasks(self, client):
        r = client.get("/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    @patch("src.host.api.get_device_manager")
    @patch("src.host.api.get_worker_pool")
    def test_delete_tasks_batch(self, mock_pool, mock_mgr, client):
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_connected_devices.return_value = []
        mock_pool.return_value = MagicMock()
        mock_pool.return_value.submit.return_value = True
        r = client.post("/tasks", json={
            "type": "telegram_send_message",
            "params": {"username": "@x", "message": "h"},
        })
        assert r.status_code == 200
        tid = r.json()["task_id"]
        import src.host.task_store as ts
        ts.set_task_result(tid, True)
        r2 = client.post("/tasks/delete-batch", json={"task_ids": [tid, "nope"]})
        assert r2.status_code == 200
        out = r2.json()
        assert out.get("deleted") == 1
        assert tid in (out.get("deleted_ids") or [])
        sk = {s.get("task_id"): s.get("reason") for s in (out.get("skipped") or [])}
        assert sk.get("nope") == "not_found"

    @patch("src.host.api.get_device_manager")
    @patch("src.host.api.get_worker_pool")
    def test_trash_all_by_status_failed_local(self, mock_pool, mock_mgr, client):
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_connected_devices.return_value = []
        mock_pool.return_value = MagicMock()
        mock_pool.return_value.submit.return_value = True
        r = client.post("/tasks", json={
            "type": "telegram_send_message",
            "params": {"username": "@x", "message": "h"},
        })
        assert r.status_code == 200
        tid = r.json()["task_id"]
        import src.host.task_store as ts
        ts.set_task_result(tid, False, error="x")
        r2 = client.post("/tasks/trash-all-by-status?status=failed&forward_cluster=false")
        assert r2.status_code == 200
        j = r2.json()
        assert j.get("ok") is True
        assert j.get("deleted_local", 0) >= 1
        assert j.get("deleted_total") == j.get("deleted_local")
        assert client.get(f"/tasks/{tid}").status_code == 404

    @patch("src.host.api.get_device_manager")
    @patch("src.host.api.get_worker_pool")
    def test_restore_erase_batch_and_get_include_deleted(self, mock_pool, mock_mgr, client):
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_connected_devices.return_value = []
        mock_pool.return_value = MagicMock()
        mock_pool.return_value.submit.return_value = True
        r = client.post("/tasks", json={
            "type": "telegram_send_message",
            "params": {"username": "@x", "message": "h"},
        })
        assert r.status_code == 200
        tid = r.json()["task_id"]
        import src.host.task_store as ts
        ts.set_task_result(tid, True)
        assert client.delete(f"/tasks/{tid}").status_code == 200
        assert client.get(f"/tasks/{tid}").status_code == 404
        r2 = client.get(f"/tasks/{tid}?include_deleted=true")
        assert r2.status_code == 200
        assert r2.json().get("deleted_at")
        r3 = client.post("/tasks/restore-batch", json={"task_ids": [tid]})
        assert r3.status_code == 200
        assert r3.json().get("restored") == 1
        assert client.post("/tasks/delete-batch", json={"task_ids": [tid]}).json().get("deleted") == 1
        r4 = client.post("/tasks/erase-batch", json={"task_ids": [tid]})
        assert r4.status_code == 200
        assert r4.json().get("erased") == 1
        assert client.get(f"/tasks/{tid}?include_deleted=true").status_code == 404

    @patch("src.host.api.get_device_manager")
    @patch("src.host.api.get_worker_pool")
    def test_tasks_count_trash_only(self, mock_pool, mock_mgr, client):
        mock_mgr.return_value = MagicMock()
        mock_mgr.return_value.get_connected_devices.return_value = []
        mock_pool.return_value = MagicMock()
        mock_pool.return_value.submit.return_value = True
        r0 = client.get("/tasks/count")
        assert r0.status_code == 200
        assert "count" in r0.json()
        rt0 = client.get("/tasks/count?trash_only=true")
        assert rt0.status_code == 200
        before = rt0.json().get("count", 0)
        r = client.post("/tasks", json={
            "type": "telegram_send_message",
            "params": {"username": "@x", "message": "h"},
        })
        assert r.status_code == 200
        tid = r.json()["task_id"]
        import src.host.task_store as ts
        ts.set_task_result(tid, True)
        assert client.delete(f"/tasks/{tid}").status_code == 200
        after = client.get("/tasks/count?trash_only=true").json().get("count", 0)
        assert after == before + 1

    def test_openapi_tasks_endpoints_document_trash_params(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths") or {}
        count_get = (paths.get("/tasks/count") or {}).get("get") or {}
        names = [p.get("name") for p in (count_get.get("parameters") or [])]
        assert "trash_only" in names
        task_id_get = (paths.get("/tasks/{task_id}") or {}).get("get") or {}
        names2 = [p.get("name") for p in (task_id_get.get("parameters") or [])]
        assert "include_deleted" in names2


class TestStatsEndpoint:

    def test_stats(self, client):
        r = client.get("/stats")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data


class TestScheduleEndpoints:

    def test_crud(self, client):
        # create
        r = client.post("/schedules", json={
            "name": "test_sched",
            "cron_expr": "0 * * * *",
            "task_type": "telegram_send_message",
            "params": {"username": "@t", "message": "hi"},
        })
        assert r.status_code == 200
        sid = r.json()["schedule_id"]

        # list
        r = client.get("/schedules")
        assert r.status_code == 200
        assert len(r.json()) >= 1

        # toggle off
        r = client.post(f"/schedules/{sid}/toggle", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        # delete
        r = client.delete(f"/schedules/{sid}")
        assert r.status_code == 200

    def test_invalid_cron(self, client):
        r = client.post("/schedules", json={
            "name": "bad",
            "cron_expr": "bad-cron",
            "task_type": "telegram_send_message",
        })
        assert r.status_code == 400
