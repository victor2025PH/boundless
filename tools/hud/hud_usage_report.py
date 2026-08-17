# -*- coding: utf-8 -*-
"""HUD 使用热度周报（P0 展演骨架 · 2026-08-08）。

出处《未来感机房展演_手势语音指挥与手机万能外设_五视角优化美化方案_20260808.md》§4.5/§七 P0。
定位：「四周零使用提退役」家规与 §14 隔空指挥「数据决定去留」的数据端——
P4/彩蛋类功能上线前必须先有这份账（埋点先行纪律）。

数据源：hud_server `/api/stat` 信标落的 C:/Users/Public/boundless-hud/hud_usage.jsonl
（六机 HUD 页面统一 POST 中枢；demo/实拍页面不落账）。
行格式：{"ts":epoch,"k":功能键,"m":机器,"v":附注,"src":输入方式}

已埋功能键：hud_open(唤出) / wake(手势唤醒) / view(切视图) / extract(捏合提取) /
            detail(详情卡) / ack(确认告警) / tour_step(展演切幕，服务端自记)
src 语义：gesture / mouse / key / remote ——「三输入热度对比」的口径。

用法：python hud_usage_report.py [--days 7] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

USAGE = Path(r"C:\Users\Public\boundless-hud\hud_usage.jsonl")

# 退役观察名单：新增的展演/交互功能键都要在这里挂号（上线即约定死法）
WATCHED = ["hud_open", "wake", "view", "extract", "detail", "ack", "tour_step",
           "voice_wake", "voice_intent", "voice_deny",
           "ops_arm", "ops_fire", "ops_deny", "ops_undo", "phone_role",
           "holo_panel", "holo_drill",   # P1/P3 功能加厚（2026-08-12）：全息操作盘/服务下钻
           "cockpit_throw", "gest_lead",  # C0/C1 指挥舱（2026-08-13）：甩桌面跨屏/指挥权接力
           "modebar", "ui_reload"]  # 首屏模式条武装/界面版本自愈（2026-08-16）：
#          modebar 持续为零=首屏切换入口没人用（回退全息盘的信号）；ui_reload src 分布
#          pill:auto 比值=有人在看时更新多还是无人值守窗自愈多
RETIRE_WEEKS = 4


def load(days: int) -> list[dict]:
    if not USAGE.is_file():
        return []
    cutoff = time.time() - days * 86400
    out = []
    for ln in USAGE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cutoff:
            out.append(rec)
    return out


def day_of(ts: float) -> str:
    return time.strftime("%m-%d", time.localtime(ts))


def build(days: int) -> dict:
    recs = load(days)
    by_key: Counter = Counter(r.get("k") for r in recs)
    by_src: Counter = Counter(r.get("src") or "?" for r in recs if r.get("k") != "hud_open")
    by_day: dict[str, Counter] = defaultdict(Counter)
    by_machine: Counter = Counter(r.get("m") or "?" for r in recs)
    views: Counter = Counter(r.get("v") for r in recs if r.get("k") == "view")
    tours = [r for r in recs if r.get("k") == "tour_step"]
    tour_opens = sum(1 for r in tours if str(r.get("v") or "").startswith("1:"))
    last_seen: dict[str, float] = {}
    for r in recs:
        by_day[day_of(float(r.get("ts") or 0))][r.get("k")] += 1
        k = r.get("k")
        if k:
            last_seen[k] = max(last_seen.get(k, 0), float(r.get("ts") or 0))
    zero = [k for k in WATCHED if by_key.get(k, 0) == 0]
    return {
        "days": days, "total": len(recs),
        "by_key": dict(by_key), "by_src": dict(by_src),
        "by_day": {d: dict(c) for d, c in sorted(by_day.items())},
        "by_machine": dict(by_machine), "views": dict(views),
        "tour_steps": len(tours), "tour_opens": tour_opens,
        "zero_usage": zero,
        "last_seen": {k: round(time.time() - v) for k, v in last_seen.items()},
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="HUD 使用热度周报")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = build(a.days)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"== HUD 使用热度（近 {rep['days']} 天，共 {rep['total']} 条） ==")
    if not rep["total"]:
        print("（无数据：埋点刚上线，或这段时间没人用 HUD）")
        return 0
    KN = {"hud_open": "唤出", "wake": "手势唤醒", "view": "切视图", "extract": "捏合提取",
          "detail": "详情卡", "ack": "确认告警", "tour_step": "展演切幕",
          "voice_wake": "语音唤醒", "voice_intent": "语音意图", "voice_deny": "语音拒答",
          "ops_arm": "指挥武装", "ops_fire": "指挥点火", "ops_deny": "指挥拒绝",
          "ops_undo": "指挥撤销", "phone_role": "手机角色", "holo_panel": "全息操作盘",
          "holo_drill": "服务下钻", "cockpit_throw": "甩桌面跨屏", "gest_lead": "指挥权接力",
          "modebar": "首屏切模式", "ui_reload": "界面自愈刷新"}
    print("\n-- 功能热度 --")
    for k, n in sorted(rep["by_key"].items(), key=lambda x: -x[1]):
        age = rep["last_seen"].get(k)
        ago = f"（最近 {age // 3600}h 前）" if age is not None else ""
        print(f"  {KN.get(k, k):<6} {n:>5} 次 {ago}")
    print("\n-- 输入方式（三输入热度对比） --")
    SN = {"gesture": "手势", "mouse": "鼠标", "key": "键盘", "remote": "遥控", "voice": "语音"}
    for s, n in sorted(rep["by_src"].items(), key=lambda x: -x[1]):
        print(f"  {SN.get(s, s):<4} {n:>5} 次")
    if rep["views"]:
        VN = {"rows": "表格", "events": "事件流", "cons": "星座"}
        print("\n-- 视图去向 --")
        for v, n in sorted(rep["views"].items(), key=lambda x: -x[1]):
            print(f"  {VN.get(v, v):<4} {n:>5} 次")
    print(f"\n-- 展演 --\n  切幕 {rep['tour_steps']} 次 · 开场（第1幕）{rep['tour_opens']} 场")
    print("\n-- 逐日 --")
    for d, c in rep["by_day"].items():
        top = " ".join(f"{KN.get(k, k)}×{n}"
                       for k, n in sorted(c.items(), key=lambda x: -x[1])[:4])
        print(f"  {d}  {sum(c.values()):>4} 条  {top}")
    if rep["by_machine"]:
        print("\n-- 屏幕分布 --")
        for m, n in sorted(rep["by_machine"].items(), key=lambda x: -x[1]):
            print(f"  {m or '?':<10} {n:>5} 条")
    if rep["zero_usage"]:
        note = ("窗口不足 4 周，先观察" if a.days < RETIRE_WEEKS * 7
                else f"连续 {RETIRE_WEEKS} 周零使用可按家规提退役")
        print(f"\n-- 零使用 --\n  {', '.join(KN.get(k, k) for k in rep['zero_usage'])}（{note}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
