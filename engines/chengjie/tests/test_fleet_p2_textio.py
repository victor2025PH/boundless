"""P2-2：ssh 查 status 乱码 —— 子进程输出按 UTF-8/OEM 解码，自己的输出统一 UTF-8。"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from src.fleet import textio

ENGINE = Path(__file__).resolve().parents[1]
GBK_LINE = "模式:                               正在运行".encode("cp936")


def test_decode_utf8_first_then_oem():
    assert textio.decode_console_bytes("在线".encode("utf-8")) == "在线"
    assert textio.decode_console_bytes(GBK_LINE, fallback="cp936").endswith("正在运行")
    assert textio.decode_console_bytes(b"") == "" and textio.decode_console_bytes(None) == ""
    assert textio.decode_console_bytes("x") == "x"
    assert textio.decode_console_bytes(b"\xff\xfe", fallback="no-such-codec")  # 不抛


def test_schtasks_gbk_text_parses_after_decoding():
    from src.fleet.service import parse_schtasks_list_state
    assert parse_schtasks_list_state(textio.decode_console_bytes(GBK_LINE, fallback="cp936")) == "Running"


def test_service_run_decodes_bytes(monkeypatch):
    from src.fleet import service
    monkeypatch.setattr(textio, "oem_encoding", lambda: "cp936")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, GBK_LINE, b""))
    p = service._run(["schtasks"])
    assert p.returncode == 0 and p.stdout.endswith("正在运行") and p.stderr == ""


def test_utf8_stdio_reconfigures_gbk_stream(monkeypatch):
    out = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", None)
    textio.utf8_stdio()
    assert out.encoding.lower().replace("-", "") == "utf8"
    out.write("未运行")
    out.flush()
    assert out.buffer.getvalue() == "未运行".encode("utf-8")


def test_source_mode_status_prints_utf8_under_gbk_locale(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "gbk"
    env["PYTHONPATH"] = str(ENGINE)
    p = subprocess.run([sys.executable, "-m", "src.fleet.agent", "--state-dir", str(tmp_path / "fleet"), "status"],
                       cwd=str(ENGINE), env=env, capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    text = p.stdout.decode("utf-8")          # 严格 UTF-8：GBK 字节会在这里抛
    assert '"ui_label"' in text


def test_node_status_helper_is_ascii_and_read_only():
    src = (ENGINE / "deploy/fleet/node_status.ps1").read_bytes()
    src.decode("ascii")
    s = src.decode("ascii")
    assert "[Console]::OutputEncoding = $utf8" in s and "ValidateSet('status', 'service-status')" in s
