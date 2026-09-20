# -*- coding: utf-8 -*-
"""一键 bump 前端构建戳（ui-build.txt 首行）。

前端批次（模板/静态资源）落地后跑本脚本，替代手工编辑首行。
配套门禁 tests/test_ui_build_freshness.py 会在「前端文件比戳新」时变红并指路此处。
（CSS ?v= 的另一半人工戳将由 admin.py 的 static_v mtime 版号在下次重启后逐步接管。）

用法：
    python scripts/bump_ui_build.py                      # 默认 notice
    python scripts/bump_ui_build.py --severity silent    # 不打扰坐席
    python scripts/bump_ui_build.py --severity urgent    # 提示不自动消失

severity（第二行 ``severity=...``，2026-08-28 加）＝**这批要不要打扰坐席**，由发布者
显式决定，三档语义（消费方：workspace_base / base.html / copilot app.html 三处探针）：

* ``silent``：不出任何提示。坐席空闲（页面隐藏满 1 分钟且无未发送媒体）时静默换版，
  否则等下一次自然导航。**改了坐席看不见的东西就该用它**（ops 卡、后台某页样式…）。
* ``notice``（缺省）：右下胶囊常驻 +「立即更新」按钮 + 一张 20 秒卡片。日常前端批次。
* ``urgent``：卡片不自动消失，胶囊转琥珀。只给「不更新会继续踩坑」的修复用
  （发不出消息、点了没反应这类），滥用＝坐席学会无视它。

背景：一天可以 bump 8 次，而绝大多数批次与坐席当下在用的页面无关——没有分级时
每次都打断人，最终结果是所有人都点「暂不」，提醒机制自我作废。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STAMP = (Path(__file__).resolve().parents[1]
         / "src" / "web" / "static" / "workspace" / "ui-build.txt")

SEVERITIES = ("silent", "notice", "urgent")
_SEV_RE = re.compile(r"^\s*severity\s*=\s*(\w+)\s*$", re.I)


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


def apply_severity(lines: list[str], severity: str) -> list[str]:
    """把 ``severity=<档>`` 收敛成恰好一行、且紧跟构建戳（纯函数，便于门禁校验）。

    存量行原地改写（不搬位置也不重复登记），没有则插在第二行；其余注释行原样保留。
    """
    out = [ln for ln in lines if not _SEV_RE.match(ln or "")]
    head, rest = (out[:1], out[1:]) if out else ([], [])
    return head + [f"severity={severity}"] + rest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="bump 前端构建戳")
    ap.add_argument("--severity", choices=SEVERITIES, default="notice",
                    help="这批要不要打扰坐席：silent=不提示只在空闲换版 / "
                         "notice=常驻胶囊+卡片（缺省）/ urgent=卡片不自动消失")
    args = ap.parse_args(argv)

    lines = STAMP.read_text(encoding="utf-8").splitlines()
    old = lines[0] if lines else ""
    new = next_stamp(old, time.strftime("%Y%m%d-%H%M"))
    if not lines:
        lines = [new]
    else:
        lines[0] = new
    lines = apply_severity(lines, args.severity)
    STAMP.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ui-build: {old or '(空)'} -> {new}  severity={args.severity}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
