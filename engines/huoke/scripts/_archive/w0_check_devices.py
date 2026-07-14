# -*- coding: utf-8 -*-
"""
W0-1: 检查设备在线状态，并验证每台设备是否已登录 Facebook。

用法:
  cd d:\mobile-auto-0327\mobile-auto-project
  $env:PYTHONPATH = "$pwd"
  python scripts/w0_check_devices.py

输出:
  - 每台设备的在线状态
  - FB App 是否已安装并处于前台/后台
  - 当前显示的 Activity（是否在 FB 主页）
  - 结果写入 data/w0_device_check.json
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("w0_check")

ADB = r"C:\platform-tools\adb.exe"
FB_PACKAGE = "com.facebook.katana"
OUT_FILE = Path(__file__).parent.parent / "data" / "w0_device_check.json"


def adb(serial: str, *args) -> str:
    """执行 adb 命令，返回 stdout 字符串。"""
    cmd = [ADB, "-s", serial] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace")
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR:{e}"


def get_devices() -> list[str]:
    r = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
    lines = r.stdout.strip().splitlines()
    serials = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def check_fb_status(serial: str) -> dict:
    result = {
        "serial": serial,
        "online": True,
        "fb_installed": False,
        "fb_running": False,
        "current_activity": "",
        "screen_on": False,
        "model": "",
        "android_version": "",
    }

    # 型号
    result["model"] = adb(serial, "shell", "getprop", "ro.product.model")
    result["android_version"] = adb(serial, "shell", "getprop", "ro.build.version.release")

    # 屏幕是否点亮
    power_out = adb(serial, "shell", "dumpsys", "power")
    result["screen_on"] = "mWakefulness=Awake" in power_out

    # FB 是否安装
    pkg_out = adb(serial, "shell", "pm", "list", "packages", FB_PACKAGE)
    result["fb_installed"] = FB_PACKAGE in pkg_out

    # 当前 Activity
    act_out = adb(serial, "shell", "dumpsys", "activity", "activities")
    for line in act_out.splitlines():
        if "mResumedActivity" in line or "ResumedActivity" in line:
            result["current_activity"] = line.strip()
            break

    # FB 是否在前台/后台运行
    if result["fb_installed"]:
        ps_out = adb(serial, "shell", "ps", "-A")
        result["fb_running"] = FB_PACKAGE in ps_out

    # 判断 FB 是否在前台
    result["fb_foreground"] = FB_PACKAGE in result["current_activity"]

    # 简单判断是否可能已登录（FB 主页 Activity）
    result["likely_logged_in"] = (
        result["fb_running"] and
        ("HomeActivity" in result["current_activity"] or
         "FeedFragment" in result["current_activity"] or
         "MainActivity" in result["current_activity"])
    )

    return result


def wake_screen(serial: str):
    """唤醒屏幕。"""
    adb(serial, "shell", "input", "keyevent", "224")
    time.sleep(0.5)
    adb(serial, "shell", "input", "keyevent", "82")
    time.sleep(0.5)


def launch_fb(serial: str):
    """启动 FB App。"""
    adb(serial, "shell", "monkey", "-p", FB_PACKAGE,
        "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(3)


def main():
    log.info("=== W0-1: 设备检查开始 ===")
    devices = get_devices()

    if not devices:
        log.error("没有找到在线设备！请检查 USB 连接。")
        sys.exit(1)

    log.info("发现设备: %s", devices)
    results = []

    for serial in devices:
        log.info("--- 检查设备: %s ---", serial)
        wake_screen(serial)
        time.sleep(0.5)

        status = check_fb_status(serial)
        log.info("  型号: %s  Android: %s", status["model"], status["android_version"])
        log.info("  FB 已安装: %s  FB 运行中: %s  FB 前台: %s",
                 status["fb_installed"], status["fb_running"], status["fb_foreground"])
        log.info("  可能已登录: %s", status["likely_logged_in"])
        log.info("  当前 Activity: %s", status["current_activity"][:120])

        if status["fb_installed"] and not status["fb_running"]:
            log.info("  → 启动 FB...")
            launch_fb(serial)
            time.sleep(2)
            # 重新检查
            status2 = check_fb_status(serial)
            status["fb_running"] = status2["fb_running"]
            status["fb_foreground"] = status2["fb_foreground"]
            status["current_activity"] = status2["current_activity"]
            status["likely_logged_in"] = status2["likely_logged_in"]
            log.info("  启动后 FB 运行中: %s  前台: %s", status["fb_running"], status["fb_foreground"])

        results.append(status)

    # 汇总
    ready = [r for r in results if r["fb_installed"] and r["fb_running"]]
    log.info("\n=== 汇总 ===")
    log.info("总设备数: %d  FB 可用: %d", len(results), len(ready))
    for r in results:
        emoji = "✅" if (r["fb_installed"] and r["fb_running"]) else "❌"
        log.info("  %s %s  (%s / Android %s)  登录可能: %s",
                 emoji, r["serial"], r["model"], r["android_version"],
                 "是" if r.get("likely_logged_in") else "未知/否")

    # 写入文件
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "checked_at": datetime.now().isoformat(),
        "total_devices": len(results),
        "fb_ready_count": len(ready),
        "devices": results,
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("结果已写入: %s", OUT_FILE)

    if not ready:
        log.error("没有可用的 FB 设备！请先手动登录 Facebook。")
        sys.exit(1)

    log.info("\n推荐使用设备（首选）: %s", ready[0]["serial"])
    return ready


if __name__ == "__main__":
    main()
