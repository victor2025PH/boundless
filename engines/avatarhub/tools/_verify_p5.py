# -*- coding: utf-8 -*-
"""P5 生产 E2E 验证（非破坏）：
① GET /api/metrics/devflow 新增 src_total 跨天聚合真出数（POST 留痕→GET 聚出；devflow 文件备份还原）
② ops 页真挂上「设备自愈漏斗」卡（devflowCard/devflowTick/来源 chips 接线）
③ 成绩单热切行的数据通路：hot_switch(src=p5-verify) → /realtime/health_timeline 能按
   「event=device + 时间窗」滤出该事件（前端 fetchHotSwitchRecap 的同款查询在生产真跑通）
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

    # ── ① src_total 跨天聚合 ──
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        for ev in ("expose", "click", "ok"):
            requests.post(HUB + "/api/metrics/devflow",
                          json={"ev": ev, "kind": "in", "src": "p5-verify"}, timeout=5)
        d = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        chk("GET 响应带 src_total 字段", isinstance(d.get("src_total"), dict), list(d.keys()))
        st = d.get("src_total") or {}
        chk("src_total 聚出本次留痕(ok:p5-verify≥1)", st.get("ok:p5-verify", 0) >= 1,
            {k: v for k, v in st.items() if "p5-verify" in k})
        f = d.get("funnel") or {}
        chk("funnel 读数仍在(与 src_total 同响应)", "expose" in f and "success_rate" in f, f)
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        chk("devflow 文件已还原(测试不污染大盘)",
            (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)

    # ── ② ops 页漏斗卡在线接线 ──
    ops = requests.get(HUB + "/ops", timeout=5).text
    for needle, label in [("devflowCard", "漏斗卡容器"), ("devflowTick", "渲染函数"),
                          ("设备自愈漏斗", "卡标题"), ("src_total", "来源细分读取")]:
        chk(f"ops 页含{label}", needle in ops)
    ui = requests.get(HUB + "/ui", timeout=5).text
    for needle, label in [("lastSession?.hotSwitch", "成绩单热切行显隐"),
                          ("设备热切", "热切行标题")]:
        chk(f"ui 页含{label}", needle in ui)

    # ── ③ 成绩单数据通路：hot_switch → timeline 时间窗滤出 ──
    cfg_orig = CFG.read_bytes()
    try:
        t0 = time.time()
        d = requests.post(HUB + "/rvc/hot_switch", json={"src": "p5-verify"}, timeout=60).json()
        chk("hot_switch ok(src=p5-verify)", d.get("ok") is True, d.get("detail"))
        tl = requests.get(HUB + "/realtime/health_timeline?limit=80", timeout=5).json()
        # 复刻前端 fetchHotSwitchRecap 的过滤：event=device 且 ts 落在 [t0-5s, now+10s]
        win = [e for e in (tl.get("timeline") or [])
               if e.get("event") == "device" and (t0 - 5) <= float(e.get("ts") or 0) <= (time.time() + 10)]
        hit = [e for e in win if "来源=p5-verify" in (e.get("label") or "")]
        chk("时间窗滤出本次 device 事件(成绩单同款查询)", bool(hit),
            (hit[-1].get("label") if hit else f"窗内 {len(win)} 条"))
        if hit:
            lab = hit[-1].get("label") or ""
            chk("账本明细含成败+来源(成绩单明细行可读)", ("热切" in lab and "来源=" in lab), lab)
    finally:
        CFG.write_bytes(cfg_orig)
        chk("config.json 已还原", CFG.read_bytes() == cfg_orig)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P5 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
