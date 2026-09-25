"""智能养号 · 账本（P1，2026-08-21）。

per-account 每日动作计数 + 最近动作时刻 + 有界影子样本环。JSON 落盘（实例数据根），
进程级锁 + 原子写。给调度器提供 ``snapshot()``（当日计数/上次时刻），给 engine 提供
``record_action()``（推进节奏 + dry 影子样本 / go_live 执行标记）。

关键语义：**dry_run 也推进 count/last_ts**——否则每 tick 都把同一条「到期」动作重排，
影子样本沦为刷屏、失去「一整天会怎么养」的模拟价值。dry_run 推进 = 忠实模拟日节奏。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_SHADOW_CAP = 200  # 影子样本环上限（防无界增长）


def _today(now: float) -> str:
    try:
        return datetime.fromtimestamp(float(now)).strftime("%Y%m%d")
    except Exception:
        return "0"


class NurtureLedger:
    def __init__(self, path: Any) -> None:
        self._path = Path(str(path))
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"accounts": {}, "shadow": []}
        self._load()
        # 进程级计数（重启清零；持久口径看 JSON）——health 快照用
        self.planned = 0
        self.executed = 0
        self.failed = 0
        self.by_kind: Dict[str, int] = {}

    def _load(self) -> None:
        try:
            if self._path.is_file():
                d = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self._data = {
                        "accounts": dict(d.get("accounts") or {}),
                        "shadow": list(d.get("shadow") or [])[-_SHADOW_CAP:],
                    }
        except Exception:
            self._data = {"accounts": {}, "shadow": []}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass  # 账本落盘 best-effort，绝不阻断养护/主链

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """给调度器：{key: {count_today, last_ts}}（当日计数按日期滚动清零）。"""
        n = float(now if now is not None else time.time())
        day = _today(n)
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, row in (self._data.get("accounts") or {}).items():
                same_day = str((row or {}).get("date") or "") == day
                out[str(key)] = {
                    "count_today": int((row or {}).get("count") or 0) if same_day else 0,
                    "last_ts": float((row or {}).get("last_ts") or 0.0),
                }
        return out

    def record_action(
        self, key: str, kind: str, *, now: Optional[float] = None,
        dry_run: bool = True, ok: bool = True, detail: str = "",
    ) -> None:
        """推进该号节奏（count/last_ts）+ 记影子样本。dry_run 与 go_live 都推进节奏。"""
        n = float(now if now is not None else time.time())
        day = _today(n)
        with self._lock:
            accts = self._data.setdefault("accounts", {})
            row = accts.get(key) or {}
            if str(row.get("date") or "") != day:
                row = {"date": day, "count": 0, "last_ts": 0.0}
            row["count"] = int(row.get("count") or 0) + 1
            row["last_ts"] = n
            row["date"] = day
            accts[key] = row
            shadow = self._data.setdefault("shadow", [])
            shadow.append({
                "ts": n, "key": str(key), "kind": str(kind),
                "dry_run": bool(dry_run), "ok": bool(ok),
                "detail": str(detail or "")[:200],
            })
            if len(shadow) > _SHADOW_CAP:
                del shadow[:-_SHADOW_CAP]
            self._save()
        # 进程计数
        self.planned += 1
        if not dry_run:
            if ok:
                self.executed += 1
            else:
                self.failed += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def recent_shadow(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.get("shadow") or [])[-int(max(1, limit)):][::-1]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            n_accts = len(self._data.get("accounts") or {})
            n_shadow = len(self._data.get("shadow") or [])
        return {
            "planned": self.planned, "executed": self.executed, "failed": self.failed,
            "by_kind": dict(self.by_kind),
            "accounts_tracked": n_accts, "shadow_len": n_shadow,
        }


_singleton: Optional[NurtureLedger] = None
_singleton_path: str = ""


def get_nurture_ledger(path: Any = None) -> NurtureLedger:
    """按路径单例（路径变则重建，与其它 store 单例同款）。"""
    global _singleton, _singleton_path
    if path:
        p = str(path)
    else:
        from src.licensing.data_paths import cwd_or_data_file
        p = str(cwd_or_data_file("nurture_ledger.json"))
    if _singleton is None or p != _singleton_path:
        _singleton = NurtureLedger(p)
        _singleton_path = p
    return _singleton


__all__ = ["NurtureLedger", "get_nurture_ledger"]
