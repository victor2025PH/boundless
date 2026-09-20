# -*- coding: utf-8 -*-
"""PC 微信主窗 · 当前打开会话的气泡属性探测（只读）：找「己方/对方」的可判定信号。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import uiautomation as auto  # noqa: E402

from scripts.wechat_pc_probe import find_wechat_windows  # noqa: E402
from scripts.wechat_pc_probe_chat import _find  # noqa: E402


def props(c) -> dict:
    out = {}
    for attr in ("Name", "AutomationId", "ClassName", "ControlTypeName", "HelpText", "ItemStatus",
                 "ItemType", "AriaProperties", "AriaRole", "FullDescription", "LocalizedControlType",
                 "IsOffscreen", "IsKeyboardFocusable", "IsContentElement", "IsControlElement"):
        try:
            out[attr] = getattr(c, attr)
        except Exception as e:  # noqa: BLE001
            out[attr] = f"<err {type(e).__name__}>"
    try:
        r = c.BoundingRectangle
        out["rect"] = (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    try:
        leg = c.GetLegacyIAccessiblePattern()
        if leg:
            out["legacy"] = {"Name": leg.Name, "Value": leg.Value, "Description": leg.Description,
                             "Help": leg.Help, "Role": leg.Role, "State": leg.State,
                             "DefaultAction": leg.DefaultAction}
    except Exception as e:  # noqa: BLE001
        out["legacy"] = f"<err {type(e).__name__}>"
    try:
        out["runtime_id"] = c.GetRuntimeId()
    except Exception:
        pass
    try:
        out["patterns"] = [p for p in dir(c) if p.startswith("Get") and p.endswith("Pattern")
                           and getattr(c, p)() is not None]
    except Exception:
        pass
    return out


def main() -> int:
    wins = [w for w in find_wechat_windows() if str(w.ClassName or "") == "mmui::MainWindow"]
    if not wins:
        print("no main window")
        return 1
    lists = _find(wins[0], lambda c: getattr(c, "AutomationId", "") == "chat_message_list")
    if not lists:
        print("no chat_message_list（先点开一个会话）")
        return 1
    items = lists[0].GetChildren()
    print("items:", len(items))
    for i, it in enumerate(items):
        p = props(it)
        print(f"--- item {i} ---")
        for k, v in p.items():
            print(f"  {k}: {v!r}")
        kids = it.GetChildren()
        print("  children:", len(kids))
        for k in kids[:6]:
            kp = props(k)
            print("    child:", kp.get("ControlTypeName"), repr(kp.get("Name")), kp.get("AutomationId"),
                  kp.get("ClassName"), kp.get("rect"))
    # 标题/输入框属性
    for aid in ("current_chat_name_label", "chat_input_field"):
        for c in _find(wins[0], lambda c, a=aid: a in str(getattr(c, "AutomationId", "") or "")):
            p = props(c)
            print(f"=== {aid}: Name={p.get('Name')!r} legacy={p.get('legacy')} patterns={p.get('patterns')}")
            try:
                vp = c.GetValuePattern()
                print("    ValuePattern.Value=", repr(vp.Value) if vp else None)
            except Exception as e:  # noqa: BLE001
                print("    ValuePattern err", type(e).__name__)
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
