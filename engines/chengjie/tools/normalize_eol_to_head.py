"""把工作树文件的换行符改回与 HEAD 一致（编辑器把 LF 文件存成 CRLF 时整文件 diff 的止血）。

用法：python tools/normalize_eol_to_head.py <path> [<path> ...]
新文件（HEAD 无此路径）默认写 LF。只改换行符，不碰任何内容。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _head_style(rel: str) -> str:
    try:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return "lf"
    return "crlf" if b"\r\n" in blob else "lf"


def main(paths: list[str]) -> int:
    root = Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True,
        text=True, check=True).stdout.strip())
    changed = 0
    for p in paths:
        fp = Path(p).resolve()
        rel = fp.relative_to(root).as_posix()
        style = _head_style(rel)
        data = fp.read_bytes()
        norm = data.replace(b"\r\n", b"\n")
        if style == "crlf":
            norm = norm.replace(b"\n", b"\r\n")
        if norm != data:
            fp.write_bytes(norm)
            changed += 1
            print(f"{rel}: -> {style.upper()}")
        else:
            print(f"{rel}: ok ({style.upper()})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
