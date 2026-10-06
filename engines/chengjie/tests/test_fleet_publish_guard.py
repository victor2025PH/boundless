"""publish_agent.ps1 / build_setup.ps1 must not overwrite a signed ChatXAgentSetup.exe (FLEET_ISSUES b).

Static checks run everywhere. The PowerShell runs are Windows-only, local-only:
build_setup.ps1 gets a temp DistDir; publish_agent.ps1 runs with -WhatIf, an
invalid host, and only on paths that must stop before any ssh / scp line.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
PUBLISH = ENGINE / "deploy/fleet/publish_agent.ps1"
BUILD = ENGINE / "fleet_agent/build_setup.ps1"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_publish_rebuilds_setup_only_with_explicit_switch():
    src = _text(PUBLISH)
    assert "if ($BuildSetup -or $iscc)" not in src
    build = src.split("if ($BuildSetup) {", 1)[1].split("} else {", 1)[0]
    assert "ISCC.exe" in build and "$setupScript" in build
    assert "[switch]$OverwriteSigned" in src and "[switch]$RequireSigned" in src
    assert "refusing to rebuild over a signed" in build
    assert build.index("SetupSignature $setupExe") < build.index("& powershell @bsArgs")


def test_publish_checks_hash_and_signature_before_upload():
    src = _text(PUBLISH)
    first_remote = src.index('Run "ssh $SshHost')
    for needle in ("manifest.setup_sha256 does not match", "does not match $setupExe",
                   "-RequireSigned: $setupExe signature is", "Get-AuthenticodeSignature"):
        assert needle in src and src.index(needle) < first_remote, needle
    assert src.index("Get-FileHash -LiteralPath $setupExe") < first_remote
    assert all(ord(c) < 128 for c in src)


def test_build_setup_refuses_to_overwrite_a_signed_installer():
    src = _text(BUILD)
    assert "[switch]$Force" in src and "[switch]$ManifestOnly" in src
    assert src.index("Get-AuthenticodeSignature -LiteralPath $existingSetup") < src.index("& $Iscc")
    assert "exit 3" in src
    assert "Set-Content -LiteralPath $mfPath -Encoding utf8" not in src
    assert all(ord(c) < 128 for c in src)


# ── Windows: really run the scripts (no ISCC, no network) ───────────────────
win = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 only")


def _ps_env() -> dict:
    # Under PowerShell 7 the inherited PSModulePath points Windows PowerShell 5.1 at
    # 7.x modules, so Get-FileHash / Get-AuthenticodeSignature fail to autoload.
    return {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}


def _ps(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                          capture_output=True, text=True, timeout=120, env=_ps_env())


def _signed_binary() -> Path:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for cand in (root / "explorer.exe", root / "System32" / "notepad.exe",
                 root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"):
        if not cand.is_file():
            continue
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-AuthenticodeSignature -LiteralPath '{cand}').Status"],
                             capture_output=True, text=True, timeout=60, env=_ps_env())
        if out.stdout.strip() == "Valid":
            return cand
    pytest.skip("no validly signed system binary to copy")


def _dist(tmp_path: Path, setup_bytes: bytes, *, setup_sha: str = "") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "chatx-agent.exe").write_bytes(b"MZ fake agent")
    (dist / "ChatXAgentSetup.exe").write_bytes(setup_bytes)
    mf = {"name": "chatx-agent", "version": "0.0.1", "file": "chatx-agent.exe",
          "url": "https://invalid.invalid/downloads/fleet/chatx-agent.exe", "sha256": "ab" * 32}
    if setup_sha:
        mf["setup_sha256"] = setup_sha
    (dist / "manifest.json").write_bytes(codecs.BOM_UTF8 + json.dumps(mf).encode("utf-8"))
    return dist


@win
def test_build_setup_exits_3_on_signed_installer(tmp_path):
    signed = _signed_binary()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "chatx-agent.exe").write_bytes(b"MZ fake agent")
    shutil.copyfile(signed, dist / "ChatXAgentSetup.exe")
    before = hashlib.sha256((dist / "ChatXAgentSetup.exe").read_bytes()).hexdigest()
    res = _ps(BUILD, "-DistDir", str(dist))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "refusing to overwrite" in res.stdout
    assert hashlib.sha256((dist / "ChatXAgentSetup.exe").read_bytes()).hexdigest() == before


@win
def test_build_setup_manifest_only_rehashes_without_bom(tmp_path):
    dist = _dist(tmp_path, b"MZ unsigned setup", setup_sha="00" * 32)
    res = _ps(BUILD, "-DistDir", str(dist), "-ManifestOnly")
    assert res.returncode == 0, res.stdout + res.stderr
    raw = (dist / "manifest.json").read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8)
    want = hashlib.sha256(b"MZ unsigned setup").hexdigest()
    assert json.loads(raw.decode("utf-8"))["setup_sha256"] == want
    assert (dist / "ChatXAgentSetup.exe.sha256").read_text(encoding="ascii").split()[0] == want


@win
@pytest.mark.parametrize("extra,needle", [
    ((), "manifest.setup_sha256 does not match"),
    (("-RequireSigned",), "-RequireSigned"),
])
def test_publish_stops_before_any_upload(tmp_path, extra, needle):
    body = b"MZ unsigned setup"
    sha = ("00" * 32) if not extra else hashlib.sha256(body).hexdigest()
    dist = _dist(tmp_path, body, setup_sha=sha)
    res = _ps(PUBLISH, "-DistDir", str(dist), "-SshHost", "nobody@invalid.invalid",
              "-PublicBase", "https://invalid.invalid/downloads/fleet", "-WhatIf", *extra)
    out = res.stdout + res.stderr
    assert res.returncode != 0 and needle in out
    assert "[publish] ssh" not in out and "[publish] scp" not in out


# ── P0-6: default ssh host + mirror sync + generated download page ─────────────
def test_publish_defaults_to_working_ssh_alias_and_syncs_mirror():
    src = _text(PUBLISH)
    assert '[string]$SshHost = "vps-bd2026",' in src and '"ubuntu@bd2026.cc"' not in src.split("param(", 1)[1].split(")", 1)[0]
    assert '[string]$MirrorDir = "/var/www/dl-mirror/downloads/fleet",' in src and "[switch]$NoMirror" in src
    mirror = src.split("if (-not $NoMirror) {", 1)[1]
    for needle in ("render_download_page.py", "sudo install -m 644 -o root -g root", "index.html.bak_$ts",
                   "sha256sum -c", "cmp -s", "chatx-agent-$ver.exe", "ChatXAgentSetup-$ver.exe"):
        assert needle in mirror, needle
    # The page is rendered from the dist files, never from typed-in numbers.
    assert "'--setup', $setupExe" in mirror and "'--agent', $agentLocal" in mirror


@win
def test_publish_whatif_dry_run_plans_mirror_and_renders_page(tmp_path):
    import sys

    body = b"MZ unsigned setup for dry run"
    dist = _dist(tmp_path, body, setup_sha=hashlib.sha256(body).hexdigest())
    mf = json.loads((dist / "manifest.json").read_bytes().decode("utf-8-sig"))
    mf["built_at"] = "2026-10-01T10:42:03"
    (dist / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
    stage = tmp_path / "stage"
    res = _ps(PUBLISH, "-DistDir", str(dist), "-SshHost", "nobody@invalid.invalid",
              "-PublicBase", "https://invalid.invalid/downloads/fleet", "-Python", sys.executable,
              "-StageDir", str(stage), "-WhatIf")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "/var/www/dl-mirror/downloads/fleet/chatx-agent-0.0.1.exe" in out
    assert "/var/www/dl-mirror/downloads/fleet/ChatXAgentSetup-0.0.1.exe" in out
    assert "index.html.bak_" in out and "sha256sum -c chatx-agent-0.0.1.exe.sha256 ChatXAgentSetup-0.0.1.exe.sha256" in out
    page = (stage / "index.html").read_text(encoding="utf-8")
    assert hashlib.sha256(body).hexdigest() in page and "v0.0.1（2026-10-01）" in page
    assert hashlib.sha256(b"MZ fake agent").hexdigest() in page
    side = (stage / "ChatXAgentSetup-0.0.1.exe.sha256").read_text(encoding="ascii")
    assert side == hashlib.sha256(body).hexdigest() + "  ChatXAgentSetup-0.0.1.exe\n"



# ── 0.3.8: started from pwsh 7 / native stderr must not abort ─────────────────
def test_scripts_guard_pwsh_host_and_native_stderr():
    for path in (PUBLISH, BUILD):
        src = _text(path)
        guard = src.index("$PSVersionTable.PSEdition -eq 'Core'")
        assert guard < src.index("Get-FileHash -LiteralPath") and guard < src.index("Get-AuthenticodeSignature -LiteralPath"), path.name
        assert "Remove-Item Env:PSModulePath" in src and "-File $PSCommandPath" in src
        assert "\\\\PowerShell\\\\7" in src and "function Invoke-Native" in src
        assert all(ord(c) < 128 for c in src)
    pub = _text(PUBLISH)
    for needle in ("Invoke-Native { Invoke-Expression $cmd }", "Invoke-Native { & powershell @bsArgs }",
                   "Invoke-Native { & $Python @pyArgs }", "Invoke-Native { & tar -cf $raw",
                   "Invoke-Native { & tar -rf $raw"):
        assert needle in pub, needle
    assert 'Invoke-Native { & $Iscc "/DAgentExe=$agent"' in _text(BUILD)


def _polluted_env() -> dict:
    """What a pwsh 7 parent hands a child powershell.exe: its own module dirs first."""
    env = _ps_env()
    home = os.environ.get("USERPROFILE", r"C:\Users\Default")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    env["PSModulePath"] = ";".join([os.path.join(home, "Documents", "PowerShell", "Modules"),
                                    os.path.join(pf, "PowerShell", "Modules"),
                                    os.path.join(pf, "PowerShell", "7", "Modules"),
                                    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                 "system32", "WindowsPowerShell", "v1.0", "Modules")])
    return env


@win
def test_build_setup_manifest_only_with_pwsh_module_path(tmp_path):
    dist = _dist(tmp_path, b"MZ unsigned setup", setup_sha="00" * 32)
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUILD),
                          "-DistDir", str(dist), "-ManifestOnly"],
                         capture_output=True, text=True, timeout=120, env=_polluted_env())
    assert res.returncode == 0, res.stdout + res.stderr
    want = hashlib.sha256(b"MZ unsigned setup").hexdigest()
    assert json.loads((dist / "manifest.json").read_bytes().decode("utf-8"))["setup_sha256"] == want


@win
def test_build_setup_started_from_pwsh_reruns_under_winps(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    dist = _dist(tmp_path, b"MZ unsigned setup", setup_sha="00" * 32)
    res = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUILD),
                          "-DistDir", str(dist), "-ManifestOnly"],
                         capture_output=True, text=True, timeout=180, env=dict(os.environ))
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "re-running under Windows PowerShell 5.1" in out
    want = hashlib.sha256(b"MZ unsigned setup").hexdigest()
    assert (dist / "ChatXAgentSetup.exe.sha256").read_text(encoding="ascii").split()[0] == want


@win
def test_publish_whatif_survives_native_stderr(tmp_path):
    """A caller that redirects the script in-process (`& publish_agent.ps1 ... *> log`) makes 5.1 turn
    any native stderr line into a NativeCommandError; with ErrorActionPreference=Stop that used to
    abort the run half way (reproduced on the 0.3.7 script with a python that prints a warning)."""
    import sys

    body = b"MZ unsigned setup noisy"
    dist = _dist(tmp_path, body, setup_sha=hashlib.sha256(body).hexdigest())
    noisy = tmp_path / "noisy_py.cmd"
    noisy.write_text('@echo off\r\necho WARNING: noise on stderr 1>&2\r\n"%s" %%*\r\n' % sys.executable, encoding="ascii")
    log = tmp_path / "publish.log"
    inner = tmp_path / "inner.ps1"
    inner.write_text(
        "& '%s' -DistDir '%s' -SshHost nobody@invalid.invalid -PublicBase https://invalid.invalid/downloads/fleet "
        "-Python '%s' -StageDir '%s' -WhatIf *> '%s'\r\nexit $LASTEXITCODE\r\n"
        % (PUBLISH, dist, noisy, tmp_path / "stage", log), encoding="ascii")
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(inner)],
                         capture_output=True, text=True, timeout=120, env=_polluted_env())
    out = res.stdout + res.stderr + (log.read_text(encoding="utf-16", errors="replace") if log.is_file() else "")
    assert res.returncode == 0, out
    assert "download page rendered from dist files" in out and "noise on stderr" in out
    assert (tmp_path / "stage" / "index.html").is_file()
