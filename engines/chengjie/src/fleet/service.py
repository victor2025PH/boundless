"""节点 Agent 服务化：开机自启 + 崩溃自愈 + 被吊销后等待重新注册。

Windows 用计划任务（``schtasks`` ONSTART / SYSTEM / 最高权限）而不是真 Windows 服务：
不依赖 pywin32，PyInstaller 单文件即可安装；SYSTEM 账户下 ``%ProgramData%\\ChatX\\fleet``
仍是同一状态目录。Linux 写 systemd unit。

    chatx-agent install-service      # 安装并立即启动
    chatx-agent uninstall-service
    chatx-agent service-status
    chatx-agent run --service        # 计划任务 / systemd 实际跑的入口：监督循环，永不因单次异常退出

命令行都经 ``build_*`` 纯函数生成，测试只断言参数，不真调 schtasks。
"""

from __future__ import annotations

import logging
import ntpath
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("fleet.service")

TASK_NAME = "ChatX Fleet Agent"
SYSTEMD_UNIT = "chatx-agent"
RETRY_UNENROLLED_SEC = 30      # 未注册：等安装器 / 人工 enroll 写入 node_key
PENDING_POLL_SEC = 15          # 已提交待批准：隔一会儿问主控是否已批准
RETRY_REVOKED_SEC = 60         # 被吊销：等主控重新发码并 enroll
CRASH_BACKOFF_MIN, CRASH_BACKOFF_MAX = 5.0, 300.0

RunFn = Callable[[Sequence[str]], subprocess.CompletedProcess]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def agent_command(state_dir: Optional[Path] = None) -> List[str]:
    """当前进程对应的「再启动自己」命令（冻结 exe 或 ``python -m src.fleet.agent``）。"""
    cmd = [sys.executable] if is_frozen() else [sys.executable, "-m", "src.fleet.agent"]
    if state_dir is not None:
        cmd += ["--state-dir", str(state_dir)]
    return cmd


def _win_quote(parts: Sequence[str]) -> str:
    return " ".join(f'"{p}"' if (" " in p or not p) else p for p in parts)


def build_schtasks_create(cmd: Sequence[str], *, task_name: str = TASK_NAME) -> List[str]:
    return ["schtasks", "/Create", "/TN", task_name, "/TR", _win_quote(cmd),
            "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"]


def build_schtasks_run(*, task_name: str = TASK_NAME) -> List[str]:
    return ["schtasks", "/Run", "/TN", task_name]


def build_schtasks_delete(*, task_name: str = TASK_NAME) -> List[List[str]]:
    return [["schtasks", "/End", "/TN", task_name], ["schtasks", "/Delete", "/TN", task_name, "/F"]]


def build_schtasks_query(*, task_name: str = TASK_NAME) -> List[str]:
    return ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"]


def service_workdir() -> Optional[Path]:
    """源码态 systemd 需要 WorkingDirectory=引擎根，否则 `python -m src.fleet.agent` 找不到包。

    冻结 exe 自带路径，返回 None（不写 WorkingDirectory）。
    """
    if is_frozen():
        return None
    # service.py -> fleet -> src -> 引擎根 (engines/chengjie)
    return Path(__file__).resolve().parents[2]


def build_systemd_unit(
    cmd: Sequence[str],
    *,
    state_dir: Optional[Path] = None,
    working_dir: Optional[Path] = None,
) -> str:
    env = f"Environment=CHATX_FLEET_STATE_DIR={state_dir}\n" if state_dir else ""
    wd = f"WorkingDirectory={working_dir}\n" if working_dir else ""
    return (
        "[Unit]\nDescription=ChatX Fleet Agent\nAfter=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"{wd}"
        f"ExecStart={shlex.join(list(cmd))}\n{env}"
        "Restart=always\nRestartSec=10\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def systemd_unit_path(name: str = SYSTEMD_UNIT) -> Path:
    return Path("/etc/systemd/system") / f"{name}.service"


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    # 不用 text=True：schtasks 在管道里按 OEM 代码页（中文 936）输出，统一由 decode_console_bytes 解码
    from .textio import decode_console_bytes

    p = subprocess.run(list(cmd), capture_output=True, timeout=60)
    return subprocess.CompletedProcess(p.args, p.returncode, decode_console_bytes(p.stdout), decode_console_bytes(p.stderr))


def install_service(state_dir: Path, *, run: RunFn = _run, task_name: str = TASK_NAME) -> Dict[str, object]:
    cmd = agent_command(state_dir) + ["run", "--service"]
    if os.name == "nt":
        # End a running instance first: /Create /F on a running task leaves the old
        # process detached from the new definition, so /End no longer reaches it.
        try:
            run(["schtasks", "/End", "/TN", task_name])
        except Exception as e:
            logger.debug("[service] schtasks /End failed: %s", e)
        steps = [build_schtasks_create(cmd, task_name=task_name), build_schtasks_run(task_name=task_name)]
        outs = []
        for s in steps:
            p = run(s)
            outs.append({"cmd": s[:2], "rc": p.returncode, "out": (p.stdout or p.stderr or "")[-300:]})
            if p.returncode != 0:
                return {"ok": False, "kind": "schtasks", "task_name": task_name, "steps": outs}
        return {"ok": True, "kind": "schtasks", "task_name": task_name, "command": cmd, "steps": outs}
    unit = systemd_unit_path()
    unit.write_text(
        build_systemd_unit(cmd, state_dir=state_dir, working_dir=service_workdir()),
        encoding="utf-8",
    )
    outs = []
    for s in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", SYSTEMD_UNIT]):
        p = run(s)
        outs.append({"cmd": s[:2], "rc": p.returncode, "out": (p.stdout or p.stderr or "")[-300:]})
        if p.returncode != 0:
            return {"ok": False, "kind": "systemd", "unit": str(unit), "steps": outs}
    return {"ok": True, "kind": "systemd", "unit": str(unit), "command": cmd, "steps": outs}


def uninstall_service(*, run: RunFn = _run, task_name: str = TASK_NAME) -> Dict[str, object]:
    if os.name == "nt":
        outs = [{"rc": run(s).returncode} for s in build_schtasks_delete(task_name=task_name)]
        return {"ok": outs[-1]["rc"] == 0, "kind": "schtasks", "task_name": task_name, "steps": outs}
    outs = [{"rc": run(s).returncode} for s in (["systemctl", "disable", "--now", SYSTEMD_UNIT],)]
    try:
        systemd_unit_path().unlink()
    except FileNotFoundError:
        pass
    run(["systemctl", "daemon-reload"])
    return {"ok": True, "kind": "systemd", "steps": outs}


# Task Scheduler TASK_STATE（COM ``IRegisteredTask.State``）：数值与界面语言无关。
TASK_STATE_NAMES = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}
# schtasks 文本兜底：值按界面语言本地化。中文 /FO LIST /V 里 Status 这一项叫「模式」，
# 「登录状态」「计划任务状态」是别的字段，所以只按整键精确匹配。
_STATUS_KEYS = ("status", "状态", "模式")
_STATE_ALIASES = {
    "running": "Running", "正在运行": "Running",
    "ready": "Ready", "就绪": "Ready", "准备就绪": "Ready",
    "disabled": "Disabled", "已禁用": "Disabled",
    "queued": "Queued", "已排队": "Queued", "排队": "Queued",
    "unknown": "Unknown", "未知": "Unknown",
}


def _windows_powershell() -> str:
    """固定用 Windows PowerShell 5.1：从 pwsh 7 启动时 PATH 里的 powershell 也可能被换掉。"""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def build_task_state_query(*, task_name: str = TASK_NAME) -> List[str]:
    """经 Schedule.Service COM 读任务的数值状态（0-4），不解析任何本地化文本。

    COM 不依赖 ScheduledTasks 模块，所以不受继承来的 PSModulePath 影响。任务不存在或无权读时 exit 3。
    """
    lit = task_name.replace("'", "''")
    script = ("$ErrorActionPreference='Stop';try{$s=New-Object -ComObject Schedule.Service;$s.Connect();"
              "[int]$s.GetFolder('\\').GetTask('" + lit + "').State}catch{exit 3}")
    return [_windows_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def parse_task_state_number(out: str) -> str:
    """COM 输出的一行数字 → Running / Ready / ...；不是 0-4 的数字返回空串。"""
    lines = [x.strip() for x in str(out or "").splitlines() if x.strip()]
    if len(lines) != 1 or not lines[0].isdigit():
        return ""
    return TASK_STATE_NAMES.get(int(lines[0]), "")


def parse_schtasks_list_state(out: str) -> str:
    """``schtasks /FO LIST /V`` 文本兜底：英文 Status、中文「模式」（老版本「状态」）。已知值归一成英文。"""
    for line in str(out or "").splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            continue
        if k.strip().lower() in _STATUS_KEYS:
            raw = v.strip()
            return _STATE_ALIASES.get(raw.lower(), raw)
    return ""


def service_status(*, run: RunFn = _run, task_name: str = TASK_NAME) -> Dict[str, object]:
    if os.name == "nt":
        p = run(build_schtasks_query(task_name=task_name))
        installed = p.returncode == 0
        state = ""
        if installed:
            # 先取与语言无关的数值状态；PowerShell 起不来、超时或没权限时才退回解析文本。
            try:
                q = run(build_task_state_query(task_name=task_name))
                if q.returncode == 0:
                    state = parse_task_state_number(q.stdout or "")
            except (OSError, subprocess.SubprocessError, ValueError):
                logger.debug("[service] COM task state query failed; falling back to schtasks text", exc_info=True)
            if not state:
                state = parse_schtasks_list_state(p.stdout or "")
        return {"installed": installed, "kind": "schtasks", "task_name": task_name, "state": state}
    p = run(["systemctl", "is-active", SYSTEMD_UNIT])
    return {"installed": systemd_unit_path().exists(), "kind": "systemd", "state": (p.stdout or "").strip()}


# ── 单实例（FLEET_ISSUES g3：计划任务外多出来的 run 进程） ─────────────────────
SINGLE_INSTANCE_WAIT_SEC = 30
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT = 0x0, 0x80, 0x102


def single_instance_name(state_dir: Path) -> str:
    """Per state dir, so a test or second agent with its own --state-dir is not blocked."""
    import hashlib

    key = os.path.normcase(os.path.abspath(str(state_dir)))
    return "ChatXFleetAgent-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class SingleInstance:
    """Held for the life of a ``run`` process. Released on exit by the OS."""

    def __init__(self, kind: str, handle: object, path: str = "") -> None:
        self.kind = kind
        self.handle = handle
        self.path = path

    def release(self) -> None:
        h, self.handle = self.handle, None
        if h is None:
            return
        try:
            if self.kind == "mutex":
                import ctypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.ReleaseMutex(h)
                k32.CloseHandle(h)
            else:
                import fcntl

                fcntl.flock(h.fileno(), fcntl.LOCK_UN)
                h.close()
        except Exception as e:
            logger.debug("[service] lock release failed: %s", e)


def _try_mutex(name: str) -> Optional[SingleInstance]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    h = k32.CreateMutexW(None, False, "Global\\" + name)
    if not h:
        # ERROR_ACCESS_DENIED: a SYSTEM agent owns it and we are a normal user.
        return None
    rc = k32.WaitForSingleObject(h, 0)
    if rc in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        return SingleInstance("mutex", h)
    k32.CloseHandle(h)
    return None


def _try_flock(state_dir: Path, name: str) -> Optional[SingleInstance]:
    import fcntl

    # Next to the state dir, never inside it: a handle inside ``fleet`` would
    # block its rename during migration on Windows, and we keep one rule.
    parent = Path(state_dir).parent
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f".{name}.lock"
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return SingleInstance("flock", f, str(path))


def acquire_single_instance(state_dir: Path, *, wait_sec: float = SINGLE_INSTANCE_WAIT_SEC,
                            sleep: Callable[[float], None] = time.sleep,
                            clock: Callable[[], float] = time.monotonic) -> Optional[SingleInstance]:
    """One ``run`` per state dir. Waits up to ``wait_sec`` (an upgrade swap may
    still be letting the old process exit), then returns None."""
    name = single_instance_name(state_dir)
    deadline = clock() + max(0.0, float(wait_sec))
    while True:
        got = _try_mutex(name) if os.name == "nt" else _try_flock(state_dir, name)
        if got is not None:
            return got
        if clock() >= deadline:
            return None
        sleep(1.0)


def _pending_request(cfg: object) -> str:
    data = getattr(cfg, "data", None)
    if isinstance(data, dict):
        return str(data.get("pending_request_id") or "")
    return ""


def _enroll_followup(cfg: object) -> str:
    """``pending`` while a request id is open; ``backoff`` while a dead id is being retried."""
    if _pending_request(cfg):
        return "pending"
    data = getattr(cfg, "data", None)
    if isinstance(data, dict) and data.get("reenroll_not_before"):
        return "backoff"
    return ""


def supervise(make_agent: Callable[[], object], stop: Optional[threading.Event] = None, *,
              sleep: Callable[[float], None] = time.sleep, max_rounds: int = 0) -> int:
    """监督循环：未注册 → 等；被吊销 → 等重新 enroll；异常 → 指数退避重启；``exit_requested`` → 退出（升级换文件）。

    ``make_agent`` 每轮重新构造（重读 agent.json，拿到新 node_key）。返回退出码：0 正常 / 3 升级退出。
    """
    stop = stop or threading.Event()
    backoff = CRASH_BACKOFF_MIN
    rounds = 0
    while not stop.is_set():
        rounds += 1
        if max_rounds and rounds > max_rounds:
            return 0
        try:
            agent = make_agent()
            cfg = getattr(agent, "cfg")
            follow = _enroll_followup(cfg)
            if follow and hasattr(agent, "poll_enrollment"):
                try:
                    agent.poll_enrollment()
                except Exception as e:
                    logger.warning("[service] pending poll failed: %s", e)
                follow = _enroll_followup(cfg)
            if not cfg.node_key:
                delay = PENDING_POLL_SEC if follow == "pending" else RETRY_UNENROLLED_SEC
                logger.info("[service] 尚未注册，%ss 后重试", delay)
                sleep(delay)
                continue
            agent.run_forever(stop)
            if getattr(agent, "exit_requested", False):
                logger.info("[service] 收到退出请求（升级换文件），退出码 3")
                return 3
            if getattr(agent, "revoked", False):
                logger.warning("[service] node_key 被吊销，%ss 后重读配置再试", RETRY_REVOKED_SEC)
                sleep(RETRY_REVOKED_SEC)
                continue
            backoff = CRASH_BACKOFF_MIN
        except Exception as e:  # run_forever 自身已兜底；这里兜构造期 / 未预期错误
            logger.error("[service] agent 崩溃: %s（%.0fs 后重启）", e, backoff, exc_info=True)
            sleep(backoff)
            backoff = min(CRASH_BACKOFF_MAX, backoff * 2)
    return 0


__all__ = [
    "TASK_NAME", "SYSTEMD_UNIT", "is_frozen", "service_workdir", "agent_command", "build_schtasks_create", "build_schtasks_run",
    "build_schtasks_delete", "build_schtasks_query", "build_task_state_query", "build_systemd_unit", "install_service", "uninstall_service",
    "service_status", "supervise", "acquire_single_instance", "single_instance_name", "SingleInstance",
]
