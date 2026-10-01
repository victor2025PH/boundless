"""P1-8: installer / bootstrap pin PSModulePath; -PackController no longer pushes the agent.

Setup started from pwsh 7 inherits a 7.x PSModulePath. Windows PowerShell 5.1 then fails to
autoload Get-Acl, the state-dir ACL check exits 2 and the install aborts (agent stopped).
Every PowerShell the installer starts is the absolute 5.1 path and pins PSModulePath first.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
ISS = ENGINE / "fleet_agent/setup/ChatXAgent.iss"
BOOT = ENGINE / "fleet_agent/setup/bootstrap.ps1"
INSTALL = ENGINE / "fleet_agent/Install-ChatXAgent.ps1"
PUBLISH = ENGINE / "deploy/fleet/publish_agent.ps1"
PIN = "$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path $env:ProgramFiles 'WindowsPowerShell\\Modules')"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def _iss_fix() -> str:
    m = re.search(r"^\s*PsModuleFix = '((?:[^']|'')*)';", _text(ISS), re.M)
    assert m, "PsModuleFix const missing"
    return m.group(1).replace("''", "'")


def test_every_installer_powershell_is_51_by_absolute_path_and_pins_modules():
    src = _text(ISS)
    execs = re.findall(r"Exec\(ExpandConstant\('([^']*powershell[^']*)'\)", src, re.I)
    assert execs and set(execs) == {r"{sys}\WindowsPowerShell\v1.0\powershell.exe"}
    commands = src.count('-Command "')
    assert commands == 5 and src.count("-Command \"' + PsModuleFix +") == commands
    fix = _iss_fix()
    assert fix.startswith("$env:PSModulePath=") and "$PSHOME" in fix and fix.endswith(";")
    assert '"' not in fix  # lives inside -Command "..."


@pytest.mark.parametrize("path", [BOOT, INSTALL])
def test_scripts_pin_psmodulepath_before_any_work(path):
    src = _text(path)
    assert PIN in src and "if ($PSVersionTable.PSVersion.Major -le 5)" in src
    first_func = src.index("\nfunction ")
    assert src.index(PIN) < first_func


win = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 only")
PS51 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@win
def test_pin_drops_inherited_pwsh7_module_paths_and_get_acl_still_loads():
    # The PSModulePath a pwsh 7 session hands to its children (PS7 dirs first).
    poisoned = ";".join([r"C:\Program Files\PowerShell\Modules", r"C:\Program Files\PowerShell\7\Modules",
                         os.environ.get("PSModulePath", "")])
    env = dict(os.environ, PSModulePath=poisoned)
    probe = ("Get-Acl -LiteralPath $env:SystemRoot | Out-Null;"
             "(Get-Module Microsoft.PowerShell.Security).Path; $env:PSModulePath")
    res = subprocess.run([str(PS51), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _iss_fix() + probe],
                         capture_output=True, text=True, timeout=120, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = [x.strip() for x in res.stdout.splitlines() if x.strip()]
    assert lines[0].lower().startswith(str(PS51.parent).lower())  # 5.1's own Security module
    assert "\\powershell\\7" not in lines[-1].lower() and lines[-1].lower().startswith(str(PS51.parent).lower())


# ── -PackController must not touch the public downloads ───────────────────────
def test_pack_controller_alone_does_not_upload_agent():
    src = _text(PUBLISH)
    assert "[switch]$PublishAgent" in src
    assert "$doAgentUpload = (-not $PackController) -or $PublishAgent" in src
    gate = src.index("if ($doAgentUpload) {")
    assert gate < src.index('Run "ssh $SshHost') and gate < src.index("$mf = Join-Path $DistDir")
    assert gate < src.index("if (-not $NoMirror) {") < src.index("if ($PackController) {")


@win
def test_pack_controller_whatif_plans_no_download_upload(tmp_path):
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    empty = tmp_path / "dist"
    empty.mkdir()  # no manifest: controller packing must not need one
    res = subprocess.run([str(PS51), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PUBLISH),
                          "-DistDir", str(empty), "-SshHost", "nobody@invalid.invalid", "-PackController", "-WhatIf"],
                         capture_output=True, text=True, timeout=120, env=env)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "agent files and the download mirror are NOT uploaded" in out
    assert "/downloads/fleet" not in out and "dl-mirror" not in out
    assert "chatx-fleet-src.tar.gz" in out


def test_pack_controller_excludes_local_backups():
    src = (ENGINE / "deploy/fleet/publish_agent.ps1").read_text(encoding="ascii")
    assert "--exclude=*.bak_*" in src          # local .bak_<ts> copies never ship to /opt/chatx-fleet/app
