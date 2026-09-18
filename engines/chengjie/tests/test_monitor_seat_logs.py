# -*- coding: utf-8 -*-
"""坐席日志监控：Traceback 时间戳继承 + 离线判定（2026-09-16 幽灵告警）。"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MOD_PATH = _REPO / "deploy" / "desktop" / "monitor_seat_logs.py"


@pytest.fixture(scope="module")
def msl():
    spec = importlib.util.spec_from_file_location("_monitor_seat_logs", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_assign_line_timestamps_inherits_for_traceback(msl):
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: not iterable",
        "[2026-09-13 15:00:00] [WARNING] later",
    ]
    stamped = msl.assign_line_timestamps(lines)
    assert stamped[0][0] == "2026-09-13 14:52:01"
    assert stamped[1][0] == "2026-09-13 14:52:01"  # Traceback 继承
    assert stamped[2][0] == "2026-09-13 14:52:01"
    assert stamped[3][0] == "2026-09-13 14:52:01"
    assert stamped[4][0] == "2026-09-13 15:00:00"


def test_traceback_below_floor_is_skipped(msl):
    """无戳 Traceback 继承后 ≤ floor → 不得当新问题（旧 bug：ts=None 绕过）。"""
    floor = "2026-09-13 14:52:01"
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
    ]
    kept = []
    for eff_ts, line in msl.assign_line_timestamps(lines):
        if eff_ts and floor and eff_ts <= floor:
            continue
        c = msl.classify(line)
        if c and c[0] == "real":
            kept.append(line)
    assert kept == []


def test_new_traceback_after_floor_still_reports(msl):
    floor = "2026-09-13 14:00:00"
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
    ]
    kept = []
    for eff_ts, line in msl.assign_line_timestamps(lines):
        if eff_ts and floor and eff_ts <= floor:
            continue
        c = msl.classify(line)
        if c and c[0] == "real":
            kept.append((eff_ts, c[2]))
    assert any("后端异常" in why for _, why in kept)
    assert kept[0][0] == "2026-09-13 14:52:01"


def test_seat_offline_when_ping_empty(msl):
    now = datetime(2026, 9, 16, 10, 0, 0)
    seat = {"ok": True, "version": "", "mtime": "2026-09-13T14:52:00"}
    why = msl.seat_offline_reason(seat, now=now, offline_after_h=6.0)
    assert why and "离线" in why and "ping" in why

    online = {"ok": True, "version": "ChatX 1.2.3", "mtime": "2026-09-16T09:50:00"}
    assert msl.seat_offline_reason(online, now=now) is None


def test_seat_offline_stale_mtime_even_with_ping(msl):
    now = datetime(2026, 9, 16, 10, 0, 0)
    seat = {"ok": True, "version": "ChatX 1.0", "mtime": "2026-09-13T14:52:00"}
    why = msl.seat_offline_reason(seat, now=now, offline_after_h=6.0)
    assert why and "停摆" in why


def test_parse_mtime_formats(msl):
    assert msl.parse_mtime("2026-09-13T14:52:00") == datetime(2026, 9, 13, 14, 52, 0)
    assert msl.parse_mtime("2026-09-13 14:52:00") == datetime(2026, 9, 13, 14, 52, 0)
    assert msl.parse_mtime("") is None
