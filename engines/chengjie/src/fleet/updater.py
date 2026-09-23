"""Agent 自升级（``upgrade`` 任务）：下载 → 校验 sha256 → 换文件 → 由计划任务 / systemd 重新拉起。

payload: ``{"url": "...chatx-agent.exe", "sha256": "<hex>", "version": "0.2.0"}``（sha256 必填，缺则拒绝）。
只支持冻结态（PyInstaller 单文件）；源码态运行的 Agent 拒绝 ``not_frozen``（源码机走 git pull）。

Windows 不能覆盖正在运行的 exe：先把新文件落到 ``<state_dir>/updates/``，再派生一个脱离的
PowerShell 等本进程退出后 ``旧→.bak、新→原路径``，然后 ``schtasks /Run`` 拉起；Linux 同理用 sh +
``systemctl restart``。Agent 先 ack done 再退出（退出码 3），保证主控看到回执。
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


def build_swap_script(current: Path, new: Path, pid: int, *, task_name: str = TASK_NAME,
                      windows: Optional[bool] = None) -> Tuple[str, str]:
    """返回 (脚本文本, 后缀)。等 pid 退出 → 备份旧文件 → 新文件就位 → 重新拉起服务。"""
    win = (os.name == "nt") if windows is None else windows
    bak = current.with_suffix(current.suffix + ".bak")
    if win:
        body = "\n".join([
            "$ErrorActionPreference = 'Continue'",
            f"try {{ Wait-Process -Id {pid} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}",
            "Start-Sleep -Seconds 1",
            f"Copy-Item -LiteralPath '{current}' -Destination '{bak}' -Force",
            f"Move-Item -LiteralPath '{new}' -Destination '{current}' -Force",
            f"schtasks /Run /TN \"{task_name}\" | Out-Null",
        ]) + "\n"
        return body, ".ps1"
    body = "\n".join([
        "#!/bin/sh",
        f"i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt 120 ]; do sleep 1; i=$((i+1)); done",
        f"cp -f '{current}' '{bak}'",
        f"mv -f '{new}' '{current}' && chmod +x '{current}'",
        f"systemctl restart {SYSTEMD_UNIT} 2>/dev/null || true",
    ]) + "\n"
    return body, ".sh"


def swap_command(script: Path) -> List[str]:
    if script.suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return ["/bin/sh", str(script)]


def _spawn_detached(cmd: List[str]) -> Any:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
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
    body, suffix = build_swap_script(cur, dest, pid or os.getpid())
    script = state_dir / "updates" / ("swap" + suffix)
    script.write_text(body, encoding="utf-8")
    try:
        spawn(swap_command(script))
    except Exception as e:
        return "failed", {"error": str(e)[:300]}, "spawn_failed"
    logger.info("[updater] 新版本已就位 %s，换文件脚本已派生，本进程即将退出", dest)
    return "done", {"version": version, "staged": str(dest), "exit": True}, "swap_scheduled"


__all__ = ["sha256_file", "download_verified", "build_swap_script", "swap_command", "apply_upgrade"]
