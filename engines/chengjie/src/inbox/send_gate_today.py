"""只读：各账号滚动 24h 发送量 vs 发送额度闸门（设置页「今日发送量」+ CLI 同源）。

刻意不经 ``SendCountStore`` / ``build_account_signals``——那条链会打开生产 DB
写连接（构造即 prune）。SQLite 一律 ``mode=ro``。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DAY = 86400.0


def data_root_from_config_path(config_path: Optional[str]) -> Optional[Path]:
    """从 config_manager.config_path 推实例数据根。

    生产：``…/data/config/config.yaml`` → 父目录名为 ``config`` → 再上一层是数据根。
    单测：``tmp_path/config.yaml`` → 父目录不是 ``config`` → 父目录即数据根
    （库落 ``tmp_path/config/*.db``）。
    """
    if not config_path:
        return None
    p = Path(config_path)
    parent = p.parent
    if parent.name == "config":
        return parent.parent
    return parent


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


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


def load_merged_config(data_root: Path) -> dict:
    root = Path(data_root)
    return _deep_merge(
        _load_yaml(root / "config" / "config.yaml"),
        _load_yaml(root / "config" / "config.local.yaml"),
    )


def _gate_from_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    gc = ((cfg or {}).get("companion_send_gate") or {}) if isinstance(cfg, dict) else {}
    if not isinstance(gc, dict):
        gc = {}
    try:
        target = int(gc.get("target_cap", 15) or 15)
    except (TypeError, ValueError):
        target = 15
    try:
        start = int(gc.get("warmup_start_cap", 2) or 2)
    except (TypeError, ValueError):
        start = 2
    try:
        ramp = int(gc.get("warmup_ramp_days", 14) or 14)
    except (TypeError, ValueError):
        ramp = 14
    try:
        reserve = max(0, int(gc.get("reserve_for_manual", 0) or 0))
    except (TypeError, ValueError):
        reserve = 0
    peers = gc.get("exempt_peers") or []
    n_peers = len(peers) if isinstance(peers, list) else 0
    return {
        "enabled": bool(gc.get("enabled", False)),
        "target_cap": max(0, target),
        "warmup_start_cap": max(0, start),
        "warmup_ramp_days": max(0, ramp),
        "reserve_for_manual": reserve,
        "exempt_peers": n_peers,
    }


def _accounts(root: Path) -> Optional[List[Tuple[str, str, float, str]]]:
    """注册表账号清单；库不存在 → None（available=false）；可读但空 → []。"""
    db = root / "config" / "account_registry.db"
    if not db.exists():
        return None
    try:
        con = _ro(db)
        try:
            rows = con.execute(
                "SELECT platform, account_id, created_at, status FROM platform_accounts"
            ).fetchall()
        finally:
            con.close()
        return [(str(p or ""), str(a or ""), float(c or 0), str(s or ""))
                for p, a, c, s in rows]
    except Exception:
        return None


def collect_send_gate_today(
    data_root: Path,
    config: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """各账号滚动 24h 用量 vs 闸门额度。纯只读。

    ``config`` 缺省则从数据根合并 yaml（CLI 用）；路由传入进程合并 config。
    """
    from src.skills.account_health import warmup_cap

    root = Path(data_root)
    ts = time.time() if now is None else float(now)
    cfg = config if isinstance(config, dict) else load_merged_config(root)
    gate = _gate_from_config(cfg)
    accounts = _accounts(root)
    out: Dict[str, Any] = {
        "available": accounts is not None,
        "gate": gate,
        "rows": [],
    }
    if accounts is None:
        return out

    enabled = bool(gate["enabled"])
    target = int(gate["target_cap"])
    start = int(gate["warmup_start_cap"])
    ramp = int(gate["warmup_ramp_days"])
    reserve = int(gate["reserve_for_manual"])
    sends_db = root / "config" / "account_sends.db"
    sends_con = None
    if sends_db.exists():
        try:
            sends_con = _ro(sends_db)
        except Exception:
            sends_con = None

    try:
        for plat, acct, created, status in accounts:
            age_days = max(0.0, (ts - created) / _DAY) if created > 0 else 0.0
            cap = warmup_cap(age_days, target, start_cap=start, ramp_days=ramp)
            auto_cap = max(0, cap - reserve)
            key = f"{plat}:{acct}"
            used = None
            frees_at = None
            auto_frees_at = None
            if sends_con is not None:
                try:
                    used = int(sends_con.execute(
                        "SELECT COUNT(*) FROM account_sends "
                        "WHERE account_key=? AND ts>=?",
                        (key, ts - _DAY),
                    ).fetchone()[0] or 0)
                except Exception:
                    used = None

                def _nth(n: int) -> Optional[float]:
                    if sends_con is None:
                        return None
                    row = sends_con.execute(
                        "SELECT ts FROM account_sends WHERE account_key=? AND ts>=? "
                        "ORDER BY ts ASC LIMIT 1 OFFSET ?",
                        (key, ts - _DAY, max(0, n - 1)),
                    ).fetchone()
                    return float(row[0]) if row else None

                if enabled and used is not None:
                    if used >= cap:
                        t0 = _nth(used - cap + 1)
                        frees_at = (t0 + _DAY) if t0 is not None else None
                    if used >= auto_cap:
                        t1 = _nth(used - auto_cap + 1)
                        auto_frees_at = (t1 + _DAY) if t1 is not None else None

            auto_verdict = "-"
            manual_verdict = "-"
            if enabled and used is not None:
                auto_verdict = "BLOCK" if used >= auto_cap else "ok"
                manual_verdict = "BLOCK" if used >= cap else "ok"
            out["rows"].append({
                "account": key,
                "status": status,
                "age_days": round(age_days, 1),
                "used_24h": used,
                "cap": cap,
                "auto_cap": auto_cap,
                "auto_verdict": auto_verdict,
                "manual_verdict": manual_verdict,
                "frees_at": frees_at,
                "auto_frees_at": auto_frees_at,
            })
    finally:
        if sends_con is not None:
            try:
                sends_con.close()
            except Exception:
                pass
    return out
