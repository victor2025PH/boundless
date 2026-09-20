# -*- coding: utf-8 -*-
"""PC 微信主窗 · 打开一个会话后探测聊天区控件树（实施97 线 B 真机联调辅助）。

比 ``wechat_pc_probe.py`` 多一步：点开会话列表第 N 项（默认第 1 项），等待渲染后只导出
``mmui::ChatDetailView`` 子树——标题 / 消息列表 / 输入框 / 发送按钮的真实锚点都在这里。
仅点击会话（等同用户切换聊天，会把该会话标记已读），**不输入、不发送**。

    python scripts/wechat_pc_probe_chat.py            # 点第 1 项
    python scripts/wechat_pc_probe_chat.py --index 1  # 点第 2 项
    python scripts/wechat_pc_probe_chat.py --no-click # 不点，只导出当前聊天区
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.wechat_pc_probe import find_wechat_windows, fmt, walk  # noqa: E402


def _find(root: Any, pred, max_depth: int = 24) -> List[Any]:
    out: List[Any] = []
    stack = [(root, 0)]
    while stack:
        c, d = stack.pop()
        try:
            if pred(c):
                out.append(c)
        except Exception:
            pass
        if d >= max_depth:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            stack.append((k, d + 1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--no-click", action="store_true")
    ap.add_argument("--out", default=os.path.join("logs", "wechat_pc_chat_tree.txt"))
    args = ap.parse_args(argv)
    wins = [w for w in find_wechat_windows() if str(w.ClassName or "") == "mmui::MainWindow"]
    if not wins:
        print("未找到微信主窗口（未登录？）", file=sys.stderr)
        return 1
    main_win = wins[0]
    if not args.no_click:
        lists = _find(main_win, lambda c: getattr(c, "AutomationId", "") == "session_list")
        if not lists:
            print("未找到 session_list", file=sys.stderr)
            return 1
        items = [c for c in lists[0].GetChildren() if getattr(c, "ControlTypeName", "") == "ListItemControl"]
        if args.index >= len(items):
            print(f"会话数 {len(items)} 不足 index={args.index}", file=sys.stderr)
            return 1
        target = items[args.index]
        print("点击会话:", repr(str(target.Name or "").split("\n")[0]), "aid=", target.AutomationId)
        target.Click(simulateMove=True)
        time.sleep(1.5)
    details = _find(main_win, lambda c: str(getattr(c, "ClassName", "") or "") == "mmui::ChatDetailView")
    root = details[0] if details else main_win
    lines: List[str] = []
    nodes: List[dict] = []
    walk(root, 0, 26, lines, nodes, [20000])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    types = {}
    for n in nodes:
        types[n["type"]] = types.get(n["type"], 0) + 1
    print(f"聊天区节点 {len(nodes)}，类型分布 {types}")
    print(f"控件树 → {args.out}")
    # 关键锚点候选
    for n in nodes:
        if n["type"] in ("EditControl", "ListControl") or (n["type"] == "ButtonControl" and n["name"] in ("发送", "发送(S)", "Send")):
            print("  锚点候选:", n["type"], repr(n["name"]), "aid=", n["aid"], "cls=", n["cls"], "rect=", n["rect"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
