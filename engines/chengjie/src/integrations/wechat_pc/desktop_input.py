# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 桌面输入互斥（跨进程）。

双开微信＝两个驾驶子进程，但鼠标/键盘/剪贴板/前台窗口只有一套：A 号刚 ``SetActive``+点输入框，
B 号紧接着点了自己的会话格，A 的 ``Ctrl+V`` 就贴进 B 的聊天——这正是「两个微信同时对话会混乱」
的机器侧根因（2026-09-21 真机双开实锤：两驱动各自 ~3s tick 互相抢前台）。

这里用 Windows 命名互斥量（进程死了自动释放，不会留死锁）把**一段**连续的桌面交互包起来：
开会话→读消息、五步发送、语音录制、点头像读资料卡。同线程可重入。非 Windows / 单测退化为进程内 RLock。
等锁上限 ``timeout`` 秒（语音一条最长约 60s），超时不死等：记警告后照做，宁可偶发抢一次，不可整条线卡死。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\chengjie_wechat_pc_desktop_input"
DEFAULT_TIMEOUT_SEC = 90.0

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


class DesktopInputLock:
    """``with lock:`` 包住一段桌面交互。同进程同线程可重入；跨进程靠命名互斥量。"""

    def __init__(self, name: str = MUTEX_NAME, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.name = name
        self.timeout = float(timeout)
        self._local = threading.RLock()
        self._depth = 0
        self._handle: Optional[int] = None
        self._owned = False            # 最外层进入时是否真的持有互斥量（超时放行＝False，出块不 Release）
        self.waited_sec = 0.0          # 最近一次等锁用时（心跳/排障用）
        self.timeouts = 0              # 等锁超时次数

    def _ensure_handle(self) -> Optional[int]:
        if os.name != "nt":
            return None
        if self._handle is None:
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = ctypes.c_void_p
            h = k32.CreateMutexW(None, False, self.name)
            self._handle = int(h) if h else None
        return self._handle

    def acquire(self) -> bool:
        """返回 True＝拿到了全局锁；False＝等超时后放行（或没有互斥量可用）。"""
        self._local.acquire()
        self._depth += 1
        if self._depth > 1:
            return self._owned
        h = self._ensure_handle()
        if h is None:
            self._owned = False
            return False
        t0 = time.monotonic()
        rc = ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(h), int(self.timeout * 1000))
        self.waited_sec = time.monotonic() - t0
        self._owned = rc in (_WAIT_OBJECT_0, _WAIT_ABANDONED)
        if self._owned:
            if self.waited_sec > 1.0:
                logger.info("[wechat_pc.desktop] 等另一个驾驶让出桌面 %.1fs", self.waited_sec)
        else:
            self.timeouts += 1
            logger.warning("[wechat_pc.desktop] 等桌面输入锁超时 %.0fs（rc=%s），照常操作", self.timeout, rc)
        return self._owned

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._owned and self._handle is not None:
            try:
                ctypes.windll.kernel32.ReleaseMutex(ctypes.c_void_p(self._handle))
            except Exception:
                pass
            self._owned = False
        self._local.release()

    def __enter__(self) -> "DesktopInputLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


DESKTOP_INPUT = DesktopInputLock()


def desktop_input() -> DesktopInputLock:
    """全局单例：``with desktop_input(): ...``"""
    return DESKTOP_INPUT


__all__ = ["DesktopInputLock", "DESKTOP_INPUT", "desktop_input", "MUTEX_NAME", "DEFAULT_TIMEOUT_SEC"]
