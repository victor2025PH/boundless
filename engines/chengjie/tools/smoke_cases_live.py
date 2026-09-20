# -*- coding: utf-8 -*-
"""案例中心 P4-P7 线上冒烟（重启装载后的只读验收；--confirm 才动演练数据）。

用途：共享树攒批重启后，快速确认案例中心整条链在**生产进程**里真的装上了——
pytest 全绿只证代码对，证不了「在跑的那份」是新代码（本仓 P4 曾靠它抓过
「/active 无 summary=旧代码还在跑」）。

检查面（S1-S4 只读；S5 默认跳过）：
  S1  /api/cases/active 契约：summary（P4）+ 行含 mute_until（P5）
  S2  effectiveness / takeover_stats（P6/P7；数据依赖字段——缺数据=n/a 不算失败，
      字段形状错才 FAIL）
  S3  /api/workspace/metrics.cases 含 suppressed（P5 计数装载标记）
  S4  周报装配自检：本工具不触发周报，只静态提示（真值走 ops_report 事件）
  S5  --confirm：POST /api/cases/close-drill → 断言回包含 remaining_open_drill
      （会真的结案演练残影——生产语义本就如此，但默认不动）

退出码：0=PASS/SKIP（实例不可达）；1=契约 FAIL。
刻意不进 gate_sweep/计划任务——它验收的是「某次重启」，不是回归。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"


class _Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))
        self._token = token

    def login(self) -> bool:
        req = urllib.request.Request(
            self.base + "/login",
            data=f"auth_token={urllib.parse.quote(self._token)}".encode(),
            method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            self._opener.open(req, timeout=8)
        except Exception:
            return False
        return any(c.name == "session" for c in self._cj)

    def get(self, path: str) -> dict:
        r = self._opener.open(self.base + path, timeout=10)
        return json.loads(r.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        csrf = next((c.value for c in self._cj if c.name == "csrf_token"), "")
        if csrf:
            req.add_header("X-CSRF-Token", csrf)
        r = self._opener.open(req, timeout=15)
        return json.loads(r.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="案例中心线上冒烟（只读为主）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    ap.add_argument("--confirm", action="store_true",
                    help="S5：真调 close-drill（会结案演练残影）")
    args = ap.parse_args(argv)

    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception as e:
        print(f"[SKIP] 实例不可达 {args.base}: {e}")
        return 0

    token = args.token
    if not token:
        try:
            from tools.verify_cases_ui import read_token   # 从仓库根跑
        except ImportError:
            from verify_cases_ui import read_token          # python tools/xxx.py 直跑
        token = read_token(args.data_root)
    if not token:
        print("[ABORT] 读不到 web_admin.auth_token")
        return 2

    cli = _Client(args.base, token)
    if not cli.login():
        print("[ABORT] 登录失败")
        return 2

    fails: list = []

    def check(name: str, ok, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    def info(name: str, detail: str) -> None:
        print(f"  [INFO] {name}  {detail}")

    print("== S1. /api/cases/active 契约 ==")
    d = cli.get("/api/cases/active")
    sm = d.get("summary")
    check("summary 在场（P4 装载标记）", isinstance(sm, dict),
          json.dumps(sm, ensure_ascii=False) if sm else "missing")
    if isinstance(sm, dict):
        need = {"open", "urgent", "media_open", "media_stale", "open_drill"}
        check("summary 键齐全", need.issubset(sm.keys()),
              str(sorted(need - set(sm.keys()))))
    rows = d.get("cases") or []
    if rows:
        check("行带 mute_until（P5 装载标记）",
              all("mute_until" in r for r in rows), f"rows={len(rows)}")
    else:
        info("行字段", "当前零案例，跳过行形状断言")

    print("== S2. 效果回环 / 接管闭环（数据依赖，缺数据=n/a）==")
    eff = d.get("effectiveness")
    if eff is None:
        info("effectiveness", "n/a（30 天内无媒体质疑结案样本，或 P6 未装载——"
                              "配合 S3 判断）")
    else:
        check("effectiveness 形状（by_bucket+alerts）",
              isinstance(eff.get("by_bucket"), dict)
              and isinstance(eff.get("alerts"), list),
              json.dumps({k: eff.get(k) for k in ("closed", "recurred")},
                         ensure_ascii=False))
    tk = d.get("takeover_stats")
    if tk is None:
        info("takeover_stats", "n/a（30 天内无 case:handoff 接管，或 P7 未装载）")
    else:
        check("takeover_stats 形状", {"count", "open"}.issubset(tk.keys()),
              json.dumps(tk, ensure_ascii=False))

    print("== S3. metrics.cases（P5 suppressed 计数）==")
    m = cli.get("/api/workspace/metrics")
    cases_m = (m or {}).get("cases") or {}
    check("metrics.cases.suppressed 键在场", "suppressed" in cases_m,
          f"keys={sorted(cases_m.keys())[:8]}")

    print("== S4. 周报装配 ==")
    info("value_lines", "案例待办/处置质量行随 ops_report 周报事件出账，"
                        "本工具不触发（health_watchdog.weekly_report_enabled）")

    if args.confirm:
        print("== S5. close-drill 契约（--confirm）==")
        r = cli.post("/api/cases/close-drill", {})
        check("回包含 remaining_open_drill（P4 契约）",
              "remaining_open_drill" in r, json.dumps(r, ensure_ascii=False))
    else:
        print("== S5. close-drill（跳过，--confirm 才动演练数据）==")

    n_fail = len(fails)
    print(f"\n== 案例中心线上冒烟: {'PASS' if not n_fail else 'FAIL'} "
          + (f"(失败: {fails})" if n_fail else ""))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
