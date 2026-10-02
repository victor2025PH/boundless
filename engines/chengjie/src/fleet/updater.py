"""Agent 自升级（``upgrade`` 任务）：下载 → 校验 sha256 → 换文件 → 由计划任务 / systemd 重新拉起。

payload: ``{"url": "...chatx-agent.exe", "sha256": "<hex>", "version": "0.2.0"}``（sha256 必填，缺则拒绝）。
只支持冻结态（PyInstaller 单文件）；源码态运行的 Agent 拒绝 ``not_frozen``（源码机走 git pull）。

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
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def apply_upgrade(payload: Dict[str, Any], state_dir: Path, *, current_exe: Optional[Path] = None,
                  fetch: Fetch = _fetch, spawn: Spawn = _spawn_detached, frozen: Optional[bool] = None,
                  pid: Optional[int] = None) -> Tuple[str, Dict[str, Any], str]:
    """返回 (status, result, detail)，status ∈ done / rejected / failed；done 表示换文件脚本已派生，调用方应 ack 后退出。"""
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
           "apply_upgrade", "health_timeout_of", "HEALTH_TIMEOUT_SEC", "UPGRADE_LOG_NAME"]
