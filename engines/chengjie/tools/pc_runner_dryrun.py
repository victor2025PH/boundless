# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」交互式会话 dry-run（实施91 P1-1，2026-08-31）。

**在受控机的交互式登录会话里跑**（双击/控制台/RDP，不要在无桌面的服务/CI
上下文）。直调 pc_runner.dispatch —— 已收敛到专属 UIA 工作线程 + 每操作超时，
所以任何上下文都不会挂死：无交互桌面时 probe_desktop 回 False、只读侦察回
uia_timeout，工具照常退出并说明原因。

验收（对齐方案 §5 + 媒体产物纪律）：
  · list_windows 报出的前台窗口应与你肉眼所见一致；
  · read_tree 节点数封顶 <= TREE_MAX_NODES 不卡死；
  · screenshot 落盘后**按 magic bytes 校验 PNG**（不看 HTTP 状态/文件大小）。

用法：
  python tools/pc_runner_dryrun.py            # 自动挑前台窗口
  python tools/pc_runner_dryrun.py --window 记事本
  python tools/pc_runner_dryrun.py --keep     # 保留截图不删
退出码：0=全绿 / 1=读路径失败 / 2=无交互桌面（需登录会话补跑）。
"""
from __future__ import annotations

import argparse
import os
import sys

# 允许从引擎根直接 `python tools/pc_runner_dryrun.py`
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runner import pc_runner as pr  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _p(tag, msg):
    print(("  %-5s " % tag) + msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="", help="目标窗口标题（子串匹配）")
    ap.add_argument("--keep", action="store_true", help="保留截图不删")
    args = ap.parse_args()

    os.environ.setdefault("PC_RUNNER_SHOTS",
                          os.path.join(os.environ.get("TEMP", "."),
                                       "pc_runner_dryrun_shots"))
    print("== pc_runner dry-run ==", flush=True)
    print("version=%s win32=%s uia=%s" % (
        pr.VERSION, pr.win32_available(), pr.uia_available()), flush=True)

    if not pr.win32_available():
        _p("SKIP", "非 Windows —— 窗口枚举/截图不可用")
        return 2

    if not pr.probe_desktop():
        _p("FAIL", "枚举不到窗口：该会话没有活动桌面")
        _p("HINT", "在有活动桌面的会话里跑（无头机需接显示器或有远程会话在渲染）")
        return 2
    _p("PASS", "desktop_ok —— 桌面窗口可枚举（Win32）")

    ok = True

    lw = pr.dispatch("list_windows", {})
    wins = (lw or {}).get("windows") or []
    if not (lw.get("ok") and wins):
        _p("FAIL", "list_windows: %s" % lw)
        return 1
    fg = [w for w in wins if w.get("foreground")]
    fg_title = args.window or (fg[0]["title"] if fg else wins[0]["title"])
    _p("PASS", "list_windows n=%d 前台=%r" % (len(wins), fg_title))
    _p("", "样本: " + " | ".join(w["title"][:22] for w in wins[:6]))

    tr = pr.dispatch("read_tree", {"window": fg_title})
    if tr.get("ok"):
        t = tr.get("tree") or {}
        cap = t.get("count", 0) <= pr.TREE_MAX_NODES
        _p("PASS" if cap else "FAIL",
           "read_tree count=%s truncated=%s cap<=%d=%s" % (
               t.get("count"), t.get("truncated"), pr.TREE_MAX_NODES, cap))
        ok = ok and cap
    else:
        _p("WARN", "read_tree 未成: %s" % tr.get("error"))

    sh = pr.dispatch("screenshot", {"window": fg_title})
    if sh.get("ok"):
        path = sh.get("path") or ""
        magic = b""
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                magic = f.read(8)
        good = magic == PNG_MAGIC
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        _p("PASS" if good else "FAIL",
           "screenshot PNG magic=%r size=%d path=%s" % (magic, size, path))
        ok = ok and good
        if path and os.path.exists(path) and not args.keep:
            try:
                os.remove(path)
            except OSError:
                pass
    else:
        _p("FAIL", "screenshot 未成: %s" % sh.get("error"))
        ok = False

    print("\nDRY-RUN " + ("GREEN" if ok else "RED"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
