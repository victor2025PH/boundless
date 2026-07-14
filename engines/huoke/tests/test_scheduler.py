# -*- coding: utf-8 -*-
"""Scheduler CRUD 单元测试。"""

import pytest
from src.host import scheduler


class TestSchedulerCRUD:

    def test_create_schedule(self, tmp_db):
        sid = scheduler.create_schedule(
            name="test", cron_expr="*/5 * * * *",
            task_type="telegram_send_message",
            params={"username": "@test", "message": "hi"},
        )
        s = scheduler.get_schedule(sid)
        assert s is not None
        assert s["name"] == "test"
        assert s["cron_expr"] == "*/5 * * * *"
        assert s["enabled"] is True
        assert s["next_run"] is not None

    def test_invalid_cron(self, tmp_db):
        with pytest.raises(ValueError, match="无效的 cron"):
            scheduler.create_schedule(
                name="bad", cron_expr="not-a-cron",
                task_type="telegram_send_message",
            )

    def test_list_schedules(self, tmp_db):
        scheduler.create_schedule("a", "* * * * *", "telegram_send_message")
        scheduler.create_schedule("b", "0 * * * *", "telegram_send_message")
        items = scheduler.list_schedules()
        assert len(items) == 2

    def test_toggle(self, tmp_db):
        sid = scheduler.create_schedule("t", "* * * * *", "telegram_send_message")
        scheduler.toggle_schedule(sid, False)
        s = scheduler.get_schedule(sid)
        assert s["enabled"] is False

        scheduler.toggle_schedule(sid, True)
        s = scheduler.get_schedule(sid)
        assert s["enabled"] is True

    def test_delete(self, tmp_db):
        sid = scheduler.create_schedule("d", "* * * * *", "telegram_send_message")
        assert scheduler.delete_schedule(sid) is True
        assert scheduler.get_schedule(sid) is None
        assert scheduler.delete_schedule(sid) is False
