# -*- coding: utf-8 -*-
"""窗口自截图（Windows，ctypes ``PrintWindow``）——个人微信 PC 副驾判向用。

为什么不用 ``uiautomation.Bitmap.FromControl``：它从**屏幕**抓图，微信主窗被浏览器/别的窗口盖住一角就取到
别人的像素（日常必发生）。``PrintWindow(PW_RENDERFULLCONTENT)`` 让 DWM 把窗口自身内容渲染出来，被遮挡照样正确；
只有最小化时拿不到（调用方据 :func:`is_minimized` 判定为「方向未知」，宁漏不错）。

一次抓整窗、多次采样：``read_visible_messages`` 每轮只截 1 张，再按各气泡矩形取像素。
"""
from __future__ import annotations

import ctypes
import os
from typing import List, Optional, Tuple

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

_WIN = os.name == "nt"
if _WIN:  # pragma: no cover - 平台相关
    from ctypes import wintypes as _wt
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG), ("biHeight", _wt.LONG),
                    ("biPlanes", _wt.WORD), ("biBitCount", _wt.WORD), ("biCompression", _wt.DWORD),
                    ("biSizeImage", _wt.DWORD), ("biXPelsPerMeter", _wt.LONG), ("biYPelsPerMeter", _wt.LONG),
                    ("biClrUsed", _wt.DWORD), ("biClrImportant", _wt.DWORD)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _wt.DWORD * 3)]

    # 句柄都是 64 位指针：不声明 argtypes/restype 时 ctypes 按 32 位 int 传，HDC 一旦超过 2^31 就 OverflowError
    _HANDLE = ctypes.c_void_p
    _user32.IsIconic.argtypes = [_HANDLE]
    _user32.IsIconic.restype = _wt.BOOL
    _user32.GetWindowRect.argtypes = [_HANDLE, ctypes.POINTER(_wt.RECT)]
    _user32.GetWindowRect.restype = _wt.BOOL
    _user32.GetWindowDC.argtypes = [_HANDLE]
    _user32.GetWindowDC.restype = _HANDLE
    _user32.ReleaseDC.argtypes = [_HANDLE, _HANDLE]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [_HANDLE, _HANDLE, _wt.UINT]
    _user32.PrintWindow.restype = _wt.BOOL
    _gdi32.CreateCompatibleDC.argtypes = [_HANDLE]
    _gdi32.CreateCompatibleDC.restype = _HANDLE
    _gdi32.CreateCompatibleBitmap.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = _HANDLE
    _gdi32.SelectObject.argtypes = [_HANDLE, _HANDLE]
    _gdi32.SelectObject.restype = _HANDLE
    _gdi32.BitBlt.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              _HANDLE, ctypes.c_int, ctypes.c_int, _wt.DWORD]
    _gdi32.BitBlt.restype = _wt.BOOL
    _gdi32.GetDIBits.argtypes = [_HANDLE, _HANDLE, _wt.UINT, _wt.UINT, ctypes.c_void_p,
                                 ctypes.POINTER(_BITMAPINFO), _wt.UINT]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.DeleteObject.argtypes = [_HANDLE]
    _gdi32.DeleteObject.restype = _wt.BOOL
    _gdi32.DeleteDC.argtypes = [_HANDLE]
    _gdi32.DeleteDC.restype = _wt.BOOL


def is_minimized(hwnd: int) -> bool:
    if not _WIN or not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(int(hwnd)))
    except Exception:
        return False


class WindowCapture:
    """一张窗口位图（BGRA 顶向下），按**屏幕坐标**取像素，返回 ``0xRRGGBB``。"""

    def __init__(self, left: int, top: int, width: int, height: int, buf: bytes) -> None:
        self.left, self.top, self.width, self.height = left, top, width, height
        self._buf = buf

    @classmethod
    def grab(cls, hwnd: int) -> Optional["WindowCapture"]:
        """抓取整窗；失败/最小化/尺寸为 0 → None。绝不抛。"""
        if not _WIN or not hwnd or is_minimized(hwnd):
            return None
        hwnd = int(hwnd)
        rect = _wt.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0 or w > 20000 or h > 20000:
            return None
        hdc_win = _user32.GetWindowDC(hwnd)
        if not hdc_win:
            return None
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
        hbmp = _gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        out: Optional["WindowCapture"] = None
        try:
            old = _gdi32.SelectObject(hdc_mem, hbmp)
            ok = _user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            if not ok:
                ok = _gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_win, 0, 0, SRCCOPY)
            if ok:
                bmi = _BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h  # 负高＝顶向下
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                bmi.bmiHeader.biCompression = BI_RGB
                buf = ctypes.create_string_buffer(w * h * 4)
                lines = _gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
                if lines == h:
                    out = cls(rect.left, rect.top, w, h, buf.raw)
            _gdi32.SelectObject(hdc_mem, old)
        except Exception:
            out = None
        finally:
            try:
                _gdi32.DeleteObject(hbmp)
                _gdi32.DeleteDC(hdc_mem)
                _user32.ReleaseDC(hwnd, hdc_win)
            except Exception:
                pass
        return out

    def pixel(self, sx: int, sy: int) -> Optional[int]:
        x, y = int(sx) - self.left, int(sy) - self.top
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return None
        i = (y * self.width + x) * 4
        b, g, r = self._buf[i], self._buf[i + 1], self._buf[i + 2]
        return (r << 16) | (g << 8) | b

    def row(self, sy: int, xs: List[int]) -> List[int]:
        out: List[int] = []
        for sx in xs:
            p = self.pixel(sx, sy)
            if p is not None:
                out.append(p)
        return out

    def sample_rect(self, left: int, top: int, right: int, bottom: int,
                    fx: Tuple[float, ...], fy: Tuple[float, ...]) -> List[int]:
        """矩形内按相对位置取样（fx/fy ∈ [0,1]）。"""
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return []
        out: List[int] = []
        for y in fy:
            for x in fx:
                p = self.pixel(left + int(w * x), top + int(h * y))
                if p is not None:
                    out.append(p)
        return out


__all__ = ["WindowCapture", "is_minimized", "PW_RENDERFULLCONTENT"]
