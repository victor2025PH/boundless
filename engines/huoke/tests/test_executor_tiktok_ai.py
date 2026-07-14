# -*- coding: utf-8 -*-
"""executor：tiktok_ai_restore / tiktok_ai_rescore 走 ai-rescore 接口。"""
import json
from unittest.mock import MagicMock, patch

import pytest


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("task_type", ["tiktok_ai_restore", "tiktok_ai_rescore"])
def test_tiktok_ai_tasks_call_ai_rescore(task_type):
    from src.host import executor as ex

    mgr = MagicMock()
    payload = {"rescored": 2, "ok": True}

    with patch.object(ex, "_fresh_tiktok", return_value=MagicMock()):
        with patch.object(ex, "_check_tiktok_version", lambda *a, **k: None):
            with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
                ok, msg, data = ex._execute_tiktok(
                    mgr, "TESTSERIAL01", task_type, {"limit": 5},
                )

    assert ok is True
    assert msg == ""
    assert data == payload
