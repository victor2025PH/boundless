# -*- coding: utf-8 -*-
"""渠道接入进度（实施99 P1-2，2026-09-08）——把「教程」变成「有状态的向导」。

两类状态来源：
- ``manual``：用户在步骤卡上点「标记完成 / 已提交审核（预计 N 天）/ 受阻 / 重置」——审核类步骤只能靠人说；
- ``auto``：处理器在事实发生时写入（如 TikTok webhook 注册成功 → 第 2 步 done），或由自检 ``onboarding_status`` 现算
  （凭证已配 / 账号已授权 / 首条进线）——自动值**覆盖**手动值。

``submitted`` 带 ``due_ts``：到期仍未变成 done → ``overdue``，状态灯转琥珀并在步骤卡提示「预计结果日已过」。
SQLite 落 ``config_dir()/onboarding_progress.db``，键 ``(slug, step_no)``，纯本地。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

STATES = ("todo", "doing", "submitted", "done", "blocked")

_DDL = """
CREATE TABLE IF NOT EXISTS steps (
    slug       TEXT NOT NULL,
    step_no    INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'todo',
    source     TEXT NOT NULL DEFAULT 'manual',
    note       TEXT NOT NULL DEFAULT '',
    due_ts     REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (slug, step_no)
);
"""


class OnboardingProgress:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self.default_path()
        if self.path != ":memory:":
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            except Exception:
                pass
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    @staticmethod
    def default_path() -> str:
        try:
            from src.licensing.data_paths import config_dir
            return str(config_dir() / "onboarding_progress.db")
        except Exception:
            return os.path.join("config", "onboarding_progress.db")

    def set(self, slug: str, step_no: int, *, state: str, source: str = "manual", note: str = "",
            due_ts: float = 0.0, now: Optional[float] = None) -> Dict[str, Any]:
        st = str(state or "todo")
        if st not in STATES:
            raise ValueError(f"bad state: {state}")
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO steps(slug, step_no, state, source, note, due_ts, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(slug, step_no) DO UPDATE SET state=excluded.state, source=excluded.source, note=excluded.note, "
                "due_ts=excluded.due_ts, updated_at=excluded.updated_at",
                (str(slug), int(step_no), st, str(source or "manual"), str(note or "")[:300], float(due_ts or 0), t))
            self._conn.commit()
        return self.get(slug, step_no) or {}

    def get(self, slug: str, step_no: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM steps WHERE slug=? AND step_no=?", (str(slug), int(step_no))).fetchone()
        return dict(row) if row else None

    def all(self, slug: str) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM steps WHERE slug=? ORDER BY step_no", (str(slug),)).fetchall()
        return {int(r["step_no"]): dict(r) for r in rows}

    def clear(self, slug: str, step_no: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM steps WHERE slug=? AND step_no=?", (str(slug), int(step_no)))
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def merge_steps(total: int, manual: Dict[int, Dict[str, Any]], auto: Dict[int, str], now: float) -> list:
    """按步合并：auto（现算事实）> manual > todo；``submitted`` 且 ``due_ts`` 已过 → ``overdue=True``。"""
    out = []
    for n in range(1, int(total) + 1):
        m = manual.get(n) or {}
        a = auto.get(n)
        if a:
            state, source, note, due = a, "auto", "", 0.0
        else:
            state = str(m.get("state") or "todo")
            source = str(m.get("source") or ("manual" if m else ""))
            note = str(m.get("note") or "")
            due = float(m.get("due_ts") or 0)
        overdue = bool(state == "submitted" and due > 0 and now > due)
        due_date = time.strftime("%Y-%m-%d", time.localtime(due)) if due > 0 else ""
        out.append({"n": n, "state": state, "source": source, "note": note, "due_ts": due, "due_date": due_date,
                    "overdue": overdue, "auto": bool(a)})
    return out


_STORE: Optional[OnboardingProgress] = None
_STORE_LOCK = threading.Lock()


def get_progress(path: Optional[str] = None) -> OnboardingProgress:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = OnboardingProgress(path)
        return _STORE


def _reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


__all__ = ["STATES", "OnboardingProgress", "merge_steps", "get_progress"]
