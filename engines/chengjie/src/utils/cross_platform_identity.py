"""
S5: CrossPlatformIdentity — maps (platform, platform_uid) → canonical_id.

Table user_identity_map lives in bot.db alongside episodic_memory.
Default canonical_id = "<platform>:<uid>" (no external deps, stable).
Manual linking via link() merges two platform IDs into one shared memory key.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("CrossPlatformIdentity")

_DDL = """
CREATE TABLE IF NOT EXISTS user_identity_map (
    platform     TEXT NOT NULL,
    platform_uid TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (platform, platform_uid)
);
CREATE INDEX IF NOT EXISTS idx_uim_canonical ON user_identity_map(canonical_id);
"""


class CrossPlatformIdentity:
    """Thread-safe (check_same_thread=False) SQLite-backed identity map."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()
        logger.info("CrossPlatformIdentity ready: %s", self._db_path)

    def resolve(self, platform: str, uid: str) -> str:
        """Return canonical_id for (platform, uid). Auto-inserts if unknown."""
        if not platform or not uid:
            return uid or ""
        row = self._conn.execute(
            "SELECT canonical_id FROM user_identity_map WHERE platform=? AND platform_uid=?",
            (platform, uid),
        ).fetchone()
        if row:
            return row[0]
        canonical_id = f"{platform}:{uid}"
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_identity_map"
                " (platform, platform_uid, canonical_id, created_at) VALUES (?,?,?,?)",
                (platform, uid, canonical_id, time.time()),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("resolve insert failed: %s", e)
        return canonical_id

    def link(self, platform_a: str, uid_a: str, platform_b: str, uid_b: str) -> str:
        """Merge platform_b:uid_b into same canonical as platform_a:uid_a.
        Returns the shared canonical_id."""
        canon = self.resolve(platform_a, uid_a)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO user_identity_map"
                " (platform, platform_uid, canonical_id, created_at) VALUES (?,?,?,?)",
                (platform_b, uid_b, canon, time.time()),
            )
            self._conn.commit()
            logger.info("identity linked %s:%s <-> %s:%s → %s", platform_a, uid_a, platform_b, uid_b, canon)
        except Exception as e:
            logger.warning("link failed: %s", e)
        return canon

    def unlink(self, platform: str, uid: str) -> str:
        """Detach (platform, uid) back to its own canonical_id."""
        new_canon = f"{platform}:{uid}"
        try:
            self._conn.execute(
                "UPDATE user_identity_map SET canonical_id=? WHERE platform=? AND platform_uid=?",
                (new_canon, platform, uid),
            )
            self._conn.commit()
            logger.info("identity unlinked %s:%s → %s", platform, uid, new_canon)
        except Exception as e:
            logger.warning("unlink failed: %s", e)
        return new_canon

    def list_all(self, limit: int = 200) -> List[Tuple[str, str, str, float]]:
        """Return [(platform, platform_uid, canonical_id, created_at), ...]."""
        rows = self._conn.execute(
            "SELECT platform, platform_uid, canonical_id, created_at"
            " FROM user_identity_map ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def get_by_canonical(self, canonical_id: str) -> List[Tuple[str, str]]:
        """Return all (platform, platform_uid) sharing a canonical_id."""
        rows = self._conn.execute(
            "SELECT platform, platform_uid FROM user_identity_map WHERE canonical_id=?",
            (canonical_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def link_and_merge_memory(
    cpi: "CrossPlatformIdentity",
    episodic_store,
    platform_a: str,
    uid_a: str,
    platform_b: str,
    uid_b: str,
) -> dict:
    """link + 把 B 侧旧 canonical 的情景记忆行迁入共享 canonical，
    并把 B 既有 cluster 成员整簇改挂（传递性）。

    修两件事：① 「关联后失忆」——``link()`` 只改 map 指向，B 侧历史
    episodic 行仍挂旧 canonical 变孤儿，这里同动作经
    :meth:`EpisodicMemoryStore.merge_key`（幂等、content_hash 去重）搬家；
    ② 「簇分裂」——B 若已属 cluster（先前手动链过第三平台），其余成员
    map 行仍指旧 canonical，历史被搬走后未来写入会重新孤儿化，故整簇
    一并改挂 A 的 canonical。

    ``episodic_store=None`` → 只做 link+整簇改挂。绝不抛：merge/relink
    失败只损失合并，不影响关联本身。
    """
    pre_a = cpi.resolve(platform_a, uid_a)
    pre_b = cpi.resolve(platform_b, uid_b)
    canon = cpi.link(platform_a, uid_a, platform_b, uid_b)
    relinked: List[dict] = []
    merged = 0
    if pre_b and pre_b != canon:
        # B 的 map 行已改挂 canon → 此刻仍指 pre_b 的就是簇友，整簇跟走
        try:
            members = list(cpi.get_by_canonical(pre_b) or [])
        except Exception:
            members = []
        for p, u in members:
            try:
                cpi.link(platform_a, uid_a, p, u)
                relinked.append({"platform": p, "uid": u})
            except Exception:
                logger.warning(
                    "cluster relink failed %s:%s", p, u, exc_info=True)
        if episodic_store is not None:
            try:
                merged = int(episodic_store.merge_key(pre_b, canon) or 0)
            except Exception:
                logger.warning(
                    "episodic merge_key %s→%s failed",
                    pre_b, canon, exc_info=True)
    return {
        "canonical_id": canon,
        "memory_rows_merged": merged,
        "merged_from": [pre_b] if merged else [],
        "cluster_relinked": relinked,
        # 幂等信号：两侧 link 前已同 canonical → 重复关联（调用方据此
        # 跳过合流观测计数，防 totals 虚胀；与 confirm_link_pair 同口径）
        "already_linked": bool(pre_a and pre_a == pre_b),
    }
