# -*- coding: utf-8 -*-
"""PC 微信（Windows 4.x / 3.9.x）UIAutomation 控件树探针（实施97 线 B，2026-09-07）。

用途：个人微信 PC 副驾走「读屏 + 模拟键入」，前提是客户端把控件树暴露给了 UIA——LINE for PC
就没有（2026-07 实测零语义句柄，被迫改协议库）。本脚本回答四个问题，**全程只读**（不点击、
不输入、不发送）：

1. 会话列表 / 消息气泡 / 输入框 / 发送按钮 是否有语义句柄（ControlType + Name）；
2. 4.x「实时渲染」下一屏能读到多少条气泡（可见消息节点数）；
3. 控件树里能否直接读到联系人「微信号」等稳定身份字段（否则驱动需点开资料卡）；
4. 登录窗口（未登录时）是否同样暴露——决定「掉线检测」能否靠 UIA 判定。

用法（仓库根目录，Windows）::

    python scripts/wechat_pc_probe.py                      # 自动找 Weixin.exe/WeChat.exe 顶层窗口
    python scripts/wechat_pc_probe.py --depth 18 --max-nodes 12000
    python scripts/wechat_pc_probe.py --out logs/wechat_pc_tree.txt
    python scripts/wechat_pc_probe.py --json logs/wechat_pc_probe.json   # 结构化摘要（驱动锚点用）

产出：控件树文本 + 摘要（按 ControlType 计数、候选锚点、四项判定）。把 ``--json`` 产物交给
``src/integrations/wechat_pc/uia_backend.py`` 的锚点表即可开始绑定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

WECHAT_PROCESS_NAMES = ("weixin.exe", "wechat.exe")
#: 4.x/3.x 已知窗口类名（不全；进程名兜底）
WECHAT_CLASS_HINTS = ("Qt", "WeChatMainWndForPC", "mmui")


def _proc_name(pid: int) -> str:
    try:
        import psutil  # type: ignore
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def find_wechat_windows() -> List[Any]:
    import uiautomation as auto  # type: ignore
    wins: List[Any] = []
    root = auto.GetRootControl()
    for w in root.GetChildren():
        try:
            pid = int(w.ProcessId or 0)
            name = str(w.Name or "")
            cls = str(w.ClassName or "")
        except Exception:
            continue
        pname = _proc_name(pid) if pid else ""
        if pname in WECHAT_PROCESS_NAMES:
            wins.append(w)
        elif name in ("微信", "WeChat", "Weixin") or (cls and any(h in cls for h in WECHAT_CLASS_HINTS) and "微信" in name):
            wins.append(w)
    return wins


def fmt(ctrl: Any) -> str:
    try:
        rect = ctrl.BoundingRectangle
        r = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
    except Exception:
        r = "()"
    parts = [
        getattr(ctrl, "ControlTypeName", "") or "",
        f"Name={ctrl.Name!r}" if getattr(ctrl, "Name", "") else "",
        f"AutomationId={ctrl.AutomationId!r}" if getattr(ctrl, "AutomationId", "") else "",
        f"ClassName={ctrl.ClassName!r}" if getattr(ctrl, "ClassName", "") else "",
        f"rect={r}",
    ]
    return " ".join(p for p in parts if p)


def walk(ctrl: Any, depth: int, max_depth: int, lines: List[str], nodes: List[Dict[str, Any]],
         cap: List[int], path: str = "0") -> None:
    if depth > max_depth or cap[0] <= 0:
        return
    lines.append("  " * depth + fmt(ctrl))
    cap[0] -= 1
    try:
        rect = ctrl.BoundingRectangle
        rect_t = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        rect_t = (0, 0, 0, 0)
    nodes.append({
        "path": path, "depth": depth,
        "type": str(getattr(ctrl, "ControlTypeName", "") or ""),
        "name": str(getattr(ctrl, "Name", "") or ""),
        "aid": str(getattr(ctrl, "AutomationId", "") or ""),
        "cls": str(getattr(ctrl, "ClassName", "") or ""),
        "rect": rect_t,
    })
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for i, ch in enumerate(children):
        walk(ch, depth + 1, max_depth, lines, nodes, cap, f"{path}.{i}")


def summarize(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从节点表推四项判定（启发式；真机上按输出人工复核）。"""
    by_type: Dict[str, int] = {}
    named = 0
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        if n["name"]:
            named += 1
    lists = [n for n in nodes if n["type"] == "ListControl"]
    list_items = [n for n in nodes if n["type"] == "ListItemControl"]
    edits = [n for n in nodes if n["type"] == "EditControl"]
    buttons = [n for n in nodes if n["type"] == "ButtonControl"]
    texts = [n for n in nodes if n["type"] == "TextControl" and n["name"]]
    send_btns = [n for n in buttons if n["name"] in ("发送", "Send", "发送(S)")]
    search = [n for n in edits if n["name"] in ("搜索", "Search")]
    # 微信号 / wxid 字段：资料卡或搜索结果里才有；主窗口通常没有
    wxid_like = [n for n in texts if n["name"].startswith(("微信号", "WeChat ID", "wxid_"))]
    login_hint = [n for n in texts if any(k in n["name"] for k in ("扫码登录", "登录", "Log in", "Scan"))]
    semantic = named >= 10 and (bool(list_items) or bool(edits))
    return {
        "node_count": len(nodes),
        "named_nodes": named,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "lists": len(lists),
        "list_items": len(list_items),
        "edit_boxes": [{"name": n["name"], "aid": n["aid"], "rect": n["rect"]} for n in edits][:8],
        "send_buttons": [{"name": n["name"], "rect": n["rect"]} for n in send_btns],
        "search_boxes": len(search),
        "wxid_fields": [n["name"] for n in wxid_like][:5],
        "login_window": bool(login_hint) and not list_items,
        "verdict": {
            "semantic_tree_exposed": semantic,
            "session_list_readable": len(list_items) > 0,
            "composer_found": bool(edits) and bool(send_btns or edits),
            "identity_in_tree": bool(wxid_like),
            "visible_message_nodes_estimate": len(list_items),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PC 微信 UIAutomation 控件树探针（只读）")
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--max-nodes", type=int, default=8000)
    ap.add_argument("--out", default=os.path.join("logs", "wechat_pc_control_tree.txt"))
    ap.add_argument("--json", default=os.path.join("logs", "wechat_pc_probe.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if os.name != "nt":
        print("仅支持 Windows。", file=sys.stderr)
        return 2
    try:
        import uiautomation  # noqa: F401
    except Exception:
        print("缺少依赖 uiautomation：请先 `pip install uiautomation`", file=sys.stderr)
        return 2

    wins = find_wechat_windows()
    if not wins:
        print("未找到微信窗口。请确认：①已安装 PC 微信；②微信已启动（登录窗口也算）；③窗口未被关闭。",
              file=sys.stderr)
        return 1

    lines: List[str] = []
    report: Dict[str, Any] = {"ts": time.time(), "windows": []}
    for i, w in enumerate(wins):
        header = f"===== WeChat window #{i}: {fmt(w)} pid={getattr(w, 'ProcessId', '')} ====="
        if not args.quiet:
            print(header)
        lines.append(header)
        nodes: List[Dict[str, Any]] = []
        cap = [args.max_nodes]
        walk(w, 0, args.depth, lines, nodes, cap)
        summ = summarize(nodes)
        summ["window"] = {"name": str(w.Name or ""), "class": str(w.ClassName or ""),
                          "pid": int(w.ProcessId or 0), "process": _proc_name(int(w.ProcessId or 0))}
        report["windows"].append(summ)
        lines.append("")
        if not args.quiet:
            print(json.dumps({k: v for k, v in summ.items() if k != "by_type"},
                             ensure_ascii=False, indent=2))
            print("by_type:", json.dumps(summ["by_type"], ensure_ascii=False))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n控件树 → {args.out}（{len(lines)} 行）；摘要 → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
