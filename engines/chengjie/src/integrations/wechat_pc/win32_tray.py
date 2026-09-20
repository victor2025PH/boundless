# -*- coding: utf-8 -*-
"""任务栏通知区（托盘）图标定位与点击（ctypes，跨进程读 ToolbarWindow32 按钮表）。

为什么需要它（2026-09-19 真机实锤，微信 4.1.13.65）：主窗点 X 收进托盘＝``SW_HIDE``；此后用
``ShowWindow(SW_RESTORE)`` 能把窗口**画**回来，但 Qt 无障碍树是空的（主窗只剩 ``MMUIRenderSubWindowHW``
一个子节点，MSAA/UIA 都拿不到会话列表/输入框），锚点自检永远失败 → 副驾锁只读。**只有走微信自己的
「托盘图标单击 → 显示主窗」路径**，mmui 才重建无障碍根（多出 ``QWidget → QStackedWidget/mmui::TitleBar``
子树）。所以「把微信拉回来」必须能点到托盘图标，而不是只会 ShowWindow。

托盘图标本身没有稳定的 UIA 路径（真机 ``Shell_TrayWnd`` 子树遍历 60s+ 且会挂），这里走经典的
``TB_BUTTONCOUNT / TB_GETBUTTON / TB_GETITEMRECT``：把 ``TBBUTTON`` 读到本进程、``dwData`` 指向的
``TRAYDATA.hwnd`` 归属进程即图标主人。同时扫可见区与溢出区（``NotifyIconOverflowWindow``）；在溢出区的
先点「显示隐藏的图标」箭头再点。任何一步失败都返回 False（调用方回落 ShowWindow）。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

TB_BUTTONCOUNT = 0x0418
TB_GETBUTTON = 0x0417
TB_GETITEMRECT = 0x041D
TB_GETBUTTONTEXTW = 0x044B
TBSTATE_HIDDEN = 0x08
PROCESS_VM_OPERATION, PROCESS_VM_READ, PROCESS_VM_WRITE = 0x0008, 0x0010, 0x0020
MEM_COMMIT, MEM_RELEASE, PAGE_READWRITE = 0x1000, 0x8000, 0x04
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004


@dataclass
class TrayIcon:
    where: str          # visible | overflow
    index: int
    text: str
    owner_pid: int
    hidden: bool
    rect: Tuple[int, int, int, int]   # 屏幕坐标
    toolbar_hwnd: int


if os.name == "nt":  # pragma: no cover - 平台相关
    class _TBBUTTON(ctypes.Structure):
        _fields_ = [("iBitmap", ctypes.c_int), ("idCommand", ctypes.c_int), ("fsState", ctypes.c_ubyte),
                    ("fsStyle", ctypes.c_ubyte), ("bReserved", ctypes.c_ubyte * 6),
                    ("dwData", ctypes.c_size_t), ("iString", ctypes.c_ssize_t)]

    class _TRAYDATA(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("uID", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                    ("Reserved", wintypes.DWORD * 2), ("hIcon", wintypes.HANDLE)]


def _toolbars() -> List[Tuple[str, int]]:
    user32 = ctypes.windll.user32
    out: List[Tuple[str, int]] = []
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None) if tray else 0
    pager = user32.FindWindowExW(notify, None, "SysPager", None) if notify else 0
    tb = user32.FindWindowExW(pager, None, "ToolbarWindow32", None) if pager else 0
    if tb:
        out.append(("visible", int(tb)))
    ov = user32.FindWindowW("NotifyIconOverflowWindow", None)
    tb2 = user32.FindWindowExW(ov, None, "ToolbarWindow32", None) if ov else 0
    if tb2:
        out.append(("overflow", int(tb2)))
    return out


def _scan_toolbar(where: str, tb: int) -> List[TrayIcon]:  # pragma: no cover - 平台相关
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(tb, ctypes.byref(pid))
    hp = kernel32.OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, False, pid.value)
    if not hp:
        return []
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]
    send = user32.SendMessageW
    send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    send.restype = ctypes.c_ssize_t
    out: List[TrayIcon] = []
    remote = kernel32.VirtualAllocEx(hp, None, 4096, MEM_COMMIT, PAGE_READWRITE)
    try:
        if not remote:
            return []
        n = int(send(tb, TB_BUTTONCOUNT, 0, 0) or 0)
        rd = ctypes.c_size_t()
        for i in range(max(0, min(n, 128))):
            send(tb, TB_GETBUTTON, i, remote)
            btn = _TBBUTTON()
            kernel32.ReadProcessMemory(hp, ctypes.c_void_p(remote), ctypes.byref(btn), ctypes.sizeof(btn), ctypes.byref(rd))
            td = _TRAYDATA()
            if btn.dwData:
                kernel32.ReadProcessMemory(hp, ctypes.c_void_p(btn.dwData), ctypes.byref(td), ctypes.sizeof(td), ctypes.byref(rd))
            text = ""
            buf = ctypes.create_unicode_buffer(256)
            if btn.iString and btn.iString != -1:
                kernel32.ReadProcessMemory(hp, ctypes.c_void_p(btn.iString), buf, 510, ctypes.byref(rd))
                text = buf.value
            else:
                send(tb, TB_GETBUTTONTEXTW, btn.idCommand, remote)
                kernel32.ReadProcessMemory(hp, ctypes.c_void_p(remote), buf, 510, ctypes.byref(rd))
                text = buf.value
            opid = wintypes.DWORD()
            if td.hwnd:
                user32.GetWindowThreadProcessId(td.hwnd, ctypes.byref(opid))
            rc = wintypes.RECT()
            send(tb, TB_GETITEMRECT, i, remote)
            kernel32.ReadProcessMemory(hp, ctypes.c_void_p(remote), ctypes.byref(rc), ctypes.sizeof(rc), ctypes.byref(rd))
            p1 = wintypes.POINT(rc.left, rc.top)
            p2 = wintypes.POINT(rc.right, rc.bottom)
            user32.ClientToScreen(tb, ctypes.byref(p1))
            user32.ClientToScreen(tb, ctypes.byref(p2))
            out.append(TrayIcon(where, i, text, int(opid.value), bool(btn.fsState & TBSTATE_HIDDEN),
                                (p1.x, p1.y, p2.x, p2.y), tb))
    finally:
        try:
            if remote:
                kernel32.VirtualFreeEx(hp, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        finally:
            kernel32.CloseHandle(hp)
    return out


def list_tray_icons() -> List[TrayIcon]:
    """可见区 + 溢出区全部通知图标；非 Windows / 任一步失败 → []。"""
    if os.name != "nt":
        return []
    out: List[TrayIcon] = []
    try:
        for where, tb in _toolbars():
            out.extend(_scan_toolbar(where, tb))
    except Exception:
        return out
    return out


def match_icon(icons: Iterable[TrayIcon], *, pids: Iterable[int] = (), names: Iterable[str] = ()) -> Optional[TrayIcon]:
    """按主人进程 pid（优先）或 tooltip 关键词匹配一个图标。"""
    pid_set = {int(p) for p in pids if p}
    name_list = [str(n) for n in names if n]
    for ic in icons:
        if pid_set and ic.owner_pid in pid_set:
            return ic
    for ic in icons:
        if any(n in (ic.text or "") for n in name_list):
            return ic
    return None


def _click_screen(x: int, y: int) -> None:  # pragma: no cover - 平台相关
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _open_overflow() -> bool:  # pragma: no cover - 平台相关
    """点通知区「显示隐藏的图标」箭头把溢出面板弹出来。"""
    user32 = ctypes.windll.user32
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None) if tray else 0
    chevron = user32.FindWindowExW(notify, None, "Button", None) if notify else 0
    if not chevron:
        return False
    rc = wintypes.RECT()
    user32.GetWindowRect(chevron, ctypes.byref(rc))
    _click_screen((rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2)
    time.sleep(0.8)
    return True


def click_tray_icon(*, pids: Iterable[int] = (), names: Iterable[str] = ("微信", "WeChat", "Weixin")) -> bool:
    """左键单击匹配的托盘图标；找不到/失败 False。溢出区图标先展开面板再点。"""
    if os.name != "nt":
        return False
    try:
        ic = match_icon(list_tray_icons(), pids=pids, names=names)
        if ic is None:
            return False
        if ic.where == "overflow":
            if not _open_overflow():
                return False
            ic2 = match_icon(list_tray_icons(), pids=pids, names=names)
            if ic2 is None:
                return False
            ic = ic2
        x1, y1, x2, y2 = ic.rect
        if x2 <= x1 or y2 <= y1:
            return False
        _click_screen((x1 + x2) // 2, (y1 + y2) // 2)
        return True
    except Exception:
        return False


__all__ = ["TrayIcon", "list_tray_icons", "match_icon", "click_tray_icon"]
