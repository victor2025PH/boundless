"""FateX 独立生辰画像库（SQLite，产品级数据分离）。

主产品的记忆（episodic）继续记「用户说过生辰」这件事；但 FateX 的**权威生辰数据**
从「episodic 前缀扫描」升级为本库的结构化行——键 = ``(platform, account_id, chat_key)``
三元组，**天然账号隔离**（双号同 peer 各自一行，杜绝 P0-2 键格式失配一族的问题）。

设计对齐 ``persona_media_store``（线程安全 SQLite + 模块级单例 + 启动期 configure）：
- 单连接 + Lock，``check_same_thread=False``，WAL；``:memory:`` 供单测。
- 全方法软失败（返 None/False + debug 日志），FateX 故障绝不阻断主产品聊天链。
- upsert 带「补全合并」语义：用户常先报生日、隔几轮再补时辰/性别——新值缺失位
  不覆盖旧已知位；年月日/历法则新值为准（用户更正生日场景）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认库路径（与其它 config/*.db 同处）；main.py 启动期用 configure_fatex_store 显式指定
# （双实例部署下随各实例 config 目录走，天然实例隔离）。
DEFAULT_DB_PATH = "config/fatex.db"

_DDL = """
CREATE TABLE IF NOT EXISTS birth_profiles (
    platform    TEXT NOT NULL DEFAULT '',
    account_id  TEXT NOT NULL DEFAULT '',
    chat_key    TEXT NOT NULL,
    birth_year  INTEGER NOT NULL DEFAULT 0,
    birth_month INTEGER NOT NULL DEFAULT 0,
    birth_day   INTEGER NOT NULL DEFAULT 0,
    birth_hour  INTEGER NOT NULL DEFAULT -1,
    birth_min   INTEGER NOT NULL DEFAULT 0,
    is_lunar    INTEGER NOT NULL DEFAULT 0,
    gender      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    raw_text    TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, account_id, chat_key)
);
"""


def _norm(v: Any) -> str:
    return str(v or "").strip()


class FatexStore:
    """FateX 产品数据层（当前：生辰画像；后续产品数据一律进本库不进主库）。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ── 生辰画像 ─────────────────────────────────────────────────────────────

    def upsert_birth(
        self,
        platform: str,
        account_id: str,
        chat_key: str,
        info: Any,
        *,
        source: str = "user_stated",
        raw_text: str = "",
    ) -> bool:
        """写入/合并生辰（info = ``bazi_engine.BirthInfo``）。

        合并语义：新 info 时辰未知(-1)/性别空 → 保留旧已知值；年月日/历法新值覆盖。
        """
        ck = _norm(chat_key)
        if not ck or info is None:
            return False
        try:
            y, mo, d = int(info.year), int(info.month), int(info.day)
            hh = int(getattr(info, "hour", -1))
            mi = int(getattr(info, "minute", 0) or 0)
            lunar = 1 if getattr(info, "is_lunar", False) else 0
            g = _norm(getattr(info, "gender", ""))
        except (TypeError, ValueError, AttributeError):
            return False
        now = time.time()
        try:
            with self._lock:
                old = self._conn.execute(
                    "SELECT * FROM birth_profiles WHERE platform=? AND account_id=? AND chat_key=?",
                    (_norm(platform), _norm(account_id), ck),
                ).fetchone()
                if old is not None:
                    if not (0 <= hh <= 23) and 0 <= int(old["birth_hour"]) <= 23:
                        hh, mi = int(old["birth_hour"]), int(old["birth_min"])
                    if not g:
                        g = _norm(old["gender"])
                self._conn.execute(
                    """INSERT INTO birth_profiles
                       (platform, account_id, chat_key, birth_year, birth_month,
                        birth_day, birth_hour, birth_min, is_lunar, gender,
                        source, raw_text, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(platform, account_id, chat_key) DO UPDATE SET
                         birth_year=excluded.birth_year,
                         birth_month=excluded.birth_month,
                         birth_day=excluded.birth_day,
                         birth_hour=excluded.birth_hour,
                         birth_min=excluded.birth_min,
                         is_lunar=excluded.is_lunar,
                         gender=excluded.gender,
                         source=excluded.source,
                         raw_text=excluded.raw_text,
                         updated_at=excluded.updated_at""",
                    (_norm(platform), _norm(account_id), ck, y, mo, d, hh, mi,
                     lunar, g, _norm(source), _norm(raw_text)[:400],
                     now, now),
                )
                self._conn.commit()
            return True
        except sqlite3.Error:
            logger.debug("[fatex] upsert_birth 失败（软忽略）", exc_info=True)
            return False

    def get_birth(
        self, platform: str, account_id: str, chat_key: str,
    ) -> Optional[Any]:
        """按会话三元组取生辰 → ``BirthInfo``；无记录/异常 → None。

        兼容读：精确 (platform, account, chat) 未命中时回落 account='' 的旧行
        （迁移前写入的存量），绝不跨账号取。
        """
        ck = _norm(chat_key)
        if not ck:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM birth_profiles WHERE platform=? AND account_id=? AND chat_key=?",
                    (_norm(platform), _norm(account_id), ck),
                ).fetchone()
                if row is None and _norm(account_id):
                    row = self._conn.execute(
                        "SELECT * FROM birth_profiles WHERE platform=? AND account_id='' AND chat_key=?",
                        (_norm(platform), ck),
                    ).fetchone()
            if row is None:
                return None
            from src.companion.bazi_engine import BirthInfo
            info = BirthInfo(
                year=int(row["birth_year"]), month=int(row["birth_month"]),
                day=int(row["birth_day"]), hour=int(row["birth_hour"]),
                minute=int(row["birth_min"]),
                is_lunar=bool(row["is_lunar"]),
                gender=str(row["gender"] or ""),
            )
            return info if info.valid() else None
        except sqlite3.Error:
            logger.debug("[fatex] get_birth 失败（软忽略）", exc_info=True)
            return None

    def delete_birth(self, platform: str, account_id: str, chat_key: str) -> bool:
        """删除生辰（用户要求遗忘时随主链 forget 一起清）。"""
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM birth_profiles WHERE platform=? AND account_id=? AND chat_key=?",
                    (_norm(platform), _norm(account_id), _norm(chat_key)),
                )
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ── 观测 ────────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """备货观测：总数 + 按平台分布 + 时辰/性别完整度（隔离健康卡用）。"""
        try:
            with self._lock:
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM birth_profiles").fetchone()[0]
                by_plat = self._conn.execute(
                    "SELECT platform, COUNT(*) c FROM birth_profiles GROUP BY platform",
                ).fetchall()
                full = self._conn.execute(
                    "SELECT COUNT(*) FROM birth_profiles WHERE birth_hour>=0 AND gender<>''",
                ).fetchone()[0]
            return {
                "birth_profiles": int(total),
                "by_platform": {str(r["platform"] or "?"): int(r["c"]) for r in by_plat},
                "complete": int(full),
            }
        except sqlite3.Error:
            return {"birth_profiles": 0, "by_platform": {}, "complete": 0}

    def list_rows(self, limit: int = 200) -> List[Dict[str, Any]]:
        """迁移/巡检用途的只读导出（脱敏由调用方负责）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM birth_profiles ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _resolved_db_path(db_path: Any) -> str:
    from src.licensing.data_paths import resolve_legacy_config_path
    return resolve_legacy_config_path(db_path)


# ── 模块级单例（与 persona_media_store 同范式） ─────────────────────────────────

_STORE: Optional[FatexStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure_fatex_store(db_path: Any = DEFAULT_DB_PATH) -> Optional[FatexStore]:
    """启动期装配。同路径幂等；路径变了则关掉旧连接再换库。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        path = _resolved_db_path(db_path)
        if _STORE is not None and _DB_PATH != path:
            try:
                _STORE.close()
            except Exception:
                logger.debug("[fatex] 旧库关闭失败（忽略）", exc_info=True)
            _STORE = None
        _DB_PATH = path
        if _STORE is None:
            try:
                _STORE = FatexStore(_DB_PATH)
            except Exception:
                logger.warning("[fatex] 建库失败", exc_info=True)
                _STORE = None
        return _STORE


_DISABLED = False


def disable_fatex_store() -> None:
    """P-4 #254：FateX 产品关着时由 main.py 调用——之后 :func:`get_fatex_store` 恒返 None，
    不再按默认路径懒建 ``config/fatex.db``（生辰记忆双写 / ops 隔离体检那几处调用软失败）。
    用户版陪伴机不该凭空多出一个命理产品库。"""
    global _DISABLED
    with _CFG_LOCK:
        _DISABLED = True


def get_fatex_store() -> Optional[FatexStore]:
    """运行期取单例；未 configure 时按默认路径懒建（失败返 None，绝不抛）；
    :func:`disable_fatex_store` 之后恒 None。"""
    global _STORE, _DB_PATH
    if _DISABLED:
        return None
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                _DB_PATH = _resolved_db_path(_DB_PATH)
                try:
                    _STORE = FatexStore(_DB_PATH)
                except Exception:
                    logger.debug("[fatex] 懒建库失败", exc_info=True)
                    return None
    return _STORE


def reset_fatex_store_for_tests() -> None:
    """单测隔离用：重置单例（生产勿用）。"""
    global _STORE, _DB_PATH, _DISABLED
    with _CFG_LOCK:
        _STORE = None
        _DB_PATH = DEFAULT_DB_PATH
        _DISABLED = False
