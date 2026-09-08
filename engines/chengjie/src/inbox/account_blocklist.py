# -*- coding: utf-8 -*-
"""账号级停联名单（P-2 E · #259 #252 · D-P4，2026-09-08）。

O-1 A 的冻结记在**会话**上（列表标签「客户要求停联」+ 档位 ``guard:stop_contact_from:<原档>``），
clean 重装 / 会话被删 / 换机即丢——H3BAJD：昨晚说「Never write me again」的 Sinue
（12134989840）今天重装登录后 3 秒又被起草。本模块把「这个账号不该再联系谁」落成
**账号级、只增不删**的名单：

- 表 ``account_blocklist(platform, account_id, peer, reason, hit_text, ts, unfrozen_ts, source)``，
  主键 (platform, account_id, peer)；``unfrozen_ts>0`` = 人工解冻过（**不删行**，历史可查；
  再次命中会重新置 0）。
- 库文件 ``account_blocklist.db`` 与 ``inbox.db`` 同目录（与 deferred_outbox.db 同手法，
  **零 store.py 改动**）；store 无 ``_db_path``（测试假件）→ 进程内 ``:memory:``。
- ``export_rows / import_rows``：随账号迁移包（``migration_export.build_migration_kit`` 的
  ``blocklist.jsonl`` 成员）导出导入；导入 = upsert，永不删既有行、不把已冻结改成解冻。
- ``seed_from_store``：存量迁移——把 conversation_meta 里已带「客户要求停联」标签的会话
  补进名单（首次取用时跑一次，幂等；CLI 见 ``account_scope_migration --blocklist-db``）。

接线：``stop_contact.freeze_conversation``（reason=stop_contact → ``add``）、
``unfreeze_conversation``（→ ``mark_unfrozen``）、登录同步完成后的历史扫描
（``stop_contact.login_scan_backfill`` 命中 → 冻结 + 进名单；名单在场且未解冻的 peer →
重新冻结）。永不 opener / 关怀 / 目标：``is_blocked`` 供 ``proactive_peer_hygiene`` 等谓词点复用。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DB_NAME = "account_blocklist.db"
#: 迁移包成员名（build_migration_kit 写、import_from_kit 读）
KIT_MEMBER = "blocklist.jsonl"
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS account_blocklist (
    platform     TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    peer         TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT 'stop_contact',
    hit_text     TEXT NOT NULL DEFAULT '',
    ts           REAL NOT NULL DEFAULT 0,
    unfrozen_ts  REAL NOT NULL DEFAULT 0,
    unfrozen_by  TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    hits         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (platform, account_id, peer)
);
CREATE INDEX IF NOT EXISTS idx_ab_account ON account_blocklist(platform, account_id);
"""


def _norm(v: Any) -> str:
    return str(v or "").strip()


class AccountBlocklist:
    """账号级停联名单存储（线程安全、只增不删）。"""

    def __init__(self, db_path: Optional[Path]) -> None:
        self._path = Path(db_path) if db_path else None
        self._lock = threading.RLock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()
        self._seeded = False

    @property
    def path(self) -> Optional[Path]:
        return self._path

    # ── 写 ────────────────────────────────────────────────────────────────
    def add(self, platform: str, account_id: str, peer: str, *,
            reason: str = "stop_contact", hit_text: str = "", ts: Optional[float] = None,
            source: str = "") -> bool:
        """加入 / 刷新一行（命中即重新冻结：unfrozen_ts 归 0）。返回是否**新增**。"""
        plat, acct, pr = _norm(platform).lower(), _norm(account_id), _norm(peer)
        if not plat or not acct or not pr:
            return False
        now = float(ts if ts is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT hits FROM account_blocklist WHERE platform=? AND account_id=? AND peer=?",
                (plat, acct, pr)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO account_blocklist(platform, account_id, peer, reason, hit_text,"
                    " ts, unfrozen_ts, unfrozen_by, source, hits) VALUES (?,?,?,?,?,?,0,'',?,1)",
                    (plat, acct, pr, _norm(reason) or "stop_contact", _norm(hit_text)[:200],
                     now, _norm(source)[:40]))
                self._conn.commit()
                return True
            self._conn.execute(
                "UPDATE account_blocklist SET reason=?, hit_text=CASE WHEN ?!='' THEN ? ELSE hit_text END,"
                " ts=MAX(ts, ?), unfrozen_ts=0, unfrozen_by='', hits=hits+1,"
                " source=CASE WHEN ?!='' THEN ? ELSE source END"
                " WHERE platform=? AND account_id=? AND peer=?",
                (_norm(reason) or "stop_contact", _norm(hit_text)[:200], _norm(hit_text)[:200],
                 now, _norm(source)[:40], _norm(source)[:40], plat, acct, pr))
            self._conn.commit()
            return False

    def mark_unfrozen(self, platform: str, account_id: str, peer: str, *,
                      by: str = "human", ts: Optional[float] = None) -> bool:
        """人工解冻：写 unfrozen_ts（**不删行**）。返回是否有行被改。"""
        plat, acct, pr = _norm(platform).lower(), _norm(account_id), _norm(peer)
        if not plat or not acct or not pr:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE account_blocklist SET unfrozen_ts=?, unfrozen_by=? "
                "WHERE platform=? AND account_id=? AND peer=? AND unfrozen_ts=0",
                (float(ts if ts is not None else time.time()), _norm(by)[:40], plat, acct, pr))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    # ── 读 ────────────────────────────────────────────────────────────────
    def get(self, platform: str, account_id: str, peer: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_blocklist WHERE platform=? AND account_id=? AND peer=?",
                (_norm(platform).lower(), _norm(account_id), _norm(peer))).fetchone()
        return dict(row) if row is not None else None

    def is_blocked(self, platform: str, account_id: str, peer: str) -> bool:
        """名单在场且**未解冻** → True。绝不抛。"""
        try:
            row = self.get(platform, account_id, peer)
        except Exception:
            return False
        return bool(row) and float(row.get("unfrozen_ts") or 0) <= 0

    def list_for_account(self, platform: str, account_id: str, *,
                         include_unfrozen: bool = True, limit: int = 5000) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM account_blocklist WHERE platform=? AND account_id=?"
        if not include_unfrozen:
            sql += " AND unfrozen_ts=0"
        sql += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(
                sql, (_norm(platform).lower(), _norm(account_id), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def blocked_peers(self, platform: str, account_id: str) -> List[str]:
        return [str(r["peer"]) for r in self.list_for_account(
            platform, account_id, include_unfrozen=False)]

    def count(self, platform: str = "", account_id: str = "") -> Dict[str, int]:
        sql = "SELECT COUNT(*) AS n, SUM(CASE WHEN unfrozen_ts=0 THEN 1 ELSE 0 END) AS active" \
              " FROM account_blocklist"
        args: List[Any] = []
        if platform:
            sql += " WHERE platform=?"
            args.append(_norm(platform).lower())
            if account_id:
                sql += " AND account_id=?"
                args.append(_norm(account_id))
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return {"total": int(row["n"] or 0), "active": int(row["active"] or 0)}

    # ── 导出 / 导入（随账号迁移包） ───────────────────────────────────────
    def export_rows(self, platform: str, account_id: str) -> List[Dict[str, Any]]:
        out = []
        for r in self.list_for_account(platform, account_id):
            out.append({
                "type": "blocklist", "schema_version": SCHEMA_VERSION,
                "platform": r["platform"], "account_id": r["account_id"], "peer": r["peer"],
                "reason": r["reason"], "hit_text": r["hit_text"], "ts": float(r["ts"] or 0),
                "unfrozen_ts": float(r["unfrozen_ts"] or 0), "unfrozen_by": r["unfrozen_by"],
                "source": r["source"], "hits": int(r["hits"] or 1),
            })
        return out

    def import_rows(self, rows: Iterable[Dict[str, Any]], *,
                    platform: str = "", account_id: str = "") -> Dict[str, int]:
        """导入（upsert，只增不删）。可用 platform/account_id 把行**重定向**到新账号
        （封号换新号迁移场景）。既有行已冻结、导入行是解冻 → 保持冻结（宁多冻不漏）。"""
        stats = {"read": 0, "inserted": 0, "updated": 0, "skipped": 0}
        for r in rows or []:
            stats["read"] += 1
            if not isinstance(r, dict) or str(r.get("type") or "blocklist") != "blocklist":
                stats["skipped"] += 1
                continue
            plat = _norm(platform or r.get("platform")).lower()
            acct = _norm(account_id or r.get("account_id"))
            pr = _norm(r.get("peer"))
            if not plat or not acct or not pr:
                stats["skipped"] += 1
                continue
            unfrozen = float(r.get("unfrozen_ts") or 0)
            with self._lock:
                cur = self._conn.execute(
                    "SELECT unfrozen_ts FROM account_blocklist WHERE platform=? AND account_id=? AND peer=?",
                    (plat, acct, pr)).fetchone()
                if cur is None:
                    self._conn.execute(
                        "INSERT INTO account_blocklist(platform, account_id, peer, reason, hit_text,"
                        " ts, unfrozen_ts, unfrozen_by, source, hits) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (plat, acct, pr, _norm(r.get("reason")) or "stop_contact",
                         _norm(r.get("hit_text"))[:200], float(r.get("ts") or 0), unfrozen,
                         _norm(r.get("unfrozen_by"))[:40],
                         (_norm(r.get("source")) or "import")[:40], int(r.get("hits") or 1)))
                    stats["inserted"] += 1
                else:
                    keep_frozen = float(cur["unfrozen_ts"] or 0) <= 0
                    self._conn.execute(
                        "UPDATE account_blocklist SET ts=MAX(ts, ?), hits=MAX(hits, ?),"
                        " unfrozen_ts=CASE WHEN ? THEN 0 ELSE ? END"
                        " WHERE platform=? AND account_id=? AND peer=?",
                        (float(r.get("ts") or 0), int(r.get("hits") or 1),
                         1 if keep_frozen else 0, unfrozen, plat, acct, pr))
                    stats["updated"] += 1
                self._conn.commit()
        return stats

    def import_from_kit(self, zip_path: Any, *, platform: str = "",
                        account_id: str = "") -> Dict[str, int]:
        """从迁移包 zip 读 ``blocklist.jsonl`` 导入；成员缺失 → 全零统计（老包无此成员）。"""
        import zipfile
        rows: List[Dict[str, Any]] = []
        try:
            with zipfile.ZipFile(str(zip_path)) as zf:
                if KIT_MEMBER not in zf.namelist():
                    return {"read": 0, "inserted": 0, "updated": 0, "skipped": 0, "member": 0}
                for line in zf.read(KIT_MEMBER).decode("utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
        except Exception:
            logger.debug("[blocklist] 迁移包读取失败", exc_info=True)
            return {"read": 0, "inserted": 0, "updated": 0, "skipped": 0, "member": 0}
        out = self.import_rows(rows, platform=platform, account_id=account_id)
        out["member"] = 1
        return out

    # ── 存量迁移：会话标签 → 名单 ─────────────────────────────────────────
    def seed_from_store(self, store: Any, *, limit: int = 300) -> int:
        """把已带「客户要求停联」标签的会话补进名单（幂等、只增）。返回新增行数。"""
        if store is None or not hasattr(store, "list_tagged_conversations"):
            return 0
        try:
            from src.inbox.stop_contact import STOP_CONTACT_TAG
            rows = store.list_tagged_conversations(STOP_CONTACT_TAG, limit=limit) or []
        except Exception:
            return 0
        added = 0
        for r in rows:
            try:
                plat = str(r.get("platform") or "")
                acct = str(r.get("account_id") or "default")
                peer = str(r.get("chat_key") or "")
                if self.get(plat, acct, peer) is None:
                    if self.add(plat, acct, peer, reason="stop_contact",
                                ts=float(r.get("last_ts") or 0) or None, source="seed:conv_tag"):
                        added += 1
            except Exception:
                continue
        if added:
            logger.info("[stop-contact] blocklist seeded_from_conv_tags added=%d", added)
        return added

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── 单例（按库路径） ─────────────────────────────────────────────────────────
_INSTANCES: Dict[str, AccountBlocklist] = {}
_INST_LOCK = threading.Lock()


def blocklist_db_path(store: Any) -> Optional[Path]:
    p = getattr(store, "_db_path", None)
    if p is None:
        return None
    try:
        return Path(str(p)).parent / DB_NAME
    except Exception:
        return None


def get_blocklist(store: Any = None, *, db_path: Any = None) -> AccountBlocklist:
    """取账号级名单（同一 inbox.db 目录一份；无路径 → 进程内 :memory: 一份）。首次取用
    自动 ``seed_from_store``（存量「客户要求停联」会话进名单）。绝不抛。"""
    path = Path(str(db_path)) if db_path else blocklist_db_path(store)
    key = str(path) if path else ":memory:"
    with _INST_LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            try:
                inst = AccountBlocklist(path)
            except Exception:
                logger.debug("[blocklist] 打开 %s 失败，回落内存", key, exc_info=True)
                inst = AccountBlocklist(None)
            _INSTANCES[key] = inst
    if store is not None and not inst._seeded:
        inst._seeded = True
        try:
            inst.seed_from_store(store)
        except Exception:
            logger.debug("[blocklist] 存量迁移失败（忽略）", exc_info=True)
    return inst


def reset_for_tests() -> None:
    with _INST_LOCK:
        for inst in _INSTANCES.values():
            inst.close()
        _INSTANCES.clear()


def is_peer_blocked(store: Any, platform: str, account_id: str, peer: str) -> bool:
    """谓词：该账号是否已被这位客户要求停联（未解冻）。供 opener / 关怀 / 目标过滤复用。"""
    try:
        return get_blocklist(store).is_blocked(platform, account_id, peer)
    except Exception:
        return False


__all__ = [
    "DB_NAME", "KIT_MEMBER", "SCHEMA_VERSION", "AccountBlocklist",
    "get_blocklist", "blocklist_db_path", "is_peer_blocked", "reset_for_tests",
]
