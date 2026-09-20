# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 后端托管（一键启停，2026-09-19 P1）。

引导页第 ③ 步此前要用户复制 PowerShell 命令（``tools/wechat_pc_devlink.ps1``）手动拉起驱动进程；本模块把驱动
（``python -m src.integrations.wechat_pc``）作为**后端子进程**托管，照 :mod:`local_tts_supervisor` 的骨架：

- **同机前提**：UIA 读屏要求驱动跑在微信所在 PC 的交互式会话里。后端 ``check_environment()`` 能看到微信进程
  ⇔ 后端与微信同机 ⇒ 可托管；否则页面回落到手动命令（局域网部署）。
- **attach-if-alive**：注册表心跳已在线（计划任务 / devlink 手动起的进程）→ 不重复拉起，只显示在线。
- **Job Object KILL_ON_JOB_CLOSE**：后端无论怎么退出都回收子进程；``stop()`` 再显式 taskkill 整棵树。
- **日志**：stdout/stderr 落 ``<state_dir>/copilot.log``，``log_tail()`` 供页面「查看日志」。
- **崩溃退避**：启动后 60 秒内退出算一次失败，2s → 10s → 60s → 300s 退避后由自启轮询再拉。
- **自启**：``platform_login.wechat_pc.autostart=true`` → :meth:`autostart_loop` 每 15 秒看一眼环境（纯 Win32，
  几毫秒），微信主窗出现（= 主人扫码登录完成）即拉起。这就是「扫码登录后自动开始」。
- **令牌**：走环境变量 ``CHATX_ADMIN_TOKEN``（驱动 ``--token-env``），不出现在进程命令行。
- **档位**：不传 ``--tier``，驱动以配置文件为准并热生效（见 ``__main__``），引导页保存档位不必重启。

纯函数 :func:`derive_state` / :func:`decide_autostart` / :func:`build_driver_command` 可离线单测。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.WeChatPcSupervisor")

STATE_IDLE = "idle"          # 没在跑、也没心跳
STATE_STARTING = "starting"  # 进程已拉起，等首个心跳
STATE_ONLINE = "online"      # 心跳在线且能读屏
STATE_BLIND = "blind"        # 心跳在线但读不到微信主窗（托盘/未登录）
STATE_OFFLINE = "offline"    # 曾有心跳已过期 / 进程正常退出
STATE_ERROR = "error"        # 进程异常退出 / 起来了却一直没心跳

ACT_SKIP = "skip"            # 未启用自启 / 已在线 / 已在跑
ACT_START = "start"
ACT_WAIT_WECHAT = "wait_wechat"
ACT_BACKOFF = "backoff"
ACT_NO_DRIVER = "no_driver"

STARTING_GRACE_SEC = 75.0    # 自检 + 首次心跳（心跳线程 10 秒一报；锚点自检真机 10–30 秒）
QUICK_EXIT_SEC = 60.0        # 启动后多久内退出算「起不来」
_BACKOFF_STEPS = (2.0, 10.0, 60.0, 300.0)
TOKEN_ENV = "CHATX_ADMIN_TOKEN"


def derive_state(*, proc_alive: bool, exit_code: Optional[int], started_at: float,
                 presence: Optional[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """进程态 + 注册表心跳 → 页面展示态（纯函数）。

    ``presence`` 是 :func:`desktop_bridge_presence.bridge_presence` 的输出（``alive / readable / age_sec / tier``）。
    ``attached=True`` 表示在线的是别人拉起的进程（计划任务 / devlink），本 supervisor 只观察不托管。
    """
    pres_alive = bool(presence and presence.get("alive"))
    readable = presence.get("readable") if isinstance(presence, dict) else None
    starting = proc_alive and started_at > 0 and now - started_at <= STARTING_GRACE_SEC
    if pres_alive:
        # 首个心跳往往带 readable=False（还没跑完第一拍）：宽限期内仍算 starting，别闪一下琥珀色 blind
        if readable is False and starting:
            return {"state": STATE_STARTING, "reason": "", "attached": False}
        st = STATE_BLIND if readable is False else STATE_ONLINE
        return {"state": st, "reason": "", "attached": not proc_alive}
    if proc_alive:
        if starting:
            return {"state": STATE_STARTING, "reason": "", "attached": False}
        return {"state": STATE_ERROR, "reason": "no_heartbeat", "attached": False}
    if exit_code is not None:
        return {"state": (STATE_OFFLINE if exit_code == 0 else STATE_ERROR), "reason": f"exit_{exit_code}",
                "attached": False}
    if presence:
        return {"state": STATE_OFFLINE, "reason": "heartbeat_stale", "attached": False}
    return {"state": STATE_IDLE, "reason": "", "attached": False}


def decide_autostart(*, enabled: bool, proc_alive: bool, presence_alive: bool, wechat_main_window: bool,
                     driver_ok: bool, backoff_until: float, now: float) -> str:
    """自启轮询每一拍该做什么（纯函数）。"""
    if not enabled or proc_alive or presence_alive:
        return ACT_SKIP
    if not driver_ok:
        return ACT_NO_DRIVER
    if now < backoff_until:
        return ACT_BACKOFF
    if not wechat_main_window:
        return ACT_WAIT_WECHAT
    return ACT_START


def next_backoff(failures: int) -> float:
    """连续第 ``failures`` 次起不来 → 退避秒数（1 → 2s … ≥4 → 300s）。"""
    if failures <= 0:
        return 0.0
    return _BACKOFF_STEPS[min(failures, len(_BACKOFF_STEPS)) - 1]


FROZEN_DRIVER_FLAG = "--wechat-pc-driver"


def build_driver_command(*, python_exe: str, backend_url: str, account_id: str, label: str, config_file: str,
                         state_dir: str, interval: float = 3.0, connected_days: float = -1.0,
                         frozen: Optional[bool] = None) -> List[str]:
    """驱动子进程命令行（纯函数）：不带 ``--tier``（配置文件权威 + 热生效）、不带令牌（环境变量）。

    ``frozen``（默认取 ``sys.frozen``）：打包态 ``sys.executable`` 是 PyInstaller 的 ``backend.exe``，
    不认 ``-u -m``——用 ``-m`` 起出来的是第二个后端（抢 18799 或直接退出，状态卡恒 error）。
    打包态改为 ``backend.exe --wechat-pc-driver <args>``，由 ``main.py`` 首参分流到驱动入口。
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        cmd = [python_exe, FROZEN_DRIVER_FLAG]
    else:
        cmd = [python_exe, "-u", "-m", "src.integrations.wechat_pc"]
    cmd += ["--backend-url", backend_url, "--account-id", account_id, "--label", label,
            "--token-env", TOKEN_ENV, "--interval", str(float(interval)), "--state-dir", state_dir]
    if config_file:
        cmd += ["--config", config_file]
    if connected_days >= 0:
        cmd += ["--connected-days", str(float(connected_days))]
    return cmd


class WeChatPcSupervisor:
    """驱动子进程的生命周期管理（永不向调用方抛异常；失败只记日志并在 ``status()`` 里体现）。"""

    def __init__(self, *, engine_root: str, backend_url: str, token_provider: Callable[[], str],
                 config_file: str, state_dir: str, account_id: str = "wechat-pc", label: str = "个人微信 · PC 副驾",
                 presence_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 env_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 driver_ready_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 python_exe: str = "", autostart: bool = False, interval: float = 3.0,
                 popen: Callable[..., Any] = subprocess.Popen, now: Callable[[], float] = time.time) -> None:
        self.engine_root = str(engine_root)
        self.backend_url = str(backend_url).rstrip("/")
        self.token_provider = token_provider
        self.config_file = str(config_file or "")
        self.state_dir = str(state_dir)
        self.account_id = str(account_id or "wechat-pc")
        self.label = str(label or "")
        self.presence_provider = presence_provider or (lambda: None)
        self.env_provider = env_provider or (lambda: {})
        self.driver_ready_provider = driver_ready_provider or (lambda: {"ok": True})
        self.python_exe = str(python_exe or sys.executable)
        self.autostart = bool(autostart)
        self.interval = float(interval)
        self._popen = popen
        self._now = now

        self._proc: Optional[Any] = None
        self._job: Optional[int] = None
        self._logf = None
        self._started_at: float = 0.0
        self._exit_code: Optional[int] = None
        self._failures: int = 0
        self._backoff_until: float = 0.0
        self._last_error: str = ""
        self._stopped_hb_ts: float = 0.0
        self._events: deque = deque(maxlen=30)
        self._task: Optional[asyncio.Task] = None

    # ── 观察 ──
    @property
    def log_path(self) -> str:
        return os.path.join(self.state_dir, "copilot.log")

    def _proc_alive(self) -> bool:
        p = self._proc
        if p is None:
            return False
        try:
            rc = p.poll()
        except Exception:
            return False
        if rc is None:
            return True
        # 刚发现退出：记一次
        if self._exit_code is None:
            self._exit_code = int(rc)
            quick = self._started_at > 0 and (self._now() - self._started_at) < QUICK_EXIT_SEC
            if rc != 0 or quick:
                self._failures += 1
                self._backoff_until = self._now() + next_backoff(self._failures)
                self._last_error = f"exit_{rc}"
                self._event("exit", f"code={rc} quick={quick} backoff={next_backoff(self._failures):.0f}s")
            else:
                self._event("exit", f"code={rc}")
            self._close_job()
            self._close_logf()
        return False

    def _event(self, kind: str, detail: str = "") -> None:
        self._events.append({"ts": round(self._now(), 3), "kind": kind, "detail": detail})
        logger.info("[wechat_pc.supervisor] %s %s", kind, detail)

    def _presence(self) -> Optional[Dict[str, Any]]:
        """注册表心跳；本 supervisor 刚 stop 掉的进程留下的最后一拍（ts ≤ 停止时看到的 ts）视为已过期，
        否则停止后 ~1 分钟内页面会闪「在线 · 由计划任务或手动命令启动」。"""
        try:
            pres = self.presence_provider()
        except Exception:
            return None
        if not isinstance(pres, dict):
            return None
        if self._stopped_hb_ts > 0 and pres.get("alive"):
            try:
                if float(pres.get("ts") or 0.0) <= self._stopped_hb_ts:
                    return {**pres, "alive": False, "stale_after_stop": True}
            except Exception:
                pass
            self._stopped_hb_ts = 0.0  # 出现了更新的心跳 → 是别处新起的进程，恢复正常判定
        return pres

    def status(self) -> Dict[str, Any]:
        """页面状态卡数据：``{state, reason, attached, managed, pid, since, heartbeat, backoff_sec, last_error, log_tail}``。"""
        alive = self._proc_alive()
        presence = self._presence()
        now = self._now()
        d = derive_state(proc_alive=alive, exit_code=self._exit_code, started_at=self._started_at,
                         presence=presence, now=now)
        pid = None
        if alive:
            try:
                pid = int(self._proc.pid)
            except Exception:
                pid = None
        return {
            **d,
            "managed": alive,
            "pid": pid,
            "since": (self._started_at if alive else 0.0),
            "uptime_sec": (int(now - self._started_at) if alive and self._started_at else 0),
            "heartbeat": presence or None,
            "tier": (presence or {}).get("tier") if presence else None,
            "backoff_sec": int(max(0.0, self._backoff_until - now)),
            "failures": self._failures,
            "last_error": self._last_error,
            "autostart": self.autostart,
            "log_path": self.log_path,
            "log_tail": self.log_tail(12),
            "events": list(self._events)[-8:],
        }

    def log_tail(self, n: int = 20) -> List[str]:
        try:
            p = Path(self.log_path)
            if not p.exists():
                return []
            with open(p, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                data = fh.read().decode("utf-8", errors="replace")
            lines = [ln.rstrip() for ln in data.splitlines() if ln.strip()]
            return lines[-max(1, int(n)):]
        except Exception:
            return []

    # ── 控制 ──
    def start(self, *, force: bool = False) -> Dict[str, Any]:
        """拉起驱动（幂等）。``force=False`` 时若心跳已在线（别处起的进程）→ attach 不重复拉。"""
        if self._proc_alive():
            return {"ok": True, "action": "already_running", **self.status()}
        if not force:
            pres = self._presence()
            if pres and pres.get("alive"):
                self._event("attach", "heartbeat alive from external process")
                return {"ok": True, "action": "attach", **self.status()}
        ready = {}
        try:
            ready = self.driver_ready_provider() or {}
        except Exception:
            ready = {"ok": False}
        if not ready.get("ok", True):
            self._last_error = "driver_not_ready"
            return {**self.status(), "ok": False, "action": "start", "reason": "driver_not_ready", "driver": ready}
        token = ""
        try:
            token = str(self.token_provider() or "").strip()
        except Exception:
            token = ""
        if not token:
            self._last_error = "admin_token_missing"
            return {**self.status(), "ok": False, "action": "start", "reason": "admin_token_missing"}
        try:
            Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        cmd = build_driver_command(python_exe=self.python_exe, backend_url=self.backend_url,
                                   account_id=self.account_id, label=self.label, config_file=self.config_file,
                                   state_dir=self.state_dir, interval=self.interval)
        env = dict(os.environ)
        env[TOKEN_ENV] = token
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self._logf = open(self.log_path, "ab", buffering=0)
        except Exception:
            self._logf = None
        try:
            self._proc = self._popen(cmd, cwd=self.engine_root, env=env,
                                     stdout=(self._logf or subprocess.DEVNULL), stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=creationflags)
        except Exception as exc:  # noqa: BLE001
            self._proc = None
            self._close_logf()
            self._last_error = f"spawn_failed: {exc}"
            self._failures += 1
            self._backoff_until = self._now() + next_backoff(self._failures)
            self._event("spawn_failed", str(exc))
            return {**self.status(), "ok": False, "action": "start", "reason": self._last_error}
        self._started_at = self._now()
        self._exit_code = None
        self._last_error = ""
        self._event("spawn", f"pid={getattr(self._proc, 'pid', '?')}")
        if sys.platform == "win32":
            self._assign_job(getattr(self._proc, "pid", 0))
        return {"ok": True, "action": "start", **self.status()}

    def stop(self) -> Dict[str, Any]:
        """结束托管的驱动进程（整棵树）。别处起的进程不归本 supervisor 管，返回 ``not_managed``。"""
        if not self._proc_alive():
            self._proc = None
            return {"ok": True, "action": "not_managed", **self.status()}
        proc = self._proc
        self._event("stop", f"pid={getattr(proc, 'pid', '?')}")
        # 记下被停进程最后一拍心跳的 ts：之后 ts 不更新的心跳都算过期（见 _presence）
        try:
            pres = self.presence_provider()
            self._stopped_hb_ts = float((pres or {}).get("ts") or 0.0) if isinstance(pres, dict) else 0.0
        except Exception:
            self._stopped_hb_ts = 0.0
        self._kill_tree(proc)
        # 主人主动停：不算失败（taskkill 的退出码是 1，不能把它当异常退出）
        self._exit_code = 0
        self._last_error = ""
        self._proc = None
        self._started_at = 0.0
        self._failures = 0
        self._backoff_until = 0.0
        self._close_job()
        self._close_logf()
        return {"ok": True, "action": "stop", **self.status()}

    def restart(self) -> Dict[str, Any]:
        self.stop()
        self._exit_code = None
        return self.start(force=True)

    def set_autostart(self, enabled: bool) -> None:
        self.autostart = bool(enabled)
        self._event("autostart", "on" if enabled else "off")

    def reset_backoff(self) -> None:
        self._failures = 0
        self._backoff_until = 0.0

    # ── 自启轮询 ──
    def autostart_tick(self) -> str:
        """一拍：算该做什么并执行；返回动作名（供测试/日志）。"""
        alive = self._proc_alive()
        pres = self._presence()
        env = {}
        try:
            env = self.env_provider() or {}
        except Exception:
            env = {}
        ready = {"ok": True}
        try:
            ready = self.driver_ready_provider() or ready
        except Exception:
            pass
        act = decide_autostart(enabled=self.autostart, proc_alive=alive, presence_alive=bool(pres and pres.get("alive")),
                               wechat_main_window=bool(env.get("main_window")), driver_ok=bool(ready.get("ok", True)),
                               backoff_until=self._backoff_until, now=self._now())
        if act == ACT_START:
            self._event("autostart_start", "wechat main window visible")
            self.start()
        return act

    async def autostart_loop(self, poll_sec: float = 15.0) -> None:
        while True:
            try:
                await asyncio.to_thread(self.autostart_tick)
            except Exception:
                logger.debug("[wechat_pc.supervisor] autostart tick 异常", exc_info=True)
            await asyncio.sleep(max(3.0, float(poll_sec)))

    def ensure_loop(self, poll_sec: float = 15.0) -> None:
        """在当前事件循环里挂自启轮询任务（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_event_loop().create_task(self.autostart_loop(poll_sec))
        except Exception:
            logger.debug("[wechat_pc.supervisor] 自启轮询挂载失败", exc_info=True)

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._proc_alive():
            await asyncio.to_thread(self.stop)

    # ── Windows Job Object：后端无论如何退出都回收子进程（与 local_tts_supervisor 同法） ──
    def _assign_job(self, pid: int) -> None:  # pragma: no cover - 平台相关
        if not pid:
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                            ("WriteOperationCount", ctypes.c_ulonglong),
                            ("OtherOperationCount", ctypes.c_ulonglong),
                            ("ReadTransferCount", ctypes.c_ulonglong),
                            ("WriteTransferCount", ctypes.c_ulonglong),
                            ("OtherTransferCount", ctypes.c_ulonglong)]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                            ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.OpenProcess.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                return
            hproc = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
            if not hproc:
                k32.CloseHandle(job)
                return
            assigned = k32.AssignProcessToJobObject(job, hproc)
            k32.CloseHandle(hproc)
            if not assigned:
                k32.CloseHandle(job)
                return
            self._job = job
        except Exception as ex:
            logger.debug("Job Object 绑定失败（降级为显式 stop）: %s", ex)

    def _kill_tree(self, proc: Any) -> None:
        try:
            if proc.poll() is not None:
                return
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        else:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _close_job(self) -> None:
        if self._job is None:
            return
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(self._job)
        except Exception:
            pass
        self._job = None

    def _close_logf(self) -> None:
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None


__all__ = ["WeChatPcSupervisor", "derive_state", "decide_autostart", "next_backoff", "build_driver_command",
           "STATE_IDLE", "STATE_STARTING", "STATE_ONLINE", "STATE_BLIND", "STATE_OFFLINE", "STATE_ERROR",
           "ACT_SKIP", "ACT_START", "ACT_WAIT_WECHAT", "ACT_BACKOFF", "ACT_NO_DRIVER", "TOKEN_ENV",
           "FROZEN_DRIVER_FLAG",
           "STARTING_GRACE_SEC", "QUICK_EXIT_SEC"]
