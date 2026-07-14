# -*- coding: utf-8 -*-
"""
统一输入法 — 把所有手机切到 ATX 自带的 ADBKeyboard。

为什么：自动化需要让 ADB 能输入中日文。`adb shell input text` 只支持 ASCII，
unicode 必须走 IME。ADBKeyboard (com.github.uiautomator/.AdbKeyboard) 接收
广播 `ADB_INPUT_TEXT` 输入任意 unicode 文字。

为什么 IME 不会自动统一：uiautomator2.connect() 装 ATX agent 但不切 IME。
项目老代码 `_try_adb_keyboard` 检查的是旧包名 `com.android.adbkeyboard`，
不识别新包 `com.github.uiautomator/.AdbKeyboard`。

MIUI 14+ 安装坑：
  1. INSTALL_FAILED_USER_RESTRICTED — securitycenter 拦截弹 AdbInstallActivity，
     ADB 不能交互→自动取消→报 USER_RESTRICTED。
     绕开：先 `pm hide com.miui.securitycenter`（即使 SecurityException 也会触发
     一次"暂停状态"，install 在窗口期成功）。
  2. Unknown input method — 装上后 IMMS 缓存没识别新 IME。
     绕开：直接写 `secure.enabled_input_methods` 字段。

用法：
  单台:   python scripts/unify_ime.py <serial>
  全集群: python scripts/unify_ime.py --all
"""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

_ATX_PKG = "com.github.uiautomator"
_ATX_IME = "com.github.uiautomator/.AdbKeyboard"
_REMOTE_TMP = "/data/local/tmp/atx-agent.apk"

# Gboard 作为 fallback（ADBKeyboard 不能渲染表情/复杂字形时用户手动切）
_GBOARD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"


def _adb(serial: str, cmd: str, timeout: int = 15) -> Tuple[bool, str]:
    cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", cmd],
            capture_output=True, text=True, timeout=timeout, creationflags=cf,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _adb_push(serial: str, local: str, remote: str, timeout: int = 30) -> Tuple[bool, str]:
    cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "push", local, remote],
            capture_output=True, text=True, timeout=timeout, creationflags=cf,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _find_atx_apk() -> Optional[str]:
    try:
        import uiautomator2 as _u2
        apk = os.path.join(os.path.dirname(_u2.__file__), "assets", "app-uiautomator.apk")
        return apk if os.path.exists(apk) else None
    except Exception:
        return None


def _is_atx_installed(serial: str) -> bool:
    ok, out = _adb(serial, f"pm list packages {_ATX_PKG}")
    return ok and _ATX_PKG in out


def _install_atx(serial: str) -> Tuple[bool, str]:
    """装 ATX agent。先 pm hide securitycenter（虽然报 SecurityException 但触发
    pause window），然后 push + pm install。"""
    apk = _find_atx_apk()
    if not apk:
        return False, "ATX APK 未找到（uiautomator2 未安装？）"

    ok, out = _adb_push(serial, apk, _REMOTE_TMP)
    if not ok:
        return False, f"push 失败: {out[:120]}"

    # 触发 securitycenter pause window
    _adb(serial, "pm hide com.miui.securitycenter", timeout=5)
    _adb(serial, "am force-stop com.miui.securitycenter", timeout=5)

    ok, out = _adb(serial, f"pm install -r -t {_REMOTE_TMP}", timeout=60)

    _adb(serial, "pm unhide com.miui.securitycenter", timeout=5)
    _adb(serial, f"rm -f {_REMOTE_TMP}", timeout=5)

    if "Success" in out:
        return True, "Success"
    return False, out[:200] or "unknown failure"


def _set_default_ime(serial: str) -> Tuple[bool, str]:
    """直接写 secure.enabled_input_methods 绕开 IMMS 缓存，再 ime set。"""
    enabled = f"{_ATX_IME}:{_GBOARD}"
    _adb(serial, f"settings put secure enabled_input_methods '{enabled}'")
    ok, out = _adb(serial, f"ime set {_ATX_IME}")
    if not ok or "selected" not in out.lower():
        return False, out[:200] or "ime set failed"

    ok, current = _adb(serial, "settings get secure default_input_method")
    return (ok and _ATX_IME in current), current.strip()


def unify(serial: str) -> dict:
    """对单台设备执行：装 ATX → 启用 + 设默认 IME。返回结果 dict。"""
    result = {"serial": serial, "atx_installed": False, "ime_set": False, "default_ime": "", "msg": ""}

    if _is_atx_installed(serial):
        result["atx_installed"] = True
    else:
        ok, msg = _install_atx(serial)
        if not ok:
            result["msg"] = f"ATX 安装失败: {msg}"
            return result
        result["atx_installed"] = True

    ok, current = _set_default_ime(serial)
    result["ime_set"] = ok
    result["default_ime"] = current
    if not ok:
        result["msg"] = f"IME 设置失败: {current}"
    else:
        result["msg"] = "OK"
    return result


def unify_quietly(serial: str) -> bool:
    """供 host 集成：只返回 bool，不打印不抛异常。"""
    try:
        r = unify(serial)
        return bool(r.get("ime_set"))
    except Exception:
        return False


def _list_serials() -> Iterable[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            yield parts[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("serial", nargs="?")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not args.all and not args.serial:
        ap.print_help()
        return 1

    serials = list(_list_serials()) if args.all else [args.serial]
    if not serials:
        print("没有在线设备")
        return 1

    rc = 0
    for s in serials:
        r = unify(s)
        mark = "OK " if r["ime_set"] else "FAIL"
        print(f"[{mark}] {s[:8]}  ime={r['default_ime'] or '?'}  ({r['msg']})")
        if not r["ime_set"]:
            rc = 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
