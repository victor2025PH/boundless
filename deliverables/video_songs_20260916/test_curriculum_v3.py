#!/usr/bin/env python3
"""结构闸：curriculum v3 是否符合「完整闭环 / 限制 BOUNDLESS」方案。

退出码 0=PASS；非 0=FAIL（打印原因）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))

FAILS: list[str] = []
WARNS: list[str] = []

# 每集必须出现的「功能主舞台」信号（步骤里至少命中一个）
REQUIRED = {
    "E1": [("open_nth_chat",), ("click_css_text", ".ftab")],
    "E2": [("click_css", "#acct-nav-btn"), ("click_css", ".acct-add-btn")],
    "E3": [("open_chat", "BOUNDLESS"), ("click_css", "#xlate-toggle-btn")],
    "E4": [("click_css", "#mode-select"), ("click_css", "#ai-reply-btn")],
    "E5": [("goto", "/personas"), ("expand_card", "persona")],
    "E6": [("goto", "/knowledge"), ("type", "#reply-ta", "/")],
    "E7": [("expand_card", "voice"), ("cp_tab", "工具箱"), ("point_in_card", "voice", "生成语音")],
    "E8": [("expand_card", "goal"), ("cp_tab", "客户关系"), ("point_in_card", "goal", "设定目标")],
    "E9": [("goto", "/care-schedule"), ("goto", "/episodic-memory")],
    "E10": [("click_css", ".asb-ball")],
    "E11": [("open_chat", "风险会话"), ("goto", "/reply-settings")],
    "E12": [("goto", "/membership"), ("point_css", "#mb-hero"), ("point_text", "购买 / 续费")],
}


def steps_of(ep: dict) -> list:
    out = []
    for clip in ep.get("footage_actions") or []:
        out.extend(clip.get("steps") or [])
    return out


def has_op(steps: list, *parts) -> bool:
    for st in steps:
        if tuple(st[: len(parts)]) == parts:
            return True
        # 宽松：首参匹配且后续包含
        if st and st[0] == parts[0] and all(p in st for p in parts[1:]):
            return True
    return False


def main() -> int:
    eps = {e["id"]: e for e in cur["episodes"] if e.get("id", "").startswith("E")}
    for eid, reqs in REQUIRED.items():
        ep = eps.get(eid)
        if not ep:
            FAILS.append(f"{eid}: 缺章节")
            continue
        steps = steps_of(ep)
        for req in reqs:
            if not has_op(steps, *req):
                FAILS.append(f"{eid}: 缺必要步骤 {req}")

        # BOUNDLESS 滥用：E3 以外 open_chat BOUNDLESS 不应出现
        if eid != "E3":
            for st in steps:
                if st[:2] == ["open_chat", "BOUNDLESS"]:
                    FAILS.append(f"{eid}: 禁止默认 open_chat BOUNDLESS（仅 E3 允许）")

        # E12：禁止空 /pricing 与教学水贴
        if eid == "E12":
            for st in steps:
                if st[:2] == ["goto", "/pricing"]:
                    FAILS.append("E12: 禁止 goto /pricing（改走 /membership）")
                if st and st[0] == "send_demo":
                    FAILS.append("E12: 禁止 send_demo 教学水贴")

        # 禁止「只发教学水贴」作为唯一结果（E11 明确禁止 send_demo）
        if eid == "E11":
            for st in steps:
                if st and st[0] == "send_demo":
                    FAILS.append("E11: 禁止 send_demo 教学水贴")

    # 录屏 DSL 文件应含新操作
    rec = (ROOT / "record_chatx.py").read_text(encoding="utf-8")
    for token in ("ensure_assist", "expand_card", "open_nth_chat", "cp_tab", "point_in_card"):
        if f'"{token}"' not in rec and f"'{token}'" not in rec and f"op == \"{token}\"" not in rec:
            FAILS.append(f"record_chatx.py 缺 DSL `{token}`")

    print("== curriculum v3 structure gate ==")
    for w in WARNS:
        print("WARN", w)
    for f in FAILS:
        print("FAIL", f)
    if FAILS:
        print(f"RESULT FAIL ({len(FAILS)})")
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
