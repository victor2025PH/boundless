# -*- coding: utf-8 -*-
"""src.utils.subprocess_text 单元测试。"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_run_merges_utf8_defaults(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr("src.utils.subprocess_text.subprocess.run", fake_run)

    from src.utils.subprocess_text import run

    run(["x", "y"], capture_output=True, timeout=3)
    assert captured.get("text") is True
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("capture_output") is True
    assert captured.get("timeout") == 3


def test_utils_package_reexports_subprocess_run_text():
    from src.utils import subprocess_run_text

    assert callable(subprocess_run_text)


def test_run_text_false_overrides(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("src.utils.subprocess_text.subprocess.run", fake_run)

    from src.utils.subprocess_text import run

    run(["adb"], capture_output=True, text=False)
    assert captured.get("text") is False


def test_run_shell_merges_utf8_and_shell(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="out", stderr="")

    monkeypatch.setattr("src.utils.subprocess_text.subprocess.run", fake_run)

    from src.utils.subprocess_text import run_shell

    run_shell("adb devices", capture_output=True, timeout=5)
    assert captured.get("text") is True
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("shell") is True
    assert captured.get("capture_output") is True
    assert captured.get("timeout") == 5


def test_run_shell_shell_false_overrides(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("src.utils.subprocess_text.subprocess.run", fake_run)

    from src.utils.subprocess_text import run_shell

    run_shell("echo", shell=False)
    assert captured.get("shell") is False


def test_utils_package_reexports_subprocess_run_shell_text():
    from src.utils import subprocess_run_shell_text

    assert callable(subprocess_run_shell_text)
