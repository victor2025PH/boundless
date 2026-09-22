#!/usr/bin/env python3
"""客户关系养成场景闸：注册表 ↔ curriculum ↔ 歌词合规。

  python test_scenarios_nurture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lyric_gate import gate_file  # noqa: E402

FAILS: list[str] = []


def main() -> int:
    sc = json.loads((ROOT / "scenarios_nurture.json").read_text(encoding="utf-8"))
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    eps = {e["id"]: e for e in cur["episodes"]}
    banned = sc.get("banned_public_phrases") or []

    if sc.get("public_label") != "客户关系养成":
        FAILS.append("public_label 必须是「客户关系养成」")

    for item in sc["episodes"]:
        eid = item["id"]
        ep = eps.get(eid)
        if not ep:
            FAILS.append(f"{eid}: curriculum 缺章")
            continue
        if ep.get("scenario") != "nurture":
            FAILS.append(f"{eid}: curriculum.scenario 应为 nurture")
        # 禁 BOUNDLESS 默认
        for clip in ep.get("footage_actions") or []:
            for st in clip.get("steps") or []:
                blob = " ".join(str(x) for x in st)
                for w in banned:
                    if w in blob:
                        FAILS.append(f"{eid}: 步骤含禁词「{w}」")
                if st[:2] == ["open_chat", "BOUNDLESS"]:
                    FAILS.append(f"{eid}: 禁止 open_chat BOUNDLESS（养成线用中文演示会话）")
        # 歌词
        for key in ("rap", "intro"):
            rel = item.get(key)
            if not rel:
                continue
            p = ROOT / rel
            if not p.exists():
                FAILS.append(f"{eid}: 缺 {rel}")
                continue
            fails, _ = gate_file(p, lang="zh", audience="public")
            for f in fails:
                FAILS.append(f"{eid}/{p.name}: {f}")
            text = p.read_text(encoding="utf-8")
            for w in banned:
                if w in text:
                    FAILS.append(f"{eid}/{p.name}: 含禁词「{w}」")
        # registry
        if not any(it.get("id") == eid for it in reg["items"]):
            FAILS.append(f"{eid}: registry 缺条目")
        if not any(it.get("id") == f"{eid}_intro" for it in reg["items"]):
            FAILS.append(f"{eid}_intro: registry 缺条目")

    # 必要步骤信号（场景主舞台必须看见真会话 / 真卡）
    need = {
        "R1": [("open_chat", "智聊支持"), ("point_text", "雾霾蓝")],
        "R2": [("open_chat", "智聊支持"), ("expand_card", "voice"), ("point_in_card", "voice", "生成语音")],
        "R3": [("goto", "/personas"), ("open_chat", "智聊支持"), ("expand_card", "persona")],
        # R4 主线三件：定目标 / SOP 工作链 / AI 拟稿
        "R4": [("open_chat", "智聊支持"), ("expand_card", "goal"), ("point_in_card", "goal", "设定目标"),
               ("expand_card", "chain"), ("point_in_card", "chain", "启动工作链"), ("point_text", "生成草稿")],
    }
    for eid, reqs in need.items():
        ep = eps.get(eid) or {}
        steps = []
        for c in ep.get("footage_actions") or []:
            steps.extend(c.get("steps") or [])
        for req in reqs:
            ok = any(tuple(st[: len(req)]) == req for st in steps)
            if not ok:
                FAILS.append(f"{eid}: 缺步骤 {req}")

    print("== test_scenarios_nurture ==")
    for f in FAILS:
        print("FAIL", f)
    print("RESULT", "FAIL" if FAILS else "PASS", f"({len(FAILS)})" if FAILS else "")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
