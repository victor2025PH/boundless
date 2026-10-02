"""控制台文本编码（P2-2：173 经 ssh 查 status 乱码）。

两头不一致造成乱码：
- Windows 自带命令（schtasks 等）写进管道时用 OEM 代码页（中文系统 936 / GBK）；
- ssh 会话里 Python 的 stdout 默认也按 GBK 输出（源码态 ``python -m src.fleet.agent status``），
  而打包的 exe 已改成 UTF-8。
统一成：读子进程输出时先试 UTF-8、不行按 OEM 代码页解码；我们自己的 stdout/stderr 一律 UTF-8。
"""

from __future__ import annotations

import locale
import logging
import os
import sys
from typing import Optional, Union

logger = logging.getLogger(__name__)


def oem_encoding() -> str:
    if os.name == "nt":
        try:
            import ctypes

            return "cp%d" % int(ctypes.windll.kernel32.GetOEMCP())
        except (AttributeError, OSError, ValueError):
            logger.debug("[textio] GetOEMCP failed", exc_info=True)
    return locale.getpreferredencoding(False) or "utf-8"


def decode_console_bytes(data: Union[bytes, str, None], *, fallback: Optional[str] = None) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    enc = fallback or oem_encoding()
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def utf8_stdio() -> None:
    """stdout / stderr 改成 UTF-8（已经是就不动）。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        if str(getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            logger.debug("[textio] reconfigure stdio failed", exc_info=True)
