"""PyInstaller 入口：agent.py 用包内相对导入，不能直接当脚本打。"""

import sys

from src.fleet.agent import main


def _utf8_console() -> None:
    """非 UTF-8 代码页（英文 Windows 默认 cp1252 / 中文 cp936）下打印中文帮助不至于崩。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


if __name__ == "__main__":
    _utf8_console()
    sys.exit(main())
