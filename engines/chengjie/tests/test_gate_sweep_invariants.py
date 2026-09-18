# -*- coding: utf-8 -*-
"""scripts/gate_sweep.ps1 静态不变量（2026-09-17 R87 sweep 株连假红沉淀）。

R87 收口那次 sweep：生产在线、4 worker、90s thread-timeout。pytest-timeout 在
Windows 只能用 thread 方法，超时用 os._exit() 干掉整个 xdist worker → 「node
down」+ 同 worker 正在跑的用例全部 FAILED（worker crashed while running）。
14 个失败文件再塞进同一个串行 pytest，其中一个 90s 超时又把整次重跑掐死，
权威结果连 summary 都没有。本文件钉住修法，不跑完整 sweep。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gate_sweep.ps1"

CRASH_RE = re.compile(r"^worker '\S+' crashed while running '([^']+)'")
FAILED_RE = re.compile(r"^FAILED\s+(\S+)")


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_prod_online_timeout_is_180_not_hardcoded_90():
    src = _src()
    assert "$gateTimeout = if ($prodUp) { 180 } else { 90 }" in src
    assert src.count("--timeout=$gateTimeout") >= 2
    assert "--timeout=90" not in src, "hardcoded 90s timeout would re-crash the serial re-run"


def test_serial_rerun_is_one_process_per_file():
    src = _src()
    assert "foreach ($ff in $failedFiles)" in src
    assert "python -m pytest $ff -q" in src
    assert "process died before summary" in src
    assert "ONE PROCESS PER FILE" in src


def test_self_listed_in_gates_array():
    src = _src()
    assert "tests/test_gate_sweep_invariants.py" in src
    assert "tests/test_install_node_script_invariants.py" in src


def test_crash_connected_parser_on_r87_shapes():
    lines = [
        "worker 'gw2' crashed while running 'tests/test_fixture_time_bombs.py::test_no_hardcoded_dates_into_trend_fixtures'",
        "worker 'gw3' crashed while running 'tests/test_sql_phantom_columns.py::test_no_phantom_sql_columns'",
        "FAILED tests/test_workspace_emoji_ratchet.py::test_workspace_emoji_not_increasing",
        "FAILED tests/test_sql_phantom_columns.py::test_no_phantom_sql_columns - worke...",
        "FAILED tests/test_sql_phantom_columns.py - process died before summary (pytest-timeout thread kill or crash), exit=1",
    ]
    crashed = []
    for ln in lines:
        m = CRASH_RE.match(ln)
        if m:
            crashed.append(m.group(1))
    failed = []
    for ln in lines:
        m = FAILED_RE.match(ln)
        if m:
            failed.append(m.group(1))
    assert crashed == [
        "tests/test_fixture_time_bombs.py::test_no_hardcoded_dates_into_trend_fixtures",
        "tests/test_sql_phantom_columns.py::test_no_phantom_sql_columns",
    ]
    assert "tests/test_sql_phantom_columns.py::test_no_phantom_sql_columns" in failed

    def still_red(tid: str) -> bool:
        file = tid.split("::", 1)[0]
        return any(r == tid or r == file or r.startswith(file + "::") for r in failed)

    assert still_red("tests/test_sql_phantom_columns.py::test_no_phantom_sql_columns")
    assert still_red("tests/test_sql_phantom_columns.py::test_union_schema_harvest_and_poisoning")
    assert not still_red("tests/test_fixture_time_bombs.py::test_no_hardcoded_dates_into_trend_fixtures")


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell not available")
def test_powershell_parses():
    cmd = (
        "$t=$null;$e=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile('"
        + str(SCRIPT)
        + "',[ref]$t,[ref]$e);"
        "if($e){$e|%{$_.Message};exit 1}else{'ok'}"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
