# -*- coding: utf-8 -*-
"""发送闸门只读体检 CLI（P2 2026-08-13，重启装载后的验收工具）。

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

判定数学与生产同源（直接 import ``account_health.warmup_cap`` 爬坡纯函数），
但**刻意不经** ``build_account_signals``/单例 limiter——那条链会打开生产 DB
的写连接（SendCountStore 构造即 prune）。体检工具的本分是绝不碰生产状态。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))

_DAY = 86400.0


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


def _accounts(root: Path) -> list:
    """注册表账号清单 [(platform, account_id, created_at, status)]；无库 → 空。"""
    db = root / "config" / "account_registry.db"
    if not db.exists():
        return []
    try:
        con = _ro(db)
        rows = con.execute(
            "SELECT platform, account_id, created_at, status FROM platform_accounts"
        ).fetchall()
        con.close()
        return [(str(p or ""), str(a or ""), float(c or 0), str(s or ""))
                for p, a, c, s in rows]
    except Exception:
        return []


def _sends_24h(root: Path, key: str, now: float):
    """(used, frees_ts_of_nth) 取数与闸门同表同窗；库缺失 → (None, None)。"""
    db = root / "config" / "account_sends.db"
    if not db.exists():
        return None, None

    def _nth(con, n: int):
        row = con.execute(
            "SELECT ts FROM account_sends WHERE account_key=? AND ts>=? "
            "ORDER BY ts ASC LIMIT 1 OFFSET ?",
            (key, now - _DAY, max(0, n - 1)),
        ).fetchone()
        return float(row[0]) if row else None

    try:
        con = _ro(db)
        used = int(con.execute(
            "SELECT COUNT(*) FROM account_sends WHERE account_key=? AND ts>=?",
            (key, now - _DAY),
        ).fetchone()[0] or 0)
        out = (used, lambda n: _nth(con, n), con)
        return out[0], (out[1], out[2])
    except Exception:
        return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description="发送闸门只读体检")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    from src.skills.account_health import warmup_cap

    now = time.time()
    root = _resolve_data_root(args.data_root)
    cfg = _deep_merge(
        _load_yaml(root / "config" / "config.yaml"),
        _load_yaml(root / "config" / "config.local.yaml"),
    )
    gc = (cfg.get("companion_send_gate") or {})
    enabled = bool(gc.get("enabled", False))
    target = int(gc.get("target_cap", 15) or 15)
    start = int(gc.get("warmup_start_cap", 2) or 2)
    ramp = int(gc.get("warmup_ramp_days", 14) or 14)
    reserve = max(0, int(gc.get("reserve_for_manual", 0) or 0))
    peers = [str(x) for x in (gc.get("exempt_peers") or [])]

    report = {
        "data_root": str(root),
        "gate": {"enabled": enabled, "target_cap": target,
                 "warmup_start_cap": start, "warmup_ramp_days": ramp,
                 "reserve_for_manual": reserve, "exempt_peers": len(peers)},
        "accounts": [],
    }

    for plat, acct, created, status in _accounts(root):
        age_days = max(0.0, (now - created) / _DAY) if created > 0 else 0.0
        cap = warmup_cap(age_days, target, start_cap=start, ramp_days=ramp)
        auto_cap = max(0, cap - reserve)
        key = f"{plat}:{acct}"
        used, nth_ctx = _sends_24h(root, key, now)
        row = {
            "account": key, "status": status,
            "age_days": round(age_days, 1),
            "used_24h": used, "cap": cap, "auto_cap": auto_cap,
            "auto_verdict": "-", "manual_verdict": "-",
            "frees_at": None, "auto_frees_at": None,
        }
        if enabled and used is not None:
            row["auto_verdict"] = "BLOCK" if used >= auto_cap else "ok"
            row["manual_verdict"] = "BLOCK" if used >= cap else "ok"
        if nth_ctx is not None:
            nth, con = nth_ctx
            if enabled and used is not None:
                # 双道各自的首空位释放时刻（滚动窗第 used-cap+1 老记录 + 24h）
                if used >= cap:
                    ts = nth(used - cap + 1)
                    row["frees_at"] = (ts + _DAY) if ts is not None else None
                if used >= auto_cap:
                    ts2 = nth(used - auto_cap + 1)
                    row["auto_frees_at"] = (ts2 + _DAY) if ts2 is not None else None
            con.close()
        report["accounts"].append(row)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    g = report["gate"]
    print(f"data_root: {report['data_root']}")
    print(f"gate: enabled={g['enabled']} cap={g['target_cap']} "
          f"reserve_for_manual={g['reserve_for_manual']} "
          f"(auto 让路线={g['target_cap'] - g['reserve_for_manual']}) "
          f"ramp={g['warmup_start_cap']}→{g['target_cap']}/{g['warmup_ramp_days']}d "
          f"白名单={g['exempt_peers']} 条")
    if not report["accounts"]:
        print("(注册表无账号或库不可读)")
    for row in report["accounts"]:
        frees = ""
        if row["frees_at"]:
            frees = "  人工首空位≈" + time.strftime(
                "%H:%M", time.localtime(row["frees_at"]))
        elif row["auto_frees_at"]:
            frees = "  AI恢复≈" + time.strftime(
                "%H:%M", time.localtime(row["auto_frees_at"]))
        print(f"  {row['account']:<28} used24h={str(row['used_24h']):>4} "
              f"cap={row['cap']} auto_cap={row['auto_cap']} "
              f"auto={row['auto_verdict']} manual={row['manual_verdict']}"
              f"{frees}  (age={row['age_days']}d {row['status']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
