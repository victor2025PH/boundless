# -*- coding: utf-8 -*-
"""一键 bump 前端构建戳（ui-build.txt 首行）。

前端批次（模板/静态资源）落地后跑本脚本，替代手工编辑首行。
配套门禁 tests/test_ui_build_freshness.py 会在「前端文件比戳新」时变红并指路此处。
（CSS ?v= 的另一半人工戳将由 admin.py 的 static_v mtime 版号在下次重启后逐步接管。）

用法：python scripts/bump_ui_build.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STAMP = (Path(__file__).resolve().parents[1]
         / "src" / "web" / "static" / "workspace" / "ui-build.txt")


def next_stamp(old: str, now_str: str) -> str:
    """单调递增守卫（2026-08-09 实锤：并行线刚打了 1106 的戳，本脚本按当前时钟
    1101 一跑就把它**回拨**——陈旧页提醒按戳变化判断，回拨会误触/漏触刷新提示）。
    新值 <= 旧值时取「旧值 +1 分钟」；旧值非本格式（历史手写）按当前时间。"""
    if not old or now_str > old:
        return now_str
    try:
        t = datetime.strptime(old.strip(), "%Y%m%d-%H%M") + timedelta(minutes=1)
        return t.strftime("%Y%m%d-%H%M")
    except ValueError:
        return now_str


def main() -> int:
    lines = STAMP.read_text(encoding="utf-8").splitlines()
    old = lines[0] if lines else ""
    new = next_stamp(old, time.strftime("%Y%m%d-%H%M"))
    if not lines:
        lines = [new]
    else:
        lines[0] = new
    STAMP.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ui-build: {old or '(空)'} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
