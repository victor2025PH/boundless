"""Agent 自升级（``upgrade`` 任务）：下载 → 校验 sha256 → 换文件 → 由计划任务 / systemd 重新拉起。

payload: ``{"url": "...chatx-agent.exe", "sha256": "<hex>", "version": "0.2.0"}``（sha256 必填，缺则拒绝）。
只支持冻结态（PyInstaller 单文件）；源码态运行的 Agent 拒绝 ``not_frozen``（源码机走 git pull）。

可选安装包（显式才走，缺省仍只换 exe）：``setup_url`` + ``setup_sha256`` 都有时改为下载
``ChatXAgentSetup``、校验 sha256、等本进程退出后静默运行（``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART``，
仅当 ``manage_adb_server`` 为 JSON true 时再加 ``/MANAGEADBSERVER=1``）。已经登记过的节点
（agent.json 里有 node_key）再加 ``/KEEPIDENTITY=1``，安装器把它传给 bootstrap.ps1 的
``-KeepIdentity``，不跑 ``identity --reinstall``。没有 node_key 的新装不带这个开关。直播机直接拒绝，不下载。

Windows 不能覆盖正在运行的 exe：先把新文件落到 ``<state_dir>/updates/``，再派生一个脱离的
PowerShell 等本进程退出后 ``旧→.bak、新→原路径``，然后 ``schtasks /Run`` 拉起；Linux 同理用 sh +
``systemctl restart``。Agent 先 ack done 再退出（退出码 3），保证主控看到回执。

自动回滚（FLEET_ISSUES c/f）：换文件后脚本继续等新版本的第一次成功心跳
（``<state_dir>/last_heartbeat.json`` 的 ``at`` 晚于换文件时刻）。``health_timeout_sec``
内没等到 → 结束任务、杀掉本 exe 的进程、新 exe 留作 ``.failed-<ver>``、``.bak`` 换回，
如果新版本把旧目录改名成 ``fleet.legacy-*``（迁移）就把旧目录原样换回来，agent.json
缺失时用换文件前读进内存的原文补回（只在内存里，不落别处），再 ``schtasks /Run``。
结果追加到 ``<state_dir 的父目录>/fleet-upgrade.log``（不含密钥）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .service import SYSTEMD_UNIT, TASK_NAME, is_frozen

logger = logging.getLogger("fleet.updater")

DOWNLOAD_TIMEOUT = 300
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

Fetch = Callable[[str, Path], None]
Spawn = Callable[[List[str]], Any]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "chatx-agent-updater"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("download too large")
            f.write(chunk)


def download_verified(url: str, sha256: str, dest: Path, *, fetch: Fetch = _fetch) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    fetch(url, tmp)
    got = sha256_file(tmp)
    if got.lower() != sha256.strip().lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch: got {got[:12]}… want {sha256[:12]}…")
    os.replace(tmp, dest)
    return dest


SWAP_TASK_NAME = TASK_NAME + " Upgrade"
HEALTH_TIMEOUT_SEC = 300
HEALTH_TIMEOUT_MIN, HEALTH_TIMEOUT_MAX = 60, 1800
UPGRADE_LOG_NAME = "fleet-upgrade.log"


def health_timeout_of(payload: Dict[str, Any]) -> int:
    """``health_timeout_sec`` from the task payload, clamped; 0 disables the rollback."""
    raw = payload.get("health_timeout_sec") if isinstance(payload, dict) else None
    if raw is None:
        return HEALTH_TIMEOUT_SEC
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return HEALTH_TIMEOUT_SEC
    if val == 0:
        return 0
    return max(HEALTH_TIMEOUT_MIN, min(HEALTH_TIMEOUT_MAX, val))


def _ps_lit(value: Any) -> str:
    """PowerShell single-quoted literal body. PowerShell also treats the curly
    quotes U+2018..U+201B as single quotes, so each of them is doubled too."""
    out = str(value)
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        out = out.replace(q, q + q)
    return out


def _sh_lit(value: Any) -> str:
    return str(value).replace("'", "'\\''")


def build_swap_script(current: Path, new: Path, pid: int, *, task_name: str = TASK_NAME,
                      windows: Optional[bool] = None, state_dir: Optional[Path] = None,
                      version: str = "", health_timeout: int = HEALTH_TIMEOUT_SEC, poll_sec: int = 5,
                      swap_task_name: str = SWAP_TASK_NAME) -> Tuple[str, str]:
    """返回 (脚本文本, 后缀)。等 pid 退出 → 备份旧文件 → 新文件就位 → 重新拉起服务 →
    （给了 state_dir 且 health_timeout>0 时）等新版本心跳，超时自动回滚。"""
    win = (os.name == "nt") if windows is None else windows
    bak = current.with_suffix(current.suffix + ".bak")
    tag = "".join(ch for ch in str(version or "new") if ch.isalnum() or ch in "._-")[:40] or "new"
    failed = current.with_name(current.name + f".failed-{tag}")
    watch = state_dir is not None and int(health_timeout or 0) > 0
    if win:
        lines = [
            "$ErrorActionPreference = 'Continue'",
            f"$cur = '{_ps_lit(current)}'",
            f"$new = '{_ps_lit(new)}'",
            f"$bak = '{_ps_lit(bak)}'",
            f"$task = '{_ps_lit(task_name)}'",
            f"$swapTask = '{_ps_lit(swap_task_name)}'",
        ]
        if watch:
            lines += [
                f"$state = '{_ps_lit(state_dir)}'",
                f"$failed = '{_ps_lit(failed)}'",
                f"$timeout = {int(health_timeout)}",
                "$parent = Split-Path -Parent $state",
                "$leaf = Split-Path -Leaf $state",
                "$log = Join-Path $parent '" + UPGRADE_LOG_NAME + "'",
                "function Note([string]$m) { try { Add-Content -LiteralPath $log -Value ((Get-Date -Format s) + ' ' + $m) -Encoding ascii } catch {} }",
                "$aj = Join-Path $state 'agent.json'",
                # Kept in memory only: the pre-upgrade agent.json, for the rollback.
                "$snap = $null; try { if (Test-Path -LiteralPath $aj) { $snap = [IO.File]::ReadAllBytes($aj) } } catch {}",
                "$before = @(); try { $before = @(Get-Item -Path (Join-Path $parent ($leaf + '.legacy-*')) -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) } catch {}",
            ]
        lines += [
            f"try {{ Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}",
            "Start-Sleep -Seconds 1",
            # 路径只经 $cur/$new/$bak（单引号字面量已转义 '→''），不再直接拼进命令行
            "Copy-Item -LiteralPath $cur -Destination $bak -Force",
            "Move-Item -LiteralPath $new -Destination $cur -Force",
            "$swapped = $?",
        ]
        if watch:
            lines += [
                "$t0 = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()",
            ]
        lines += [
            "schtasks /Run /TN \"$task\" | Out-Null",
        ]
        if watch:
            lines += [
                "if (-not $swapped) { Note 'swap failed: new binary not moved into place; old binary restarted' }",
                "else {",
                "  $ok = $false",
                "  $deadline = (Get-Date).AddSeconds($timeout)",
                "  while ((Get-Date) -lt $deadline) {",
                f"    Start-Sleep -Seconds {max(1, int(poll_sec))}",
                "    try {",
                "      $hb = Get-Content -LiteralPath (Join-Path $state 'last_heartbeat.json') -Raw -ErrorAction Stop | ConvertFrom-Json",
                "      if ([double]$hb.at -gt $t0) { $ok = $true; break }",
                "    } catch {}",
                "  }",
                "  if ($ok) { Note ('upgrade ok: ' + " + f"'{_ps_lit(tag)}'" + " + ' heartbeat seen') }",
                "  else {",
                "    Note ('upgrade rollback: no heartbeat from ' + " + f"'{_ps_lit(tag)}'" + " + ' within ' + $timeout + 's')",
                "    schtasks /End /TN \"$task\" 2>$null | Out-Null",
                "    try { Get-CimInstance Win32_Process -Filter \"Name='chatx-agent.exe'\" | Where-Object { $_.ExecutablePath -eq $cur } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } catch {}",
                "    Start-Sleep -Seconds 2",
                "    Copy-Item -LiteralPath $cur -Destination $failed -Force",
                "    Copy-Item -LiteralPath $bak -Destination $cur -Force",
                "    if (-not $?) { Note 'rollback: could not copy .bak back' }",
                "    $mine = @(); try { $mine = @(Get-Item -Path (Join-Path $parent ($leaf + '.legacy-*')) -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.FullName } | Sort-Object LastWriteTime -Descending) } catch {}",
                "    if ($mine.Count -ge 1) {",
                # The new version migrated: put the old directory back exactly as it was.
                "      $aside = $leaf + '.failed-' + [guid]::NewGuid().ToString('N')",
                "      try { Rename-Item -LiteralPath $state -NewName $aside -ErrorAction Stop; Rename-Item -LiteralPath $mine[0].FullName -NewName $leaf -ErrorAction Stop; Note ('rollback: state dir restored from ' + $mine[0].Name) }",
                "      catch { Note ('rollback: state dir not restored: ' + $_.Exception.Message) }",
                "    }",
                "    if ((-not (Test-Path -LiteralPath $aj)) -and $snap) {",
                "      try { if (-not (Test-Path -LiteralPath $state)) { New-Item -ItemType Directory -Path $state | Out-Null }; [IO.File]::WriteAllBytes($aj, $snap); Note 'rollback: agent.json restored' }",
                "      catch { Note ('rollback: agent.json not restored: ' + $_.Exception.Message) }",
                "    }",
                "    $snap = $null",
                "    schtasks /Run /TN \"$task\" | Out-Null",
                "    Note 'rollback: old binary restarted'",
                "  }",
                "}",
                "$snap = $null",
            ]
        lines += [
            "schtasks /Delete /TN \"$swapTask\" /F 2>$null | Out-Null",
        ]
        return "\n".join(lines) + "\n", ".ps1"
    lines = [
        "#!/bin/sh",
        f"i=0; while kill -0 {int(pid)} 2>/dev/null && [ $i -lt 120 ]; do sleep 1; i=$((i+1)); done",
        f"cp -f '{_sh_lit(current)}' '{_sh_lit(bak)}'",
        f"mv -f '{_sh_lit(new)}' '{_sh_lit(current)}' && chmod +x '{_sh_lit(current)}'",
    ]
    if watch:
        stamp = Path(state_dir) / "last_heartbeat.json"
        aj = Path(state_dir) / "agent.json"
        log = Path(state_dir).parent / UPGRADE_LOG_NAME
        # In a shell variable only; never copied to /tmp.
        lines = lines[:1] + [f"snap=$(cat '{_sh_lit(aj)}' 2>/dev/null)"] + lines[1:]
        lines += [
            "t0=$(date +%s)",
            f"systemctl restart {SYSTEMD_UNIT} 2>/dev/null || true",
            f"ok=0; end=$((t0+{int(health_timeout)}))",
            f"while [ $(date +%s) -lt $end ]; do sleep {max(1, int(poll_sec))}; "
            f"at=$(sed -n 's/.*\"at\": *\\([0-9]*\\).*/\\1/p' '{_sh_lit(stamp)}' 2>/dev/null); "
            "if [ -n \"$at\" ] && [ \"$at\" -gt \"$t0\" ]; then ok=1; break; fi; done",
            "if [ $ok -eq 1 ]; then echo \"$(date -Is) upgrade ok\" >> " + f"'{_sh_lit(log)}'; else",
            f"  echo \"$(date -Is) upgrade rollback: no heartbeat\" >> '{_sh_lit(log)}'",
            f"  systemctl stop {SYSTEMD_UNIT} 2>/dev/null || true",
            f"  cp -f '{_sh_lit(current)}' '{_sh_lit(failed)}'; cp -f '{_sh_lit(bak)}' '{_sh_lit(current)}'"
            f" && chmod +x '{_sh_lit(current)}'",
            f"  if [ ! -f '{_sh_lit(aj)}' ] && [ -n \"$snap\" ]; then (umask 077; printf '%s\\n' \"$snap\" > '{_sh_lit(aj)}'); fi",
            f"  systemctl restart {SYSTEMD_UNIT} 2>/dev/null || true",
            "fi",
            "snap=''",
        ]
    else:
        lines += [f"systemctl restart {SYSTEMD_UNIT} 2>/dev/null || true"]
    return "\n".join(lines) + "\n", ".sh"


def swap_command(script: Path) -> List[str]:
    if script.suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return ["/bin/sh", str(script)]


def build_swap_task_commands(cmd: List[str], *, task_name: str = SWAP_TASK_NAME) -> List[List[str]]:
    """计划任务里跑的进程退出时，Task Scheduler 会连带杀掉它派生的子进程（同一 job object），
    所以换文件脚本不能直接 Popen，而是注册成独立的一次性计划任务立即 /Run。"""
    tr = subprocess.list2cmdline(cmd)
    return [
        ["schtasks", "/Create", "/TN", task_name, "/TR", tr, "/SC", "ONCE", "/ST", "00:00",
         "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"],
        ["schtasks", "/Run", "/TN", task_name],
    ]


def _spawn_detached(cmd: List[str]) -> Any:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        last = None
        for c in build_swap_task_commands(cmd):
            last = subprocess.run(c, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            if last.returncode != 0:
                raise RuntimeError(f"{c[1]} rc={last.returncode}: {(last.stderr or last.stdout).strip()[:200]}")
        return last
    kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_SETUP_FLAGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
_MANAGE_ADB_FLAG = "/MANAGEADBSERVER=1"
_KEEP_IDENTITY_FLAG = "/KEEPIDENTITY=1"


def state_is_enrolled(state_dir: Path) -> bool:
    """True when this state dir already has a node key. A pending or empty dir is not enrolled."""
    path = Path(state_dir) / "agent.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(str(data.get("node_key") or "").strip())


def setup_installer_flags(*, manage_adb_server: bool = False, keep_identity: bool = False) -> List[str]:
    """Fixed silent-install arguments. No operator strings are interpolated."""
    flags = list(_SETUP_FLAGS)
    if keep_identity:
        flags.append(_KEEP_IDENTITY_FLAG)
    if manage_adb_server:
        flags.append(_MANAGE_ADB_FLAG)
    return flags


def bootstrap_switches_for_setup(flags: List[str]) -> List[str]:
    """Switches ChatXAgent.iss appends to bootstrap.ps1 for these installer arguments.

    ``/KEEPIDENTITY=1`` → ``-KeepIdentity`` (skip ``identity --reinstall``).
    ``/MANAGEADBSERVER=1`` → ``-ManageAdbServer``.
    """
    out: List[str] = []
    if _KEEP_IDENTITY_FLAG in flags:
        out.append("-KeepIdentity")
    if _MANAGE_ADB_FLAG in flags:
        out.append("-ManageAdbServer")
    return out


def setup_install_requested(payload: Any) -> bool:
    """True when the task asks for the installer. An empty string does not count."""
    if not isinstance(payload, dict):
        return False
    return bool(str(payload.get("setup_url") or "").strip() or str(payload.get("setup_sha256") or "").strip())


def _https_setup_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def _as_live_stream(state_dir: Path, live_stream: Optional[bool]) -> bool:
    if live_stream is None:
        from .detect import is_live_stream_host

        return bool(is_live_stream_host(state_dir))
    return bool(live_stream)


def _version_tag(version: str) -> str:
    return "".join(ch for ch in (version or "new") if ch.isalnum() or ch in "._-")[:40] or "new"


def build_setup_script(setup: Path, sha256: str, pid: int, state_dir: Path, *,
                       manage_adb_server: bool = False, keep_identity: bool = False) -> Tuple[str, str]:
    """Wait for this process, refuse a live-stream host, re-check sha256, then run the installer.

    The argument list is fixed. ``keep_identity`` adds ``/KEEPIDENTITY=1``.
    ``manage_adb_server`` only adds ``/MANAGEADBSERVER=1``.
    """
    flags = setup_installer_flags(manage_adb_server=manage_adb_server, keep_identity=keep_identity)
    arg_list = ", ".join("'" + _ps_lit(flag) + "'" for flag in flags)
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$setup = '{_ps_lit(setup)}'",
        f"$want = '{_ps_lit(sha256.strip().lower())}'",
        f"$state = '{_ps_lit(state_dir)}'",
        f"try {{ Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}",
        "Start-Sleep -Seconds 1",
        "$envLive = [string]$env:CHATX_FLEET_LIVE_STREAM",
        "if ($envLive -match '^(?i)(1|true|yes|on)$') { exit 2 }",
        "$flagName = 'live-stream.flag'",
        "$roots = @((Split-Path -Parent $state), $state)",
        "if ($env:ProgramData) { $roots += (Join-Path $env:ProgramData 'ChatX') }",
        "foreach ($root in $roots) {",
        "  if (Test-Path -LiteralPath (Join-Path $root $flagName)) { exit 2 }",
        "}",
        "$got = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash",
        "if ($got.ToLower() -ne $want) { exit 1 }",
        f"$argList = @({arg_list})",
        "$proc = Start-Process -FilePath $setup -ArgumentList $argList -Wait -PassThru",
        "if ($null -eq $proc) { exit 1 }",
        "exit $proc.ExitCode",
    ]
    return "\n".join(lines) + "\n", ".ps1"


def apply_remote_setup(payload: Dict[str, Any], state_dir: Path, *, fetch: Fetch = _fetch,
                       spawn: Spawn = _spawn_detached, frozen: Optional[bool] = None,
                       pid: Optional[int] = None, live_stream: Optional[bool] = None,
                       windows: Optional[bool] = None) -> Tuple[str, Dict[str, Any], str]:
    """Download a verified installer and schedule a silent run. Never falls through to an exe swap."""
    if _as_live_stream(state_dir, live_stream):
        return "rejected", {}, "live_stream_host"
    url = str(payload.get("setup_url") or "").strip()
    sha = str(payload.get("setup_sha256") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not url or not sha:
        return "rejected", {}, "setup_url_and_sha256_required"
    if not _SHA256_HEX.match(sha):
        return "rejected", {}, "setup_sha256_invalid"
    if not _https_setup_url(url):
        return "rejected", {}, "setup_url_must_be_https"
    if not (is_frozen() if frozen is None else frozen):
        return "rejected", {"hint": "source checkout: git pull instead"}, "not_frozen"
    win = (os.name == "nt") if windows is None else bool(windows)
    if not win:
        return "rejected", {}, "not_windows"
    manage = payload.get("manage_adb_server") is True
    keep = state_is_enrolled(state_dir)
    dest = state_dir / "updates" / f"ChatXAgentSetup-{_version_tag(version)}.exe"
    try:
        download_verified(url, sha, dest, fetch=fetch)
    except Exception as e:
        msg = str(e)
        if "sha256 mismatch" in msg:
            return "rejected", {"error": msg[:300]}, "sha256_mismatch"
        return "failed", {"error": msg[:300]}, "download_failed"
    got = sha256_file(dest)
    if got.lower() != sha.lower():
        dest.unlink(missing_ok=True)
        return "rejected", {"error": "sha256 mismatch"}, "sha256_mismatch"
    body, suffix = build_setup_script(dest, got, pid or os.getpid(), state_dir,
                                     manage_adb_server=manage, keep_identity=keep)
    script = state_dir / "updates" / ("install-setup" + suffix)
    script.write_text(body, encoding="utf-8")
    try:
        spawn(swap_command(script))
    except Exception as e:
        return "failed", {"error": str(e)[:300]}, "spawn_failed"
    logger.info("[updater] 安装包已校验 %s，静默安装脚本已派生，本进程即将退出", dest)
    return "done", {"version": version, "staged": str(dest), "exit": True, "setup": True,
                    "manage_adb_server": manage, "keep_identity": keep}, "setup_scheduled"


def apply_upgrade(payload: Dict[str, Any], state_dir: Path, *, current_exe: Optional[Path] = None,
                  fetch: Fetch = _fetch, spawn: Spawn = _spawn_detached, frozen: Optional[bool] = None,
                  pid: Optional[int] = None, live_stream: Optional[bool] = None,
                  windows: Optional[bool] = None) -> Tuple[str, Dict[str, Any], str]:
    """返回 (status, result, detail)，status ∈ done / rejected / failed；done 表示换文件脚本已派生，调用方应 ack 后退出。

    ``setup_url`` / ``setup_sha256`` 任一非空则只走安装包，不再换 exe。
    """
    if setup_install_requested(payload):
        return apply_remote_setup(payload, state_dir, fetch=fetch, spawn=spawn, frozen=frozen, pid=pid,
                                  live_stream=live_stream, windows=windows)
    url = str(payload.get("url") or "").strip()
    sha = str(payload.get("sha256") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not url or not sha:
        return "rejected", {}, "url_and_sha256_required"
    if not (is_frozen() if frozen is None else frozen):
        return "rejected", {"hint": "source checkout: git pull instead"}, "not_frozen"
    cur = Path(current_exe or sys.executable)
    dest = state_dir / "updates" / (f"chatx-agent-{version or 'new'}" + cur.suffix)
    try:
        download_verified(url, sha, dest, fetch=fetch)
    except Exception as e:
        return "failed", {"error": str(e)[:300]}, "download_failed"
    timeout = health_timeout_of(payload)
    body, suffix = build_swap_script(cur, dest, pid or os.getpid(), state_dir=state_dir,
                                     version=version, health_timeout=timeout)
    script = state_dir / "updates" / ("swap" + suffix)
    script.write_text(body, encoding="utf-8")
    try:
        spawn(swap_command(script))
    except Exception as e:
        return "failed", {"error": str(e)[:300]}, "spawn_failed"
    logger.info("[updater] 新版本已就位 %s，换文件脚本已派生，本进程即将退出", dest)
    return "done", {"version": version, "staged": str(dest), "exit": True,
                    "rollback_after_sec": timeout}, "swap_scheduled"


__all__ = ["sha256_file", "download_verified", "build_swap_script", "build_swap_task_commands", "swap_command",
           "apply_upgrade", "apply_remote_setup", "setup_install_requested", "build_setup_script",
           "setup_installer_flags", "bootstrap_switches_for_setup", "state_is_enrolled",
           "health_timeout_of", "HEALTH_TIMEOUT_SEC", "UPGRADE_LOG_NAME"]
