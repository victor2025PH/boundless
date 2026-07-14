#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenClaw 服务包装器 — 统一管理 server.py 的生命周期。

功能:
  1. 启动 server.py 作为子进程
  2. 健康检查 (30s) — 挂了自动拉起
  3. 更新检查 (5min) — 有新版本自动拉取 + 重启
  4. 哨兵文件 (.restart-required) — pull-update 写入后立即重启

用法:
  python service_wrapper.py                     # 默认配置
  python service_wrapper.py --no-auto-update    # 关闭自动更新检查
  python service_wrapper.py --update-interval 600  # 自定义更新检查间隔(秒)
"""

import os
import sys
import time
import json
import signal
import logging
import argparse
import subprocess
import urllib.request

from src.utils.subprocess_text import run as _sp_run_text
import urllib.error
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.openclaw_env import openclaw_port

SENTINEL_FILE = PROJECT_ROOT / ".restart-required"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wrapper] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "service_wrapper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("wrapper")


def find_python() -> str:
    """找到可用的 Python 解释器（排除 WindowsApps 假 python）。"""
    exe = sys.executable
    if exe and os.path.exists(exe) and "WindowsApps" not in exe:
        return exe

    for ver in ("313", "312", "311"):
        for tmpl in [
            rf"C:\Users\Administrator\AppData\Local\Programs\Python\Python{ver}\python.exe",
            rf"C:\Python{ver}\python.exe",
        ]:
            if os.path.exists(tmpl):
                return tmpl

    try:
        r = _sp_run_text(["where", "python"], capture_output=True, timeout=5)
        for line in r.stdout.strip().split("\n"):
            p = line.strip()
            if p and os.path.exists(p) and "WindowsApps" not in p:
                return p
    except Exception:
        pass

    return "python"


def health_check(port: int | None = None, timeout: int = 10) -> bool:
    """检查 server.py 健康状态。"""
    if port is None:
        port = openclaw_port()
    try:
        url = f"http://127.0.0.1:{port}/health"
        resp = urllib.request.urlopen(url, timeout=timeout)
        data = json.loads(resp.read())
        return data.get("status") in ("ok", "degraded")
    except Exception:
        return False


def check_for_updates(coordinator_url: str, timeout: int = 10) -> dict | None:
    """检查 Coordinator 是否有更新包可用。返回 info dict 或 None。"""
    if not coordinator_url:
        return None
    try:
        url = coordinator_url.rstrip("/") + "/cluster/update-package/info"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def pull_update(port: int | None = None, coordinator_url: str = "") -> dict | None:
    """通过本地 API 触发更新拉取（不自动重启，由 wrapper 管理）。"""
    if port is None:
        port = openclaw_port()
    try:
        url = f"http://127.0.0.1:{port}/cluster/pull-update"
        body = json.dumps({
            "coordinator_url": coordinator_url,
            "auto_restart": False,  # wrapper 自己管重启
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read())
    except Exception as e:
        log.error("拉取更新失败: %s", e)
        return None


def load_coordinator_url() -> str:
    """从 cluster.yaml 读取 coordinator_url。"""
    yaml_path = PROJECT_ROOT / "config" / "cluster.yaml"
    if not yaml_path.exists():
        return ""
    try:
        import yaml
        with open(yaml_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("coordinator_url", "")
    except ImportError:
        # 手动解析
        with open(yaml_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("coordinator_url:"):
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    return val
    return ""


def load_local_role() -> str:
    """读取本机角色。"""
    yaml_path = PROJECT_ROOT / "config" / "cluster.yaml"
    if not yaml_path.exists():
        return "standalone"
    try:
        with open(yaml_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("role:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "standalone"


class ServiceWrapper:
    """管理 server.py 子进程的生命周期。"""

    def __init__(self, auto_update: bool = True, update_interval: int = 300,
                 health_interval: int = 30, max_restarts: int = 20,
                 restart_cooldown: int = 60):
        self.python_exe = find_python()
        self.auto_update = auto_update
        self.update_interval = update_interval
        self.health_interval = health_interval
        self.max_restarts = max_restarts
        self.restart_cooldown = restart_cooldown

        self.process: subprocess.Popen | None = None
        self.restart_count = 0
        self.last_restart_time = 0.0
        self.last_update_check = 0.0
        self.last_update_version = ""
        self.running = True
        self.port = openclaw_port()

        self.role = load_local_role()
        self.coordinator_url = load_coordinator_url()

    def start_server(self) -> bool:
        """启动 server.py 子进程。"""
        if self.process and self.process.poll() is None:
            log.warning("server.py 仍在运行 (PID=%d)，先停止", self.process.pid)
            self.stop_server()

        log.info("启动 server.py (Python: %s)", self.python_exe)
        try:
            self.process = subprocess.Popen(
                [self.python_exe, "server.py"],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self.last_restart_time = time.time()
            log.info("server.py 已启动 PID=%d", self.process.pid)

            # 等待服务就绪
            for i in range(20):
                time.sleep(1)
                if health_check(self.port, timeout=3):
                    log.info("健康检查通过，服务就绪")
                    return True

            log.warning("启动后 20s 仍未通过健康检查")
            return self.process.poll() is None  # 进程还在就算部分成功

        except Exception as e:
            log.error("启动 server.py 失败: %s", e)
            return False

    def stop_server(self):
        """优雅停止 server.py。"""
        if not self.process:
            return

        pid = self.process.pid
        log.info("停止 server.py PID=%d", pid)

        # 先尝试优雅终止
        try:
            if sys.platform == "win32":
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGTERM)
        except Exception:
            pass

        # 等待最多 10 秒
        try:
            self.process.wait(timeout=10)
            log.info("server.py 已正常退出")
        except subprocess.TimeoutExpired:
            log.warning("server.py 未在 10s 内退出，强制杀掉")
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass

        # 确保端口释放
        self._kill_port_holder()
        self.process = None

    def _kill_port_holder(self):
        """杀掉占用端口的残留进程。"""
        if sys.platform != "win32":
            return
        try:
            r = _sp_run_text(
                ["netstat", "-aon"],
                capture_output=True, timeout=5,
            )
            for line in r.stdout.split("\n"):
                if f":{self.port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid > 0:
                        _sp_run_text(["taskkill", "/F", "/PID", str(pid)],
                                     capture_output=True, timeout=5)
                        log.info("清除残留进程 PID=%d", pid)
        except Exception:
            pass

    def restart_server(self, reason: str = ""):
        """重启 server.py。"""
        now = time.time()
        elapsed = now - self.last_restart_time

        if elapsed < self.restart_cooldown:
            log.warning("距上次重启仅 %.0fs（冷却期 %ds），跳过",
                        elapsed, self.restart_cooldown)
            return

        self.restart_count += 1
        if self.restart_count > self.max_restarts:
            log.error("已达最大重启次数 %d，停止守护", self.max_restarts)
            self.running = False
            return

        log.info("第 %d 次重启 (原因: %s)", self.restart_count, reason or "unknown")
        self.stop_server()
        time.sleep(3)

        if self.start_server():
            log.info("重启成功")
        else:
            log.error("重启失败")

    def check_sentinel(self) -> bool:
        """检查哨兵文件，存在则触发重启。"""
        if SENTINEL_FILE.exists():
            try:
                reason = SENTINEL_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                reason = "sentinel"
            SENTINEL_FILE.unlink(missing_ok=True)
            log.info("检测到哨兵文件: %s", reason)
            return True
        return False

    def try_auto_update(self) -> bool:
        """检查并拉取更新。返回 True 表示有更新并需要重启。"""
        if not self.auto_update:
            return False

        # Worker 才需要自动从 Coordinator 拉取更新
        if self.role != "worker" or not self.coordinator_url:
            return False

        now = time.time()
        if now - self.last_update_check < self.update_interval:
            return False
        self.last_update_check = now

        info = check_for_updates(self.coordinator_url)
        if not info:
            return False

        remote_version = info.get("version", "")
        remote_ts = info.get("timestamp", "")

        if remote_version and remote_version == self.last_update_version:
            return False  # 版本没变

        log.info("[自动更新] 检测到更新: version=%s ts=%s", remote_version, remote_ts)

        result = pull_update(self.port, self.coordinator_url)
        if result and result.get("ok") and result.get("updated_files", 0) > 0:
            self.last_update_version = remote_version
            log.info("[自动更新] 更新成功: %d 文件, %d KB",
                     result["updated_files"], result.get("size_kb", 0))
            return True

        return False

    def run(self):
        """主循环。"""
        log.info("=" * 50)
        log.info("OpenClaw 服务包装器启动")
        log.info("  角色: %s", self.role)
        log.info("  Python: %s", self.python_exe)
        log.info("  端口: %d", self.port)
        log.info("  自动更新: %s (间隔 %ds)", self.auto_update, self.update_interval)
        log.info("  健康检查: %ds", self.health_interval)
        log.info("=" * 50)

        # 清理残留哨兵文件
        SENTINEL_FILE.unlink(missing_ok=True)

        if not self.start_server():
            log.error("初始启动失败")
            return

        while self.running:
            time.sleep(self.health_interval)

            # 1. 哨兵文件检测 — 最高优先级
            if self.check_sentinel():
                self.restart_count = 0  # OTA重启不计入异常计数
                self.restart_server(reason="OTA更新后重启")
                continue

            # 2. 自动更新检查
            if self.try_auto_update():
                self.restart_count = 0
                self.restart_server(reason="自动更新")
                continue

            # 3. 健康检查
            if self.process and self.process.poll() is not None:
                log.warning("server.py 进程已退出 (code=%s)", self.process.returncode)
                self.restart_server(reason="进程退出")
                continue

            if not health_check(self.port, timeout=10):
                log.warning("健康检查失败")
                # 连续两次失败才重启（避免偶发超时）
                time.sleep(5)
                if not health_check(self.port, timeout=10):
                    self.restart_server(reason="健康检查连续失败")
                continue

            # 一切正常，重置计数
            self.restart_count = 0

        log.info("服务包装器退出")
        self.stop_server()


def main():
    parser = argparse.ArgumentParser(description="OpenClaw 服务包装器")
    parser.add_argument("--no-auto-update", action="store_true",
                        help="关闭自动更新检查")
    parser.add_argument("--update-interval", type=int, default=300,
                        help="更新检查间隔(秒)，默认300")
    parser.add_argument("--health-interval", type=int, default=30,
                        help="健康检查间隔(秒)，默认30")
    parser.add_argument("--max-restarts", type=int, default=20,
                        help="最大连续重启次数，默认20")
    args = parser.parse_args()

    wrapper = ServiceWrapper(
        auto_update=not args.no_auto_update,
        update_interval=args.update_interval,
        health_interval=args.health_interval,
        max_restarts=args.max_restarts,
    )

    # 处理 Ctrl+C
    def on_signal(sig, frame):
        log.info("收到终止信号，正在退出...")
        wrapper.running = False

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    wrapper.run()


if __name__ == "__main__":
    main()
