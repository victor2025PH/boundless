#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机端 OpenClaw Agent：轮询主机任务 → 执行（本机 ADB）→ 上报结果。
在 Termux 中运行，需先完成本机 ADB 自连（见 README）。
"""

import os
import sys
import time
import logging
import subprocess
from pathlib import Path

try:
    import yaml
    import requests
except ImportError:
    print("请先安装依赖: pip install -r requirements.txt")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        logger.error("请复制 config.example.yaml 为 config.yaml 并填写配置")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def adb_cmd(config, cmd: str):
    """执行 adb 命令。use_host_adb=True 时用主机 ADB 控制指定 device_id（手机 USB/无线连主机）。"""
    if config.get("use_host_adb"):
        device_id = config.get("device_id") or ""
        if not device_id:
            return False, "use_host_adb 时需配置 device_id"
        full = ["adb", "-s", device_id] + cmd.split()
    else:
        host = config.get("adb_host", "127.0.0.1")
        port = config.get("adb_port", 37123)
        full = ["adb", "-s", f"{host}:{port}"] + cmd.split()
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def ensure_adb_connected(config) -> bool:
    if config.get("use_host_adb"):
        device_id = config.get("device_id") or ""
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        out = (r.stdout or r.stderr or "") if r.returncode == 0 else ""
        if r.returncode != 0 or device_id not in out or "device" not in out:
            logger.warning("主机 ADB 未发现设备 %s，请连接手机", device_id)
            return False
        return True
    ok, out = adb_cmd(config, "devices")
    if not ok:
        return False
    device = f"{config.get('adb_host', '127.0.0.1')}:{config.get('adb_port', 37123)}"
    if device not in out or "device" not in out:
        logger.warning("本机 ADB 未连接，请先执行: adb connect %s", device)
        return False
    return True


def fetch_pending_tasks(config) -> list:
    """从主机拉取本设备待执行任务。"""
    url = config["host_url"].rstrip("/") + "/tasks"
    params = {"device_id": config["device_id"], "status": "pending"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("拉取任务失败: %s", e)
        return []


def report_result(config, task_id: str, success: bool, error: str = "", screenshot_path: str = ""):
    """上报任务结果到主机，任务才不会一直“停”在 pending。"""
    url = config["host_url"].rstrip("/") + f"/tasks/{task_id}/result"
    body = {"success": success, "error": error, "screenshot_path": screenshot_path}
    try:
        r = requests.put(url, json=body, timeout=10)
        if r.status_code in (200, 204):
            logger.info("已上报任务 %s 结果: success=%s", task_id, success)
        else:
            logger.warning("上报结果失败: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.warning("上报结果异常: %s", e)


def execute_telegram_send(config, params: dict):
    # -> (bool, str)
    """执行 telegram_send_message：用本机 ADB 打开 Telegram、搜索用户、发消息。"""
    username = (params.get("username") or params.get("target") or "").strip()
    message = (params.get("message") or "").strip()
    if not username:
        return False, "缺少 username 或 target"

    # 1) 启动 Telegram（按你主机侧包名改）
    ok, _ = adb_cmd(config, "shell am start -n org.telegram.messenger/org.telegram.ui.LaunchActivity")
    if not ok:
        return False, "启动 Telegram 失败"
    time.sleep(2)

    # 2) 点击搜索（坐标需按分辨率调整，这里用常见 720p 参考）
    adb_cmd(config, "shell input tap 700 100")
    time.sleep(1)
    adb_cmd(config, f'shell input text "{username.replace(" ", "%s")}"')
    time.sleep(2)
    adb_cmd(config, "shell input tap 360 300")
    time.sleep(2)

    # 3) 点击输入框并输入（中文需主机侧或本机用剪贴板方案）
    adb_cmd(config, "shell input tap 360 1400")
    time.sleep(0.5)
    # 仅演示英文；中文会失败，需接剪贴板或其它输入方式
    escaped = message.replace(" ", "%s").replace("'", "\\'")
    ok, out = adb_cmd(config, f"shell input text '{escaped}'")
    if not ok:
        return False, f"输入消息失败: {out}"
    time.sleep(0.5)
    adb_cmd(config, "shell input tap 650 1400")
    return True, ""


def run_task(config, task: dict) -> None:
    task_id = task.get("task_id")
    typ = task.get("type")
    params = task.get("params") or {}
    if not task_id or not typ:
        return

    logger.info("执行任务 %s type=%s", task_id, typ)
    success = False
    err = ""

    if typ == "telegram_send_message":
        success, err = execute_telegram_send(config, params)
    else:
        err = f"未知任务类型: {typ}"

    report_result(config, task_id, success, err)


def get_local_serial(config) -> str:
    """从本机 ADB 获取序列号（仅非 use_host_adb 时），用于未配置 device_id 时自动填充。"""
    if config.get("use_host_adb"):
        return ""
    ok, out = adb_cmd(config, "get-serialno")
    if ok and out and "unknown" not in out.lower():
        return out.strip()
    return ""


def main():
    config = load_config()
    device_id = (config.get("device_id") or "").strip()
    if not device_id and ensure_adb_connected(config):
        device_id = get_local_serial(config)
        if device_id:
            logger.info("已从本机 ADB 读取 device_id: %s", device_id)
            config["device_id"] = device_id
    if not device_id:
        logger.error("请在 config.yaml 中填写 device_id，或先完成本机 ADB 连接后由程序自动读取")
        sys.exit(1)

    logger.info("手机端 OpenClaw 启动, device_id=%s", device_id)
    interval = max(5, int(config.get("poll_interval", 10)))

    while True:
        if not ensure_adb_connected(config):
            time.sleep(interval)
            continue
        tasks = fetch_pending_tasks(config)
        for t in tasks:
            run_task(config, t)
        time.sleep(interval)


if __name__ == "__main__":
    main()
