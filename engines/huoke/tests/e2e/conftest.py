# -*- coding: utf-8 -*-
"""E2E：独立子进程启动 uvicorn，保证 OPENCLAW_API_KEY 为空以便加载 /dashboard。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from src.host.device_registry import PROJECT_ROOT


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def e2e_base_url():
    pytest.importorskip("pytest_playwright")
    port = _free_port()
    env = os.environ.copy()
    env["OPENCLAW_API_KEY"] = ""
    env["OPENCLAW_SESSION_TOKEN"] = ""
    pr = str(PROJECT_ROOT)
    env["PYTHONPATH"] = pr + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else pr
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.host.api:app",
        "--host",
        "127.0.0.1",
        f"--port={port}",
        "--log-level",
        "warning",
    ]
    popen_kw: dict = {
        "cwd": str(PROJECT_ROOT),
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.Popen(cmd, **popen_kw)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/health", timeout=2)
            break
        except OSError as e:
            last_err = e
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else b""
                pytest.fail("uvicorn exited early: " + err.decode(errors="replace")[:4000])
            time.sleep(0.25)
    else:
        err = proc.stderr.read() if proc.stderr else b""
        proc.terminate()
        pytest.fail(
            "server not ready: "
            + repr(last_err)
            + " stderr="
            + err.decode(errors="replace")[:4000]
        )

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def e2e_guest_token(e2e_base_url: str) -> str:
    """会话级 guest token，避免多用例重复 POST /auth/login 与 uvicorn 短时竞态。"""
    req = urllib.request.Request(
        e2e_base_url + "/auth/login",
        data=json.dumps({}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["token"]
