# -*- coding: utf-8 -*-
"""
APK 安全安装工具 — 绕开 MIUI 14+ 的手机管家拦截。

MIUI 14/15 会把从 PC 端发起的 `adb install` 路由到
`com.miui.securitycenter/com.miui.permcenter.install.AdbInstallActivity`，
拉起手机管家自检并关闭 USB 调试。改用 `adb push + adb shell pm install`
走 shell 内 PackageManagerService 通道，完全绕开该拦截。

详见 memory: miui_security_popup_kill.md
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Tuple

log = logging.getLogger(__name__)

_REMOTE_TMP = "/data/local/tmp"


def safe_install_apk(adb_path: str, serial: str, local_apk: str,
                     replace: bool = True, test: bool = False,
                     downgrade: bool = False,
                     timeout: int = 120) -> Tuple[bool, str]:
    """用 push + pm install 替代 adb install。

    Args:
        adb_path: adb 可执行路径（通常 "adb"）
        serial: 设备序列号
        local_apk: 本地 APK 路径
        replace: 是否传 -r（覆盖安装）
        test: 是否传 -t（允许测试包，需 -t 标记的 APK）
        downgrade: 是否传 -d（允许降级）
        timeout: 单步 pm install 超时

    Returns:
        (success, message)。success=True 表示 pm install 返回 Success。
    """
    if not os.path.exists(local_apk):
        return False, f"本地 APK 不存在: {local_apk}"

    cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    remote = f"{_REMOTE_TMP}/{os.path.basename(local_apk)}"

    try:
        r = subprocess.run(
            [adb_path, "-s", serial, "push", local_apk, remote],
            capture_output=True, text=True, timeout=min(60, timeout),
            creationflags=cf,
        )
        if r.returncode != 0:
            return False, f"push 失败: {(r.stdout + r.stderr).strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, "push 超时"
    except Exception as e:
        return False, f"push 异常: {e}"

    flags = []
    if replace:   flags.append("-r")
    if test:      flags.append("-t")
    if downgrade: flags.append("-d")

    out = ""
    rc = -1
    try:
        r = subprocess.run(
            [adb_path, "-s", serial, "shell", "pm", "install"] + flags + [remote],
            capture_output=True, text=True, timeout=timeout, creationflags=cf,
        )
        rc = r.returncode
        out = (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        out = "pm install 超时"
    except Exception as e:
        out = f"pm install 异常: {e}"

    try:
        subprocess.run(
            [adb_path, "-s", serial, "shell", "rm", "-f", remote],
            capture_output=True, timeout=5, creationflags=cf,
        )
    except Exception:
        pass

    if rc == 0 and "Success" in out:
        return True, "Success"
    return False, out[:200] or f"pm install rc={rc}"
