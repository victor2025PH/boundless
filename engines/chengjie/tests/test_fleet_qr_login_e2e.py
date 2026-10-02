"""Runs tools/fleet_qr_login_e2e.py: real controller routes + real agent + fake instance + console in Chromium."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
HARNESS = ENGINE / "tools" / "fleet_qr_login_e2e.py"


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.mark.parametrize("scenario,expect", [("authorized", "登录成功"), ("failed", "已结束：failed")])
def test_qr_login_end_to_end_in_browser(scenario, expect):
    if not _chromium_ok():
        pytest.skip("playwright chromium not available")
    out = subprocess.run([sys.executable, "-X", "utf8", str(HARNESS), "--scenario", scenario, "--timeout", "60"],
                         capture_output=True, text=True, encoding="utf-8", timeout=240, cwd=str(ENGINE))
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    assert lines, out.stdout + out.stderr
    summary = json.loads(lines[-1])
    assert summary["ok"], json.dumps(summary, ensure_ascii=False, indent=1)
    assert summary["ui"]["final_msg"].startswith(expect)
    assert summary["tasks"] == {"login_qr": 1, "login_status": 3}
    assert out.returncode == 0


def test_harness_never_uses_production_paths():
    src = HARNESS.read_text(encoding="utf-8")
    assert "ProgramData\\\\ChatX" not in src.replace("never C:\\\\ProgramData\\\\ChatX", "")
    assert "bd2026" not in src and "127.0.0.1" in src
    assert "LIVE_STREAM_PORTS" in src
