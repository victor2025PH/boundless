# -*- coding: utf-8 -*-
"""挽回效果统计（RH-P2，纯函数核心）——「发现→话术→发送→回来了」的最后一环。

判定口径（与页面 KPI / 周报 CLI 单一事实源）：
- 样本＝``journey_events`` 里的 ``reactivation_sent`` 事件（坐席点「标记已发/
  直接发送」经 ``ReactivationScheduler.mark_sent`` 落的账）；
- 挽回成功＝发送后 ``reply_window_days`` 内**同 journey** 出现 ``msg_in``；
- **分母只算已到期样本**（matured：已回复 或 观察窗已满）——刚发出去还没到窗
  的记 pending 不记失败，防止把「还没来得及回」压成低挽回率；
- ``until_offset_days`` 支持周环比（上一窗口的 sent 也用**真实 now** 判成熟度，
  绝不把「窗口边界」当「现在」用——那会把上周尾部的发送错判成未到期）。

消费方：
- 路由薄包装 ``GET /api/relations/winback-stats``（contacts_routes.py，随
  contacts 子系统条件注册）；
- 周报 CLI ``scripts/winback_report.py``（data-root 契约，只读连接）。

设计说明：本模块接**裸 sqlite 连接 + 可选锁**而非 ContactStore——CLI 场景必须
mode=ro 打开活体生产库（实例服务同时在写），实例化 ContactStore 会跑 DDL/迁移
写事务，违背只读纪律。门禁 ``tests/test_winback_stats.py``。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DAYS = 30
DEFAULT_REPLY_WINDOW_DAYS = 7


def readonly_conn(contacts_db_path: Path) -> sqlite3.Connection:
    """打开活体 contacts.db 的只读连接（CLI 用；服务进程内请直接用 store）。"""
    con = sqlite3.connect(
        f"file:{Path(contacts_db_path).as_posix()}?mode=ro", uri=True, timeout=10,
    )
    con.row_factory = sqlite3.Row
    return con


def compute_winback(
    conn: sqlite3.Connection,
    lock: Any = None,
    *,
    days: int = DEFAULT_DAYS,
    reply_window_days: int = DEFAULT_REPLY_WINDOW_DAYS,
    until_offset_days: int = 0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """统计 [now-offset-days, now-offset) 窗口内 reactivation_sent 的挽回漏斗。"""
    days_n = max(1, min(int(days or DEFAULT_DAYS), 365))
    win_d = max(1, min(int(reply_window_days or DEFAULT_REPLY_WINDOW_DAYS), 30))
    off_d = max(0, int(until_offset_days or 0))
    ts_now = float(now if now is not None else time.time())
    until = ts_now - off_d * 86400
    since = until - days_n * 86400
    win_s = win_d * 86400
    ctx = lock if lock is not None else nullcontext()
    with ctx:
        rows = conn.execute(
            "SELECT e.journey_id, e.ts, "
            "  EXISTS(SELECT 1 FROM journey_events m "
            "         WHERE m.journey_id=e.journey_id "
            "           AND m.event_type='msg_in' "
            "           AND m.ts>e.ts AND m.ts<=e.ts+?) AS replied "
            "FROM journey_events e "
            "WHERE e.event_type='reactivation_sent' "
            "  AND e.ts>=? AND e.ts<? "
            "ORDER BY e.ts DESC LIMIT 1000",
            (win_s, since, until),
        ).fetchall()
    sent = len(rows)
    replied = sum(1 for r in rows if r["replied"])
    # 成熟度永远按真实 now 判（历史窗口的样本天然全成熟）
    matured = sum(
        1 for r in rows
        if r["replied"] or (ts_now - float(r["ts"])) >= win_s)
    pending = sent - matured
    rate = round(replied / matured, 3) if matured else None
    return {
        "ok": True,
        "days": days_n,
        "reply_window_days": win_d,
        "until_offset_days": off_d,
        "sent": sent,
        "replied": replied,
        "matured": matured,
        "pending": pending,
        "rate": rate,
    }


def recent_reactivations(
    conn: sqlite3.Connection,
    lock: Any = None,
    *,
    limit: int = 10,
    reply_window_days: int = DEFAULT_REPLY_WINDOW_DAYS,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """最近 N 条挽回发送（带客户名/回复状态），周报「逐条看」用。"""
    win_s = max(1, min(int(reply_window_days or 7), 30)) * 86400
    ts_now = float(now if now is not None else time.time())
    lim = max(1, min(int(limit or 10), 100))
    ctx = lock if lock is not None else nullcontext()
    with ctx:
        rows = conn.execute(
            "SELECT e.journey_id, e.ts, c.primary_name AS name, "
            "  EXISTS(SELECT 1 FROM journey_events m "
            "         WHERE m.journey_id=e.journey_id AND m.event_type='msg_in' "
            "           AND m.ts>e.ts AND m.ts<=e.ts+?) AS replied "
            "FROM journey_events e "
            "LEFT JOIN journeys j ON j.journey_id=e.journey_id "
            "LEFT JOIN contacts c ON c.contact_id=j.contact_id "
            "WHERE e.event_type='reactivation_sent' "
            "ORDER BY e.ts DESC LIMIT ?",
            (win_s, lim),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        replied = bool(r["replied"])
        out.append({
            "journey_id": str(r["journey_id"]),
            "name": str(r["name"] or ""),
            "ts": float(r["ts"]),
            "replied": replied,
            "pending": (not replied) and (ts_now - float(r["ts"])) < win_s,
        })
    return out
