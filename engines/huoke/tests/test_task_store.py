# -*- coding: utf-8 -*-
"""task_store 单元测试 — 纯 SQLite 逻辑，不需要设备。"""

import pytest
from src.host import task_store


class TestTaskStore:

    def test_create_and_get(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "dev1", {"msg": "hello"})
        t = task_store.get_task(tid)
        assert t is not None
        assert t["task_id"] == tid
        assert t["type"] == "telegram_send_message"
        assert t["device_id"] == "dev1"
        assert t["status"] == "pending"
        assert t["params"]["msg"] == "hello"

    def test_set_running(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "dev1", {})
        task_store.set_task_running(tid)
        t = task_store.get_task(tid)
        assert t["status"] == "running"

    def test_set_result_success(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "dev1", {})
        task_store.set_task_result(tid, success=True, error="",
                                   screenshot_path="/tmp/s.png",
                                   extra={"device_id": "dev1"})
        t = task_store.get_task(tid)
        assert t["status"] == "completed"
        assert t["result"]["success"] is True
        assert t["result"]["screenshot_path"] == "/tmp/s.png"

    def test_set_result_failure(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "dev1", {})
        task_store.set_task_result(tid, success=False, error="连接失败")
        t = task_store.get_task(tid)
        assert t["status"] == "failed"
        assert "连接失败" in t["result"]["error"]

    def test_set_result_persists_gate_hint_message(self, tmp_db):
        tid = task_store.create_task("tiktok_follow", "dev1", {})
        task_store.set_task_result(
            tid,
            success=False,
            error="[gate] 预检未通过",
            extra={
                "device_id": "dev1",
                "gate_evaluation": {"hint_code": "preflight_vpn", "allowed": False},
            },
        )
        t = task_store.get_task(tid)
        ge = t["result"]["gate_evaluation"]
        assert ge.get("hint_message")
        assert "VPN" in ge["hint_message"] or "预检" in ge["hint_message"]

    def test_cancel(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "dev1", {})
        task_store.set_task_cancelled(tid)
        t = task_store.get_task(tid)
        assert t["status"] == "cancelled"

    def test_list_filter_by_status(self, tmp_db):
        t1 = task_store.create_task("telegram_send_message", "dev1", {})
        t2 = task_store.create_task("telegram_send_message", "dev2", {})
        task_store.set_task_running(t2)
        pending = task_store.list_tasks(status="pending")
        running = task_store.list_tasks(status="running")
        assert len(pending) == 1
        assert len(running) == 1
        assert pending[0]["task_id"] == t1
        assert running[0]["task_id"] == t2

    def test_list_filter_by_device(self, tmp_db):
        task_store.create_task("telegram_send_message", "devA", {})
        task_store.create_task("telegram_send_message", "devB", {})
        items = task_store.list_tasks(device_id="devA")
        assert len(items) == 1
        assert items[0]["device_id"] == "devA"

    def test_get_stats(self, tmp_db):
        t1 = task_store.create_task("telegram_send_message", "d1", {})
        t2 = task_store.create_task("telegram_send_message", "d1", {})
        t3 = task_store.create_task("telegram_send_message", "d1", {})
        task_store.set_task_result(t1, True)
        task_store.set_task_result(t2, False, error="err")
        stats = task_store.get_stats()
        assert stats["total"] == 3
        assert stats.get("completed", 0) == 1
        assert stats.get("failed", 0) == 1
        assert stats.get("pending", 0) == 1

    def test_get_nonexistent(self, tmp_db):
        assert task_store.get_task("does-not-exist") is None

    def test_delete_tasks_batch(self, tmp_db):
        ok1 = task_store.create_task("telegram_send_message", "d1", {})
        ok2 = task_store.create_task("telegram_send_message", "d1", {})
        pend = task_store.create_task("telegram_send_message", "d1", {})
        task_store.set_task_result(ok1, True)
        task_store.set_task_result(ok2, False, error="x")
        r = task_store.delete_tasks_batch([ok1, ok2, pend, "missing-id"])
        assert r["deleted"] == 2
        assert set(r["deleted_ids"]) == {ok1, ok2}
        reasons = {s["task_id"]: s["reason"] for s in r["skipped"]}
        assert reasons[pend] == "running_or_pending"
        assert reasons["missing-id"] == "not_found"
        assert task_store.get_task(ok1) is None
        assert task_store.get_task(pend) is not None

    def test_delete_tasks_batch_max(self, tmp_db):
        ids = [f"id-{i}" for i in range(101)]
        with pytest.raises(ValueError, match="最多"):
            task_store.delete_tasks_batch(ids)

    def test_soft_delete_all_by_status(self, tmp_db):
        t1 = task_store.create_task("telegram_send_message", "d1", {})
        t2 = task_store.create_task("telegram_send_message", "d1", {})
        task_store.set_task_result(t1, False, error="a")
        task_store.set_task_result(t2, False, error="b")
        n = task_store.soft_delete_all_by_status("failed")
        assert n == 2
        assert task_store.get_task(t1) is None
        t1d = task_store.get_task(t1, include_deleted=True)
        assert t1d and t1d.get("deleted_at")
        with pytest.raises(ValueError, match="必须是"):
            task_store.soft_delete_all_by_status("running")

    def test_soft_delete_restore_erase_and_trash_list(self, tmp_db):
        tid = task_store.create_task("telegram_send_message", "d1", {})
        task_store.set_task_result(tid, True)
        assert task_store.delete_task(tid) is True
        assert task_store.get_task(tid) is None
        assert task_store.get_task(tid, include_deleted=True) is not None
        trash = task_store.list_tasks(trash_only=True, limit=20)
        assert len(trash) == 1
        r = task_store.restore_tasks_batch([tid])
        assert r["restored"] == 1
        assert task_store.get_task(tid) is not None
        assert task_store.list_tasks(trash_only=True, limit=20) == []
        assert task_store.delete_tasks_batch([tid])["deleted"] == 1
        e = task_store.erase_tasks_batch([tid])
        assert e["erased"] == 1
        assert task_store.get_task(tid, include_deleted=True) is None
