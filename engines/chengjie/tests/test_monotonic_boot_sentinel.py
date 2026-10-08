"""开机不足冷却/窗口秒数时，monotonic 节流的「第一次」不能被吞（智安 NOTES §7.3）。

time.monotonic() 从开机起算：新 CI runner、刚重启的客户机上它可能只有几十秒。
节流哨兵若用 0.0，``now - 0.0 < window`` 会把第一条审计 / 第一次载入 / 第一次拉起
误判为「冷却中」。哨兵统一为 -inf（或 None），这里把 monotonic 钉在 5 秒复现。
"""
from __future__ import annotations

import time

import pytest

BOOT_UPTIME = 5.0  # 比任何窗口/冷却都小


def test_audit_throttle_first_emit_right_after_boot():
    from src.utils.audit_throttle import AuditThrottle

    th = AuditThrottle(window_sec=60.0)
    assert th.peek("k", now=BOOT_UPTIME) is True
    assert th.remaining_sec("k", now=BOOT_UPTIME) == 0.0
    assert th.should_emit("k", now=BOOT_UPTIME) is True
    # 第二次才进窗口
    assert th.should_emit("k", now=BOOT_UPTIME + 1) is False
    assert th.remaining_sec("k", now=BOOT_UPTIME + 1) == pytest.approx(59.0)


def test_voice_clone_first_load_trigger_right_after_boot(monkeypatch):
    from src.ai import voice_clone_client as vc

    started = []

    class _T:
        def __init__(self, *a, target=None, **k):
            self._t = target

        def start(self):
            started.append(self._t)

    monkeypatch.setattr(vc, "_LOAD_TRIGGER", {})
    monkeypatch.setattr(vc.threading, "Thread", _T)
    monkeypatch.setattr(vc.time, "monotonic", lambda: BOOT_UPTIME)
    cli = vc.VoiceCloneClient({"base_url": "http://127.0.0.1:9", "load_cooldown_sec": 60})
    assert cli.request_model_load_async() is True
    assert len(started) == 1
    # 冷却内的第二次不重复触发
    assert cli.request_model_load_async() is False


def test_avatar_voice_first_boot_trigger_right_after_boot(monkeypatch):
    from src.ai import avatar_voice as av

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, *a, **k):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(av, "_BOOT_TRIGGER", {})
    monkeypatch.setattr(av.subprocess, "run", _run)
    monkeypatch.setattr(av.time, "monotonic", lambda: BOOT_UPTIME)
    cli = av.AvatarVoiceClient({"base_url": "http://127.0.0.1:9", "boot_cooldown_sec": 120})
    cli._trigger_boot_task("ExampleBootTask")
    assert len(calls) == 1 and calls[0][:2] == ["schtasks", "/Run"]
    assert cli._trigger_boot_task("ExampleBootTask") is False
    assert len(calls) == 1


def test_no_zero_sentinel_left_in_fixed_throttles():
    """钉住已修点：这些 monotonic 冷却不得回退成 .get(..., 0.0)。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pins = {
        "src/utils/audit_throttle.py": "self._data.get(key, 0.0)",
        "src/ai/voice_clone_client.py": "_LOAD_TRIGGER.get(self.base_url, 0.0)",
        "src/ai/avatar_voice.py": "_BOOT_TRIGGER.get(task_name, 0.0)",
        "src/integrations/messenger_rpa/runner.py": "_mr_last_navigate.get(serial, 0.0)",
    }
    left = [f for f, pat in pins.items() if pat in (root / f).read_text(encoding="utf-8")]
    assert not left, left
