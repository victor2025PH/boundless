# -*- coding: utf-8 -*-
"""发送闸门只读体检 CLI（P2 2026-08-13；P1 2026-08-29 收口到 send_gate_today）。

回答四个问题（全部只读，SQLite 一律 ``mode=ro`` URI，零写事务、零单例、
零生产进程依赖——服务宕着也能跑）：

1. 闸门配置现在是什么（enabled / target_cap / reserve_for_manual / 白名单几条）
2. 各账号滚动 24h 已发多少（与闸门同一张 ``account_sends`` 表、同窗口口径）
3. 双道判定各是什么（自动链 auto_cap / 人工 cap，谁被拦谁放行）
4. 额度拦满时首个空位几点释放（第 used-cap+1 老的发送记录 + 24h）

用法::

    python tools/verify_send_gate.py            # 自动发现活跃实例数据根
    python tools/verify_send_gate.py --data-root D:/chengjie-instances/zhiliao/data
    python tools/verify_send_gate.py --json     # 机器可读（接监控/巡检）

判定数学与生产同源（``src.inbox.send_gate_today`` + ``account_health.warmup_cap``），
刻意不经 ``build_account_signals`` / ``SendCountStore``（构造即 prune）。
``--json`` 仍输出 ``accounts``（= collector 的 ``rows``），保持旧消费方契约。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))


def _resolve_data_root(cli: str = "") -> Path:
    if cli:
        return Path(cli)
    try:
        from scripts._data_root import resolve_data_roots
        roots = resolve_data_roots()
        if roots:
            return Path(roots[0])
    except Exception:
        pass
    return _ENGINE_ROOT


def main() -> int:
    ap = argparse.ArgumentParser(description="发送闸门只读体检")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    from src.inbox.send_gate_today import collect_send_gate_today

    root = _resolve_data_root(args.data_root)
    snap = collect_send_gate_today(root)
    report = {
        "data_root": str(root),
        "gate": snap.get("gate") or {},
        "accounts": snap.get("rows") or [],
        "available": bool(snap.get("available")),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    g = report["gate"]
    print(f"data_root: {report['data_root']}")
    print(f"gate: enabled={g.get('enabled')} cap={g.get('target_cap')} "
          f"reserve_for_manual={g.get('reserve_for_manual')} "
          f"(auto 让路线={int(g.get('target_cap') or 0) - int(g.get('reserve_for_manual') or 0)}) "
          f"ramp={g.get('warmup_start_cap')}→{g.get('target_cap')}/{g.get('warmup_ramp_days')}d "
          f"白名单={g.get('exempt_peers')} 条")
    if not report["accounts"]:
        print("(注册表无账号或库不可读)" if not report["available"]
              else "(注册表无账号)")
    for row in report["accounts"]:
        frees = ""
        if row.get("frees_at"):
            frees = "  人工首空位≈" + time.strftime(
                "%H:%M", time.localtime(row["frees_at"]))
        elif row.get("auto_frees_at"):
            frees = "  AI恢复≈" + time.strftime(
                "%H:%M", time.localtime(row["auto_frees_at"]))
        print(f"  {str(row.get('account') or ''):<28} used24h={str(row.get('used_24h')):>4} "
              f"cap={row.get('cap')} auto_cap={row.get('auto_cap')} "
              f"auto={row.get('auto_verdict')} manual={row.get('manual_verdict')}"
              f"{frees}  (age={row.get('age_days')}d {row.get('status')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
