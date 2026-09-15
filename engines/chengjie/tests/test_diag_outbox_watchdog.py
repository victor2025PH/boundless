# -*- coding: utf-8 -*-
"""报障 outbox 补传的看门狗兜底（2026-09-16）。

`hosted_gateway.refresh_once` 的补传只在托管/桌面版跑（刷新守护被 `_wants_hosted` 闸住），
源码态实例的暂存件此前只能等「下一次成功上传顺手补传」——zhiliao 一份坏件躺了 8h。
本文件钉住：空目录零动作、有件即补、每小时至多一次、失败吞掉。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.inbox import health_watchdog as hw
from src.utils.diag_upload import list_staged, stage_bundle


class _CM:
    def __init__(self, root: Path):
        cfg_dir = root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = str(cfg_dir / "config.yaml")
        self.config = {}


def _wd(cm):
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=SimpleNamespace())
    w._config_manager = cm
    return w


def _ok_urlopen(req, timeout=0):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "code": "777777"}).encode()
    return _R()


def test_empty_outbox_is_noop(tmp_path):
    w = _wd(_CM(tmp_path))
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not upload")):
        out = w._check_diag_outbox(now=time.time())
    assert out == {"sent": 0, "dropped": 0, "remaining": 0}


def test_staged_bundle_is_flushed_and_throttled(tmp_path):
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    assert stage_bundle(logs, b"PK-1", note="人设页红条", fp="FP", ver="1.0.85")
    w = _wd(cm)
    t0 = time.time()
    calls = {"n": 0}

    def _up(req, timeout=0):
        calls["n"] += 1
        return _ok_urlopen(req, timeout)

    with patch("urllib.request.urlopen", side_effect=_up):
        out = w._check_diag_outbox(now=t0)
    assert out["sent"] == 1 and calls["n"] == 1
    assert list_staged(logs) == []
    # 一小时内再查：不重扫、不重发
    stage_bundle(logs, b"PK-2")
    with patch("urllib.request.urlopen", side_effect=_up):
        assert w._check_diag_outbox(now=t0 + 1800) == {}
    assert calls["n"] == 1
    with patch("urllib.request.urlopen", side_effect=_up):
        out2 = w._check_diag_outbox(now=t0 + 3601)
    assert out2["sent"] == 1 and calls["n"] == 2


def test_flush_failure_is_swallowed(tmp_path):
    cm = _CM(tmp_path)
    stage_bundle(tmp_path / "logs", b"PK-1")
    w = _wd(cm)
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        out = w._check_diag_outbox(now=time.time())
    assert out.get("sent", 0) == 0 and out.get("remaining") == 1


def test_tick_wires_the_check():
    import inspect
    assert "self._check_diag_outbox()" in inspect.getsource(hw.HealthWatchdog._tick)
