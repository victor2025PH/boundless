# -*- coding: utf-8 -*-
"""智能养号 · 只读复盘 CLI（P2，2026-08-21）。

离线检视养号引擎状态：引擎档位（pause/dry_run/go_live）+ 每号方案与账本计数 +
影子样本尾（dry_run 会怎么养 / go_live 执行记录）+ 配置级闸门原因。多实例逐根跑
（``scripts/_data_root`` 契约），全程只读（NurtureLedger 只读方法 + 配置只读）。

用法：
    python tools/nurture_review.py [--data-root PATH] [--shadow N] [--json]

注：stage/风险(flood/errors) 属**运行时信号**，CLI 无 live 账号信号 → account_status 以
空信号给「配置级」判词（disabled/off_hours/no_behaviors/budget/gap/due）；stage/risk 级
判词以运行引擎的 GET /api/nurture/status 为准。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402


def _review_root(root: Path, shadow_n: int, now: float) -> dict:
    cfg = load_merged_config(root)
    ncfg = ((cfg.get("ops") or {}).get("nurture") or {})
    accts_cfg = ncfg.get("accounts") or {}
    canary = list(ncfg.get("canary_accounts") or [])

    # 引擎档位（config 口径；running 需 live API）
    enabled = bool(ncfg.get("enabled", False))
    dry_run = bool(ncfg.get("dry_run", True))
    mode = "pause" if not enabled else ("dry_run" if dry_run else "go_live")

    from src.nurture.nurture_ledger import NurtureLedger
    from src.nurture.nurture_scheduler import account_status, lint_nurture_config
    led = NurtureLedger(Path(root) / "config" / "nurture_ledger.json")
    snap = led.snapshot(now=now)
    # 配置防呆（registry_keys=None：离线不查陈旧号，其余 go_live 误配照查）
    warnings = lint_nurture_config(ncfg, registry_keys=None)

    accounts = []
    if isinstance(accts_cfg, dict):
        for key, plan in accts_cfg.items():
            key = str(key)
            acct = {"key": key, "plan": plan if isinstance(plan, dict) else {}}
            st = account_status(acct, snap, {}, now, ncfg)  # 空信号=配置级判词
            accounts.append({
                "key": key,
                "enabled": st["enabled"], "profile": st["profile"],
                "count_today": st["count_today"], "budget": st["budget"],
                "reason": st["reason"], "next_kind": st["next_kind"],
                "in_canary": key in canary,
            })

    return {
        "root": str(root),
        "mode": mode, "enabled": enabled, "dry_run": dry_run,
        "interval_sec": ncfg.get("interval_sec", 900),
        "canary": canary, "hours": ncfg.get("hours"),
        "risk_backoff": ncfg.get("risk_backoff", {"enabled": True}),
        "accounts": accounts,
        "warnings": warnings,
        "ledger_stats": led.stats(),
        "shadow": led.recent_shadow(shadow_n),
    }


def _print_root(r: dict) -> None:
    print(f"\n=== nurture @ {r['root']} ===")
    print(f"  engine: {r['mode']}  (enabled={r['enabled']} dry_run={r['dry_run']} "
          f"interval={r['interval_sec']}s)  canary={len(r['canary'])}  hours={r['hours']}")
    for w in r.get("warnings") or []:
        key = (" " + w["key"]) if w.get("key") else ""
        print(f"  [{str(w.get('severity','')).upper()}] {w.get('code','')}{key}")
    st = r["ledger_stats"]
    print(f"  ledger: planned={st.get('planned',0)} executed={st.get('executed',0)} "
          f"failed={st.get('failed',0)} by_kind={st.get('by_kind',{})}")
    if not r["accounts"]:
        print("  accounts: (none configured — ops.nurture.accounts 为空)")
    else:
        print("  accounts (config-level status; stage/risk live via API):")
        for a in r["accounts"]:
            tag = "canary" if a["in_canary"] else "-"
            print(f"    {a['key']:<28} {a['profile']:<12} on={str(a['enabled']):<5} "
                  f"{a['count_today']}/{a['budget']:<4} {a['reason']:<16} "
                  f"next={a['next_kind'] or '-':<9} [{tag}]")
    sh = r["shadow"]
    if sh:
        print(f"  recent shadow ({len(sh)}):")
        for s in sh[:12]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(s.get("ts") or 0)))
            dr = "dry" if s.get("dry_run") else ("ok" if s.get("ok") else "FAIL")
            print(f"    {ts}  {str(s.get('key','')):<26} {str(s.get('kind','')):<10} "
                  f"{dr:<5} {s.get('detail','')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="智能养号只读复盘")
    ap.add_argument("--data-root", default="", help="显式实例数据根（默认自动发现活跃实例）")
    ap.add_argument("--shadow", type=int, default=20, help="影子样本尾条数")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--lint", action="store_true",
                    help="只查配置防呆；有 warn 级 finding 时以非零码退出（供夜间告警）")
    args = ap.parse_args()
    now = time.time()
    roots = resolve_data_roots(args.data_root)
    reports = []
    for root in roots:
        try:
            reports.append(_review_root(root, args.shadow, now))
        except Exception as ex:  # noqa: BLE001
            reports.append({"root": str(root), "error": str(ex)})
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            if r.get("error"):
                print(f"\n=== nurture @ {r['root']} ===\n  ERROR: {r['error']}")
            elif args.lint:
                ws = r.get("warnings") or []
                print(f"\n=== nurture lint @ {r['root']} ===")
                if not ws:
                    print("  clean (no config footguns)")
                for w in ws:
                    key = (" " + w["key"]) if w.get("key") else ""
                    print(f"  [{str(w.get('severity','')).upper()}] {w.get('code','')}{key}")
            else:
                _print_root(r)
    if args.lint:
        has_warn = any(w.get("severity") == "warn"
                       for r in reports for w in (r.get("warnings") or []))
        return 1 if has_warn else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
