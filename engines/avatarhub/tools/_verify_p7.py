# -*- coding: utf-8 -*-
"""P7 生产 E2E 验证（非破坏，不需要 RVC 在跑）：
① /realtime/status 顺风车：无近期自动热切时 dev_autoswitch_last=null（不带陈年旧账）
② /api/metrics/devflow 带 auto_advice：注入「人工战绩达标」分桶 → suggest=true 且 reason 人话；
   注入「auto 刷的数据」→ 被剔除不算战绩（suggest=false 样本不足）；devflow 文件备份还原
③ 前端接线：/ui 真挂上建议条与即时感知处理器
"""
import json
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
FLOW = Path(r"C:\模仿音色\data\devflow_stats.json")
FAILS = []


def chk(name, cond, detail=""):
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:180]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    requests.get(HUB + "/health", timeout=5)

    # ── ① status 顺风车 ──
    st = requests.get(HUB + "/realtime/status", timeout=5).json()
    chk("status 含 dev_autoswitch_last 键", "dev_autoswitch_last" in st, list(st.keys())[:8])
    # 10min 窗口语义（与实弹演练的先后顺序解耦）：无近期事件=null；有则必须在窗内（不带陈年旧账）
    asw = st.get("dev_autoswitch_last")
    chk("顺风车 10min 窗口语义", asw is None or (time.time() - float(asw.get("ts") or 0)) < 600, asw)

    # ── ② auto_advice ──
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        # 人工战绩达标（3/3/100%）→ 建议
        doc = {"days": {"2026-07-06": {"expose_in": 3, "click_in": 3, "ok_in": 3,
                                       "src": {"ok:strip": 3, "click:strip": 3, "expose:strip": 3}}}}
        FLOW.parent.mkdir(parents=True, exist_ok=True)
        FLOW.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        d = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        a = d.get("auto_advice") or {}
        chk("人工战绩达标→suggest=true", a.get("suggest") is True, a)
        chk("reason 人话（含次数+成功率）", "3 次" in (a.get("reason") or "") and "100%" in (a.get("reason") or ""),
            a.get("reason"))
        # 全是 auto 刷的 → 剔除后样本 0，不建议（自动切不能给自己刷开启依据）
        doc = {"days": {"2026-07-06": {"expose_in": 5, "click_in": 5, "ok_in": 5,
                                       "src": {"expose:auto": 5, "click:auto": 5, "ok:auto": 5}}}}
        FLOW.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        d = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        a = d.get("auto_advice") or {}
        chk("auto 分量被剔除→不建议", a.get("suggest") is False and (a.get("manual") or {}).get("click") == 0,
            a)
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        chk("devflow 文件已还原", (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)

    # ── ③ 前端接线 ──
    ui = requests.get(HUB + "/ui", timeout=5).text
    for needle, label in [("autoSwAdvice", "建议条状态"), ("不再提示", "一次性退场按钮"),
                          ("开启自动热切", "一键开启按钮")]:
        chk(f"ui 页含{label}", needle in ui)
    js = requests.get(HUB + "/static/hub.js", timeout=5).text
    for needle, label in [("_autoSwNotice", "即时感知处理器"), ("_autoSwBaselined", "基线防重放"),
                          ("checkAutoSwAdvice", "建议裁决拉取")]:
        chk(f"hub.js 含{label}", needle in js)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P7 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
