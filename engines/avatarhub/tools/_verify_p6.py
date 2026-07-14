# -*- coding: utf-8 -*-
"""P6 生产 E2E 验证（非破坏，不需要 RVC 在跑）：
① heal config 新开关 dev_autoswitch：GET 可读(带护栏参数) / dry_run 不落盘 / POST 真切换+持久化 / 还原
② 周报端点 /api/metrics/devflow/weekly：只读报文结构 + 注入上周分桶后聚合读数对得上（devflow 备份还原）
③ send=1 强发：走 alerts 通道真发一次（无 webhook 时 sent=0 但 ok），且不写 weekly_sent 自动去重标记
"""
import datetime as dt
import json
import sys
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
FLOW = Path(r"C:\模仿音色\data\devflow_stats.json")
HEAL = Path(r"C:\模仿音色\data\heal_config.json")
FAILS = []


def chk(name, cond, detail=""):
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:180]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    requests.get(HUB + "/health", timeout=5)

    # ── ① dev_autoswitch 开关 ──
    heal_orig = HEAL.read_bytes() if HEAL.exists() else None
    try:
        d0 = requests.get(HUB + "/api/heal/config", timeout=5).json()
        chk("GET heal config 带 dev_autoswitch", isinstance(d0.get("dev_autoswitch"), bool),
            d0.get("dev_autoswitch"))
        g = d0.get("dev_autoswitch_guard") or {}
        chk("护栏参数可见(confirm/cooldown/max)",
            all(k in g for k in ("confirm", "cooldown", "max")), g)
        orig = bool(d0.get("dev_autoswitch"))
        r = requests.post(HUB + "/api/heal/config",
                          json={"dev_autoswitch": (not orig), "dry_run": True}, timeout=5).json()
        chk("dry_run 只回显不生效", r.get("dry_run") is True
            and r.get("changed", {}).get("dev_autoswitch") == (not orig)
            and r.get("dev_autoswitch") == orig, r.get("changed"))
        r = requests.post(HUB + "/api/heal/config", json={"dev_autoswitch": (not orig)}, timeout=5).json()
        chk("POST 真切换", r.get("dev_autoswitch") == (not orig))
        per = json.loads(HEAL.read_text(encoding="utf-8"))
        chk("已持久化到 heal_config.json", per.get("dev_autoswitch") == (not orig), per)
        r = requests.post(HUB + "/api/heal/config", json={"dev_autoswitch": orig}, timeout=5).json()
        chk("切回原值", r.get("dev_autoswitch") == orig)
    finally:
        if heal_orig is None:
            HEAL.unlink(missing_ok=True)
        else:
            HEAL.write_bytes(heal_orig)

    # ── ② 周报聚合（注入上周分桶）──
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        today = dt.date.today()
        monday_prev = today - dt.timedelta(days=today.weekday() + 7)
        doc = {}
        if flow_orig:
            try:
                doc = json.loads(flow_orig.decode("utf-8"))
            except Exception:
                doc = {}
        days = doc.setdefault("days", {})
        days[monday_prev.isoformat()] = {"expose_in": 4, "click_in": 3, "ok_in": 3,
                                         "src": {"ok:strip": 2, "ok:auto": 1}}
        days[(monday_prev + dt.timedelta(days=3)).isoformat()] = {"expose_in": 1, "click_in": 1, "fail_in": 1,
                                                                  "src": {"fail:auto": 1}}
        FLOW.parent.mkdir(parents=True, exist_ok=True)
        FLOW.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        d = requests.get(HUB + "/api/metrics/devflow/weekly", timeout=5).json()
        rep = d.get("report") or {}
        chk("周报窗口=上一自然周", rep.get("monday") == monday_prev.isoformat(), rep.get("span"))
        chk("周报聚合读数(4/4/3/1)", rep.get("expose") == 5 and rep.get("click") == 4
            and rep.get("ok") == 3 and rep.get("fail") == 1,
            {k: rep.get(k) for k in ("expose", "click", "ok", "fail")})
        chk("周报文本含成功率", "成功率 75%" in (d.get("text") or ""), d.get("text"))
        chk("weekly_auto 状态可见", isinstance(d.get("weekly_auto"), dict), d.get("weekly_auto"))

        # ── ③ send=1 强发（不占自动额度）──
        r = requests.get(HUB + "/api/metrics/devflow/weekly?send=1", timeout=15).json()
        chk("send=1 ok(经 alerts 外发)", r.get("ok") is True and isinstance(r.get("sent"), int),
            f"sent={r.get('sent')} (0=未配 webhook,正常)")
        cur = json.loads(FLOW.read_text(encoding="utf-8"))
        chk("强发不写 weekly_sent(不占本周自动额度)",
            cur.get("weekly_sent") != monday_prev.isoformat(), cur.get("weekly_sent"))
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        chk("devflow 文件已还原", (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P6 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
