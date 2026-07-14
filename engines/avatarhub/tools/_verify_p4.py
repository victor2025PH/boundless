# -*- coding: utf-8 -*-
"""P4 生产 E2E 验证（非破坏）：
① /api/metrics/devflow 漏斗：POST expose→click→ok 真记账 → GET 读数对得上（devflow 文件备份还原）
② /rvc/hot_switch 带 src → timeline(event=device) 真记账（何时切/从哪只到哪只/来源），stats 计数器进位
③ 未在转换时 /rvc/devices 不做 fresh 合并（无 fresh_note；不给日常路径加子进程枚举开销）
"""
import json
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
CFG = Path(r"C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\configs\config.json")
FLOW = Path(r"C:\模仿音色\data\devflow_stats.json")
FAILS = []


def chk(name, cond, detail=""):
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:160]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    h = requests.get(HUB + "/health", timeout=5).json()
    chk("hub 在线", bool(h))

    # ── ① 漏斗端点真记账 ──
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        base = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        b = base.get("funnel") or {}
        for ev in ("expose", "click", "ok"):
            r = requests.post(HUB + "/api/metrics/devflow",
                              json={"ev": ev, "kind": "in", "src": "p4-verify"}, timeout=5).json()
            chk(f"埋点 {ev} 落账", r.get("ok") is True, r)
        r = requests.post(HUB + "/api/metrics/devflow",
                          json={"ev": "bogus", "kind": "in"}, timeout=5).json()
        chk("非法事件被拒", r.get("ok") is False)
        now = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        f = now.get("funnel") or {}
        chk("漏斗读数进位(expose/click/ok 各+1)",
            f.get("expose") == (b.get("expose") or 0) + 1
            and f.get("click") == (b.get("click") or 0) + 1
            and f.get("ok") == (b.get("ok") or 0) + 1, json.dumps(f))
        srcs = (now.get("today") or {}).get("src") or {}
        chk("来源细分留痕(ok:p4-verify)", srcs.get("ok:p4-verify", 0) >= 1, srcs)
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        chk("devflow 文件已还原(测试不污染大盘)", (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)

    # ── ② 热切→场次账本(timeline event=device) ──
    cfg_orig = CFG.read_bytes()
    try:
        st0 = requests.get(HUB + "/realtime/health_timeline?limit=5", timeout=5).json()
        n0 = int((st0.get("stats") or {}).get("dev_hot_switch") or 0)
        d = requests.post(HUB + "/rvc/hot_switch", json={"src": "p4-verify"}, timeout=60).json()
        chk("hot_switch ok(带 src)", d.get("ok") is True, d.get("detail"))
        chk("响应含 from_input(账本『从哪只切走』)", "from_input" in d, list(d.keys()))
        chk("响应含 elapsed_s(热切耗时)", isinstance(d.get("elapsed_s"), (int, float)))
        tl = requests.get(HUB + "/realtime/health_timeline?limit=20", timeout=5).json()
        evs = [e for e in (tl.get("timeline") or []) if e.get("event") == "device"]
        hit = [e for e in evs if "来源=p4-verify" in (e.get("label") or "")]
        chk("timeline 有 device 事件(来源=p4-verify)", bool(hit),
            (hit[-1].get("label") if hit else [e.get("label") for e in evs][-3:]))
        n1 = int((tl.get("stats") or {}).get("dev_hot_switch") or 0)
        chk("stats.dev_hot_switch 进位", n1 >= n0 + 1, f"{n0} -> {n1}")
    finally:
        CFG.write_bytes(cfg_orig)
        chk("config.json 已还原", CFG.read_bytes() == cfg_orig)

    # ── ③ 未在转换 → 不做 fresh 合并 ──
    dv = requests.get(HUB + "/rvc/devices", timeout=15).json()
    chk("/rvc/devices ok", dv.get("ok") is True, dv.get("detail"))
    chk("未在转换时无 fresh_note(不加子进程枚举开销)", not dv.get("fresh_note"), dv.get("fresh_note"))

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P4 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
