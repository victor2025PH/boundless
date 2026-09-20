# -*- coding: utf-8 -*-
"""PC 微信主窗 · 资料卡探测（找「微信号」字段）。只点标题/头像，不改任何资料，不发 Esc。

用 Win32 快速定位窗口（毫秒级），再 ControlFromHandle 进 UIA。流程：主窗 → 若「聊天信息」侧栏
未开则点标题打开 → 点侧栏头像（mmui::ContactHeadView）→ 导出新出现的顶层窗口（资料卡弹窗）与
主窗子树 → 关闭弹窗（WindowPattern.Close）。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import uiautomation as auto  # noqa: E402

from scripts.wechat_pc_probe import fmt, walk  # noqa: E402
from scripts.wechat_pc_probe_chat import _find  # noqa: E402
from src.integrations.wechat_pc.win32_windows import find_wechat_windows  # noqa: E402


def uia_windows(visible_only: bool = True) -> List[Any]:
    out = []
    for w in find_wechat_windows(visible_only=visible_only):
        if not w.class_name.startswith("Qt"):
            continue
        try:
            c = auto.ControlFromHandle(w.hwnd)
            if c is not None:
                out.append(c)
        except Exception:
            continue
    return out


def dump(root: Any, tag: str) -> List[dict]:
    lines: List[str] = []
    nodes: List[dict] = []
    walk(root, 0, 26, lines, nodes, [8000])
    path = os.path.join("logs", f"wechat_pc_profile_{tag}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    hits = [n for n in nodes if any(k in n["name"] for k in ("微信号", "WeChat ID", "wxid_", "地区", "备注", "朋友权限", "来源", "标签"))]
    print(f"[{tag}] nodes={len(nodes)} → {path}")
    for h in hits[:20]:
        print("   hit:", h["type"], repr(h["name"]), "aid=", h["aid"], "cls=", h["cls"], "rect=", h["rect"])
    return nodes


def main() -> int:
    t0 = time.time()
    wins = uia_windows()
    mains = [w for w in wins if str(w.ClassName or "") == "mmui::MainWindow"]
    print("可见 Qt 窗口:", [(str(w.ClassName), str(w.Name)) for w in wins], f"({time.time() - t0:.1f}s)")
    if not mains:
        print("no main window")
        return 1
    main_win = mains[0]
    before = {int(w.NativeWindowHandle) for w in wins}
    def _heads():
        return _find(main_win, lambda c: str(getattr(c, "ClassName", "") or "") == "mmui::ContactHeadView")

    heads = _heads()
    # 「聊天信息」是开/关切换：每次只点一下再检查，最多两轮（防止把刚打开的又关上）
    for attempt in range(2):
        if heads:
            break
        more = _find(main_win, lambda c: str(getattr(c, "Name", "") or "") == "聊天信息"
                     and getattr(c, "ControlTypeName", "") == "ButtonControl")
        if not more:
            print("no 聊天信息 button（先点开一个会话）")
            return 1
        print(f"侧栏未开 → 点击「聊天信息」(第 {attempt + 1} 次)")
        try:
            main_win.SetActive()
            time.sleep(0.3)
        except Exception:
            pass
        inv = None
        try:
            inv = more[0].GetInvokePattern()
        except Exception:
            inv = None
        if inv is not None:
            print("   经 InvokePattern")
            inv.Invoke()
        else:
            more[0].Click(simulateMove=True)
        time.sleep(1.5)
        heads = _heads()
        if not heads:
            n = len(_find(main_win, lambda c: True))
            print(f"   点击后节点数={n}")
    if not heads:
        dump(main_win, "no_head_main")
        print("未找到 ContactHeadView")
        return 1
    print("点击头像 ContactHeadView rect=", heads[0].BoundingRectangle)
    heads[0].Click(simulateMove=True)
    time.sleep(1.5)
    after = uia_windows()
    popups = [w for w in after if int(w.NativeWindowHandle) not in before]
    print(f"新弹窗数={len(popups)}")
    for i, p in enumerate(popups):
        print(f"popup#{i}: {fmt(p)}")
        dump(p, f"popup{i}")
    dump(main_win, "main_after_head_click")
    for p in popups:
        try:
            wp = p.GetWindowPattern()
            if wp:
                wp.Close()
                print("已关闭弹窗:", p.ClassName)
        except Exception:
            pass
    print(f"done ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
