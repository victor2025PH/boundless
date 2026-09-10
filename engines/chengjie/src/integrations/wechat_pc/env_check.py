# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 接入引导第 ① 步「环境检测」（Windows）。

只用 Win32（EnumWindows / 进程映像路径 / 文件版本信息），不碰 UIA——引导页每几秒轮询一次，不能拖慢微信。
返回 ``{os_windows, running, main_window, login_window, version, version_ok, exe_path, pid}``；
``version_ok`` = 主版本 ≥ 4（4.x 的 ``mmui::*`` 锚点是驱动的前提）。纯函数 :func:`parse_version` / :func:`version_ok` 可单测。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Tuple

MIN_MAJOR = 4
_VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: Any) -> Tuple[int, ...]:
    m = _VER_RE.search(str(text or ""))
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def version_ok(text: Any, min_major: int = MIN_MAJOR) -> bool:
    v = parse_version(text)
    return bool(v) and v[0] >= int(min_major)


def _exe_path_of(pid: int) -> str:  # pragma: no cover - 平台相关
    if os.name != "nt" or not pid:
        return ""
    import ctypes
    from ctypes import wintypes as wt
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def _file_version(path: str) -> str:  # pragma: no cover - 平台相关
    if os.name != "nt" or not path or not os.path.exists(path):
        return ""
    import ctypes
    from ctypes import wintypes as wt
    ver = ctypes.windll.version
    ver.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = wt.DWORD
    ver.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    ver.GetFileVersionInfoW.restype = wt.BOOL
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
    ver.VerQueryValueW.restype = wt.BOOL
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return ""
    ptr = ctypes.c_void_p()
    ln = wt.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)) or not ptr.value:
        return ""

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [("dwSignature", wt.DWORD), ("dwStrucVersion", wt.DWORD),
                    ("dwFileVersionMS", wt.DWORD), ("dwFileVersionLS", wt.DWORD),
                    ("dwProductVersionMS", wt.DWORD), ("dwProductVersionLS", wt.DWORD)]

    info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    ms, ls = info.dwProductVersionMS, info.dwProductVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def check_environment() -> Dict[str, Any]:
    """真机环境快照；非 Windows 或任何异常都给出结构完整的「否」答案，绝不抛。"""
    out: Dict[str, Any] = {"os_windows": os.name == "nt", "running": False, "main_window": False,
                           "login_window": False, "version": "", "version_ok": False, "exe_path": "", "pid": 0}
    if os.name != "nt":
        return out
    try:
        from src.integrations.wechat_pc.win32_windows import find_wechat_windows
        wins = find_wechat_windows(visible_only=False)
    except Exception:
        wins = []
    if not wins:
        return out
    out["running"] = True
    pid = 0
    for w in wins:
        if w.class_name.startswith("Qt") and w.visible:
            pid = pid or int(w.pid)
            if str(w.title) == "微信" or str(w.title).lower() == "wechat":
                out["main_window"] = True
            elif str(w.title).lower() in ("weixin", "登录", "login"):
                out["login_window"] = True
    if not pid:
        pid = int(wins[0].pid)
    out["pid"] = pid
    try:
        path = _exe_path_of(pid)
        out["exe_path"] = path
        out["version"] = _file_version(path)
        out["version_ok"] = version_ok(out["version"])
    except Exception:
        pass
    # 主窗与登录窗都不可见但进程在：多半是最小化到托盘或正在启动
    return out


__all__ = ["MIN_MAJOR", "parse_version", "version_ok", "check_environment"]
