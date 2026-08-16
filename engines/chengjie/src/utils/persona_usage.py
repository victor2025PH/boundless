"""人设使用计数账本 —— 统计「每个人设每天生成了多少条 AI 回复」。

轻量 sqlite 模块级单例。埋点位置选在 ``PersonaManager.format_persona_block``：
所有生产回复链路（ai_client._build_system_instruction、skill_manager autodraft 等）
最终都经它拼人设块，是「一个人设真的被用来生成回复」的唯一收口点——在各调用方
分别埋点必漏（历史上 persona 解析入口就散落过 3 处）。预览/管理类调用传
``record_usage=False`` 跳过，避免运营点预览刷虚计数。

设计约束（**绝不阻塞/破坏回复链路**）：
- ``record()`` / ``counts()`` 任何异常一律吞掉只 logger.debug——统计丢一条无所谓，
  回复发不出去是事故；
- 内部自带 threading.Lock（Web 线程与主循环会并发调用），连接
  ``check_same_thread=False``，所有操作持锁；
- 未显式 ``init()`` 先用时惰性连默认路径（$AITR_DATA_DIR/config 或 <cwd>/config，
  与 config_manager 的 config 目录定位同口径）；persona_routes 注册时会用
  config.yaml 同目录显式 init。
- init 时顺带清理 90 天前旧行（表恒小，无需摊还清理）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 90  # init 时清理早于此天数的旧行

_DDL = """
CREATE TABLE IF NOT EXISTS usage_daily (
    persona_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (persona_id, day)
);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[Path] = None


def _default_db_path() -> Path:
    """惰性默认路径：$AITR_DATA_DIR/config/persona_usage.db，否则 <cwd>/config/。"""
    env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / "config" / "persona_usage.db"
    return Path.cwd() / "config" / "persona_usage.db"


def _connect_locked(path: Path) -> None:
    """（须持锁）连到指定 db：建目录 → 连接 → WAL → 建表 → 清理 90 天前旧行。"""
    global _conn, _db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.commit()
    _conn = conn
    _db_path = path
    # 顺带清理陈旧行（异常忽略——清理失败不影响记账）
    try:
        cutoff = (_dt.date.today() - _dt.timedelta(days=_RETENTION_DAYS)).isoformat()
        conn.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
        conn.commit()
    except Exception:
        logger.debug("[persona-usage] 清理旧行失败（忽略）", exc_info=True)


def _ensure_conn_locked() -> sqlite3.Connection:
    """（须持锁）未 init 先用时惰性连默认路径。"""
    if _conn is None:
        _connect_locked(_default_db_path())
    assert _conn is not None
    return _conn


def init(db_path: Any) -> None:
    """显式指定 DB 路径（persona_routes 注册时调用）。

    幂等：同路径重复调用无副作用；换路径则关旧连接重连。
    """
    global _conn, _db_path
    path = Path(db_path).expanduser()
    with _lock:
        if _conn is not None and _db_path == path:
            return
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            _db_path = None
        _connect_locked(path)


def record(persona_id: str) -> None:
    """当天（本地时区）该人设计数 +1。任何异常吞掉只 debug——绝不影响回复链路。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return
    try:
        with _lock:
            conn = _ensure_conn_locked()
            day = _dt.date.today().isoformat()
            conn.execute(
                "INSERT INTO usage_daily (persona_id, day, count) VALUES (?, ?, 1) "
                "ON CONFLICT(persona_id, day) DO UPDATE SET count = count + 1",
                (pid, day),
            )
            conn.commit()
    except Exception:
        logger.debug("[persona-usage] record 失败（忽略）", exc_info=True)


def counts(days: int = 7) -> Dict[str, int]:
    """最近 N 天（含今天）按 persona_id 汇总计数。异常安全退化 {}。"""
    try:
        n = max(1, int(days))
        since = (_dt.date.today() - _dt.timedelta(days=n - 1)).isoformat()
        with _lock:
            conn = _ensure_conn_locked()
            rows = conn.execute(
                "SELECT persona_id, SUM(count) FROM usage_daily "
                "WHERE day >= ? GROUP BY persona_id",
                (since,),
            ).fetchall()
        return {str(r[0]): int(r[1] or 0) for r in rows}
    except Exception:
        logger.debug("[persona-usage] counts 失败（忽略）", exc_info=True)
        return {}


__all__ = ["init", "record", "counts"]
