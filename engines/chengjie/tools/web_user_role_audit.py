# -*- coding: utf-8 -*-
"""坐席最小权限审计清单（只读）——智安 P1-6，2026-10-08。

用法（在目标实例本机执行；本工具只读打开 web_users.db，不改任何账号/会话）::

    python tools/web_user_role_audit.py --db config/web_users.db [--max-admins 2] [--stale-days 30] [--json]

输出：角色分布 + 发现项（admin 超上限 / 久未登录 / 按人放开的额外权限 / 会话过多）及建议。
不输出密码、哈希、会话令牌、通知绑定。降级 / 禁用由运营在「用户管理」页人工执行。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.web_user_store import DEFAULT_MAX_ADMINS, least_privilege_report  # noqa: E402


class _ReadOnlyUsers:
    """最小只读视图：只提供 least_privilege_report 需要的三个查询。"""

    def __init__(self, db: Path):
        self._conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def _has_table(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def list_users(self) -> List[Dict]:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(web_users)")}
        want = [c for c in ("id", "username", "role", "display_name", "created_at",
                            "last_login", "enabled", "perms_json") if c in cols]
        rows = self._conn.execute(f"SELECT {', '.join(want)} FROM web_users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def last_active_map(self) -> Dict[str, str]:
        if not self._has_table("web_sessions"):
            return {}
        rows = self._conn.execute(
            "SELECT username, MAX(last_seen) AS ts FROM web_sessions GROUP BY username").fetchall()
        return {str(r["username"]): str(r["ts"] or "") for r in rows if r["ts"]}

    def last_session_login_map(self) -> Dict[str, str]:
        return self.last_active_map()

    def session_counts(self) -> Dict[str, int]:
        if not self._has_table("web_sessions"):
            return {}
        rows = self._conn.execute(
            "SELECT username, COUNT(*) AS n FROM web_sessions WHERE revoked=0 GROUP BY username").fetchall()
        return {str(r["username"]): int(r["n"] or 0) for r in rows}


def build_report(db: Path, *, max_admins: int = DEFAULT_MAX_ADMINS, stale_days: int = 30) -> Dict:
    view = _ReadOnlyUsers(db)
    try:
        return least_privilege_report(view, max_admins=max_admins, stale_days=stale_days)
    finally:
        view.close()


def _render(rep: Dict) -> str:
    lines = [f"生成时间 {rep['generated_at']}  用户 {rep['users']}  启用 admin {rep['admins']}"
             f"（上限 {rep['max_admins'] or '不限'}）",
             "角色分布：" + "，".join(f"{k}={v}" for k, v in rep["roles"].items())]
    if not rep["findings"]:
        lines.append("无发现项。")
    for f in rep["findings"]:
        extra = ""
        if f.get("perms"):
            extra = " perms=" + ",".join(f["perms"])
        if f.get("sessions"):
            extra = f" sessions={f['sessions']}"
        lines.append(f"- [{f['issue']}] {f['username']}（{f['role']}）"
                     f" 最近活跃={f.get('last_login') or '-'}{extra} → {f['suggestion']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="坐席最小权限审计清单（只读）")
    ap.add_argument("--db", default="config/web_users.db")
    ap.add_argument("--max-admins", type=int, default=DEFAULT_MAX_ADMINS)
    ap.add_argument("--stale-days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db)
    if not db.is_file():
        print(f"找不到 {db}", file=sys.stderr)
        return 2
    rep = build_report(db, max_admins=a.max_admins, stale_days=a.stale_days)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else _render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
