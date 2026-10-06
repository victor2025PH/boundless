"""0.3.8: scripts/fleet_tests.ps1 跑的本地 fleet 门禁要覆盖 CI 才有的全仓 ratchet。

    python -m pytest tests/test_fleet_local_gate.py -q -p no:cacheprovider
"""
from __future__ import annotations

import codecs
import os
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
GATE = ENGINE / "scripts" / "fleet_tests.ps1"


def test_gate_script_static_shape():
    raw = GATE.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)            # 中文注释：5.1 无 BOM 会按 ANSI 读坏
    src = raw.decode("utf-8-sig")
    assert '"(fleet|phone|ratchet)"' in src            # 文件名自动发现，含全仓 ratchet
    assert "src\\.fleet|fleet_control|fleet_agent|deploy/fleet|fleet_console" in src
    for hook in ("check-yaml", "check-merge-conflict", "debug-statements"):
        assert hook in src                             # 与 CI lint job 同一组只检查钩子
    assert "gitleaks dir" in src and "--redact" in src
    assert "--timeout=90" in src and "git diff --name-only -- src" in src
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("2>&1" in ln for ln in code)       # 5.1 + Stop 下给原生命令重定向 stderr 会中断


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell only")
def test_gate_list_includes_ci_only_ratchets():
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(GATE), "-List"],
                         capture_output=True, text=True, timeout=120, env=env, cwd=str(ENGINE))
    assert res.returncode == 0, res.stdout + res.stderr
    listed = set(res.stdout.split())
    for name in ("test_silent_exception_ratchet.py", "test_template_bare_fetch_ratchet.py",
                 "test_template_inline_color_ratchet.py", "test_workspace_emoji_ratchet.py",
                 "test_fleet_phone_ops.py", "test_fleet_local_gate.py",
                 "test_config_init.py"):
        assert f"tests/{name}" in listed, name
