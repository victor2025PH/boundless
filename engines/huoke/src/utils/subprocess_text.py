# -*- coding: utf-8 -*-
"""
subprocess 文本模式统一入口：默认 text=True + UTF-8 解码。

Windows 上若仅用 capture_output=True、text=True 而不指定 encoding，CPython 会用系统编码
读子进程管道，易触发 GBK 解码错误。新代码应优先使用本模块的 run，而非裸 subprocess.run。
"""

from __future__ import annotations

import subprocess
from typing import Any

_UTF8_TEXT_DEFAULTS = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """
    等价于 subprocess.run，但合并默认 ``text/encoding/errors``。
    调用方可覆盖任意键（例如 ``text=False`` 以接收 bytes）。
    """
    merged = {**_UTF8_TEXT_DEFAULTS, **kwargs}
    return subprocess.run(*popenargs, **merged)


def run_shell(command: str, **kwargs: Any) -> subprocess.CompletedProcess:
    """
    ``shell=True`` 的单字符串命令（如 ``adb -s …``），同样默认 UTF-8 文本解码。
    """
    merged = {**_UTF8_TEXT_DEFAULTS, "shell": True, **kwargs}
    return subprocess.run(command, **merged)
