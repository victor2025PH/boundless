# -*- coding: utf-8 -*-
"""Win32 顶层窗口枚举（ctypes，毫秒级）——给 UIA 后端与探针定位微信窗口用。

为什么不用 ``uiautomation.GetRootControl().GetChildren()``：真机实测一次要 60–90 秒（桌面上其它
窗口的 UIA provider 响应慢、外加逐窗 psutil 查进程名），服务每 tick 都要找主窗，等不起。
这里只用 ``EnumWindows / GetClassNameW / GetWindowThreadProcessId / IsWindowVisible``，再按
进程名过滤；拿到 hwnd 后由调用方 ``uiautomation.ControlFromHandle`` 进入 UIA。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

WECHAT_PROCESS_NAMES = ("weixin.exe", "wechat.exe")


@dataclass
class TopWindow:
    hwnd: int
    pid: int
    class_name: str
    title: str
    visible: bool
    process: str = ""


def _proc_name(pid: int) -> str:
    try:
        import psutil  # type: ignore
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def enum_top_windows(process_names: Optional[tuple] = None, *, visible_only: bool = False) -> List[TopWindow]:
    """枚举顶层窗口；``process_names`` 给定时只留这些进程的。非 Windows 返回空。"""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    out: List[TopWindow] = []
    names = tuple(n.lower() for n in (process_names or ()))
    pid_cache = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            visible = bool(user32.IsWindowVisible(hwnd))
            if visible_only and not visible:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            p = int(pid.value)
            if names:
                if p not in pid_cache:
                    pid_cache[p] = _proc_name(p)
                if pid_cache[p] not in names:
                    return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            tbuf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, tbuf, 512)
            out.append(TopWindow(int(hwnd), p, buf.value, tbuf.value, visible, pid_cache.get(p, "")))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return out


def find_wechat_windows(*, visible_only: bool = False) -> List[TopWindow]:
    return enum_top_windows(WECHAT_PROCESS_NAMES, visible_only=visible_only)


def find_wechat_main_hwnd() -> int:
    """可见的 ``mmui::MainWindow``（多个时取第一个）；没有返回 0。"""
    for w in find_wechat_windows(visible_only=True):
        if w.class_name == "mmui::MainWindow":
            return w.hwnd
    return 0


def find_wechat_login_hwnd() -> int:
    for w in find_wechat_windows(visible_only=True):
        if w.class_name == "mmui::LoginWindow":
            return w.hwnd
    return 0


__all__ = ["TopWindow", "enum_top_windows", "find_wechat_windows", "find_wechat_main_hwnd",
           "find_wechat_login_hwnd", "WECHAT_PROCESS_NAMES"]
