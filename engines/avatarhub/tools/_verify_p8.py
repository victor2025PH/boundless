# -*- coding: utf-8 -*-
"""P8 生产 E2E 验证（非破坏，不需要 RVC 在跑）：
① /realtime/status 带 rvc_conv 布尔（自救卡退场判据的顺风车）
② advice_* 埋点端到端：bump 端点收三事件（kind 缺省/乱给都行）→ GET 聚合出 advice
   且主漏斗 funnel 与 P7-2 裁决分母都不被污染；非法 ev 拒绝；devflow 文件备份还原
③ 周报预览含 advice 段；④ 前端接线（/ui /static/hub.js /ops 真挂上自救卡+埋点）
"""
import json
import sys
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

    # ── ① rvc_conv 顺风车 ──
    st = requests.get(HUB + "/realtime/status", timeout=5).json()
    chk("status 含 rvc_conv 布尔", isinstance(st.get("rvc_conv"), bool), st.get("rvc_conv"))

    # ── ② advice 埋点端到端（备份→打点→读数→还原）──
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        FLOW.parent.mkdir(parents=True, exist_ok=True)
        FLOW.write_text("{}", encoding="utf-8")
        for ev, kind in [("advice_expose", ""), ("advice_expose", "in"),   # kind 应被忽略
                         ("advice_enable", ""), ("advice_dismiss", "")]:
            r = requests.post(HUB + "/api/metrics/devflow", json={"ev": ev, "kind": kind}, timeout=5).json()
            if not r.get("ok"):
                chk(f"bump {ev} 被收下", False, r)
        r = requests.post(HUB + "/api/metrics/devflow", json={"ev": "advice_bogus"}, timeout=5).json()
        chk("非法 advice ev 被拒", r.get("ok") is False, r)
        # 对照组：一条真主漏斗事件
        requests.post(HUB + "/api/metrics/devflow", json={"ev": "expose", "kind": "in", "src": "strip"}, timeout=5)
        d = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        adv = d.get("advice") or {}
        chk("advice 聚合(曝光2/采纳1/婉拒1)", (adv.get("expose"), adv.get("enable"), adv.get("dismiss")) == (2, 1, 1), adv)
        chk("采纳率 50%", adv.get("enable_rate") == 0.5, adv.get("enable_rate"))
        f = d.get("funnel") or {}
        chk("主漏斗不被 advice 污染(expose=1, click=0)", f.get("expose") == 1 and f.get("click") == 0, f)
        man = ((d.get("auto_advice") or {}).get("manual") or {})
        chk("P7-2 裁决分母不含 advice(expose=1)", man.get("expose") == 1, man)
        # ── ③ 周报预览含 advice 段（本周数据不在上周窗→advice 全 0 且不占行；结构存在即可）──
        w = requests.get(HUB + "/api/metrics/devflow/weekly", timeout=5).json()
        rep = w.get("report") or {}
        chk("周报结构含 advice 段", isinstance(rep.get("advice"), dict), rep.get("advice"))
        chk("零 advice 周不占行", "开启建议" not in (w.get("text") or ""), w.get("text"))
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        chk("devflow 文件已还原", (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)

    # ── ④ 前端接线 ──
    ui = requests.get(HUB + "/ui", timeout=5).text
    for needle, label in [("autoSwFail", "自救卡状态绑定"), ("再试", "再试热切按钮"),
                          ("autoSwRescueRestart()", "直接重启变声按钮")]:
        chk(f"ui 页含{label}", needle in ui)
    js = requests.get(HUB + "/static/hub.js", timeout=5).text
    for needle, label in [("_autoSwFailShow", "失败进场"), ("d.rvc_conv", "转换旗标退场判据"),
                          ("_advFlow", "advice 埋点 helper")]:
        chk(f"hub.js 含{label}", needle in js)
    ops = requests.get(HUB + "/ops", timeout=5).text
    chk("ops 含建议条效果读数", "开启建议" in ops and "d.advice" in ops)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P8 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
