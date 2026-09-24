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
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=60)


def install_service(state_dir: Path, *, run: RunFn = _run, task_name: str = TASK_NAME) -> Dict[str, object]:
    cmd = agent_command(state_dir) + ["run", "--service"]
    if os.name == "nt":
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


def service_status(*, run: RunFn = _run, task_name: str = TASK_NAME) -> Dict[str, object]:
    if os.name == "nt":
        p = run(build_schtasks_query(task_name=task_name))
        installed = p.returncode == 0
        state = ""
        for line in (p.stdout or "").splitlines():
            k, _, v = line.partition(":")
            if k.strip().lower() in ("status", "状态"):
                state = v.strip()
                break
        return {"installed": installed, "kind": "schtasks", "task_name": task_name, "state": state}
    p = run(["systemctl", "is-active", SYSTEMD_UNIT])
    return {"installed": systemd_unit_path().exists(), "kind": "systemd", "state": (p.stdout or "").strip()}


def _pending_request(cfg: object) -> str:
    data = getattr(cfg, "data", None)
    if isinstance(data, dict):
        return str(data.get("pending_request_id") or "")
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
            if _pending_request(cfg) and hasattr(agent, "poll_enrollment"):
                try:
                    agent.poll_enrollment()
                except Exception as e:
                    logger.warning("[service] pending poll failed: %s", e)
            if not cfg.node_key:
                delay = PENDING_POLL_SEC if _pending_request(cfg) else RETRY_UNENROLLED_SEC
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
    "build_schtasks_delete", "build_schtasks_query", "build_systemd_unit", "install_service", "uninstall_service",
    "service_status", "supervise",
]
