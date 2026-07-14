"""
Leads Store — SQLite-backed lightweight CRM for multi-platform customer acquisition.

Schema:
  leads              — customer profiles with scoring and status tracking
  platform_profiles  — per-platform identity (same person, multiple platforms)
  interactions       — full interaction log (messages, follows, likes, etc.)

Key features:
  - Cross-platform dedup by email, phone, or normalized name
  - Automatic lead scoring based on interaction history
  - Status pipeline: new → contacted → responded → qualified → converted
  - Thread-safe with WAL mode for concurrent reads
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.host.device_registry import data_file

log = logging.getLogger(__name__)

_DEFAULT_DB = data_file("leads.db")

LEAD_STATUSES = ("new", "contacted", "responded", "qualified", "converted", "blacklisted")


# ---------------------------------------------------------------------------
# Name normalization for dedup
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, strip accents, collapse whitespace."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    for suffix in (" jr", " sr", " ii", " iii", " iv"):
        name = name.removesuffix(suffix)
    return name.strip()


def normalize_phone(phone: str) -> str:
    """Strip all non-digits for phone comparison."""
    return re.sub(r"\D", "", phone) if phone else ""


def normalize_email(email: str) -> str:
    return email.strip().lower() if email else ""


# ---------------------------------------------------------------------------
# Leads Store
# ---------------------------------------------------------------------------

class LeadsStore:
    """
    Thread-safe SQLite CRM.

    Usage:
        store = LeadsStore()
        lead_id = store.add_lead(name="John Smith", email="john@example.com",
                                 source_platform="linkedin")
        store.add_platform_profile(lead_id, "linkedin",
                                   profile_url="https://linkedin.com/in/john")
        store.add_interaction(lead_id, "linkedin", "send_message",
                              direction="outbound", content="Hi John!")
        store.update_score(lead_id)
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = str(db_path or _DEFAULT_DB)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL 模式已在 _init_db 中持久化设置，此处只需启用外键（每次连接必须设置）
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")  # P9-A: 性能优化（比FULL快3倍，安全性与WAL兼容）
        return conn

    def _migrate_schema(self, conn: sqlite3.Connection):
        """Add columns on existing DBs (SQLite lightweight migration)."""
        def _cols(table: str) -> set:
            cur = conn.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cur.fetchall()}

        lead_cols = _cols("leads")
        for col, decl in (
            ("conversion_value", "REAL DEFAULT NULL"),
            ("conversion_currency", "TEXT DEFAULT ''"),
            ("converted_at", "TEXT DEFAULT ''"),
            ("conversion_external_ref", "TEXT DEFAULT ''"),
        ):
            if col not in lead_cols:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")

        interaction_cols = _cols("interactions")
        for col, decl in (
            ("device_id", "TEXT DEFAULT ''"),
        ):
            if col not in interaction_cols:
                conn.execute(f"ALTER TABLE interactions ADD COLUMN {col} {decl}")

    def _init_db(self):
        conn = self._conn()
        # 持久化设置 WAL 模式（数据库级别，重启后保留）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                normalized_name TEXT,
                email           TEXT,
                normalized_email TEXT,
                phone           TEXT,
                normalized_phone TEXT,
                company         TEXT DEFAULT '',
                title           TEXT DEFAULT '',
                industry        TEXT DEFAULT '',
                location        TEXT DEFAULT '',
                source_platform TEXT DEFAULT '',
                tags            TEXT DEFAULT '[]',
                status          TEXT DEFAULT 'new',
                score           REAL DEFAULT 0.0,
                notes           TEXT DEFAULT '',
                created_at      TEXT,
                updated_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS platform_profiles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                platform    TEXT NOT NULL,
                profile_id  TEXT DEFAULT '',
                profile_url TEXT DEFAULT '',
                username    TEXT DEFAULT '',
                bio         TEXT DEFAULT '',
                followers   INTEGER DEFAULT 0,
                following   INTEGER DEFAULT 0,
                verified    INTEGER DEFAULT 0,
                last_checked TEXT,
                UNIQUE(lead_id, platform)
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                platform    TEXT NOT NULL,
                action      TEXT NOT NULL,
                direction   TEXT DEFAULT 'outbound',
                content     TEXT DEFAULT '',
                status      TEXT DEFAULT 'sent',
                metadata    TEXT DEFAULT '{}',
                created_at  TEXT,
                device_id   TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(normalized_email);
            CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(normalized_phone);
            CREATE INDEX IF NOT EXISTS idx_leads_name ON leads(normalized_name);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
            CREATE INDEX IF NOT EXISTS idx_profiles_lead ON platform_profiles(lead_id);
            CREATE INDEX IF NOT EXISTS idx_profiles_platform ON platform_profiles(platform, profile_id);
            CREATE INDEX IF NOT EXISTS idx_profiles_username ON platform_profiles(platform, username);
            CREATE INDEX IF NOT EXISTS idx_interactions_lead ON interactions(lead_id);
            CREATE INDEX IF NOT EXISTS idx_interactions_time ON interactions(created_at DESC);
        """)
        self._migrate_schema(conn)
        conn.commit()
        conn.close()

    # ── Lead CRUD ──────────────────────────────────────────────────────────

    def add_lead(self, name: str, email: str = "", phone: str = "",
                 company: str = "", title: str = "", industry: str = "",
                 location: str = "", source_platform: str = "",
                 tags: Optional[List[str]] = None,
                 notes: str = "",
                 dedup: bool = True) -> int:
        """
        Add a lead. If dedup=True, checks for existing match first.
        Returns lead_id (existing if matched, new if created).
        """
        now = datetime.now(timezone.utc).isoformat()
        n_name = normalize_name(name)
        n_email = normalize_email(email)
        n_phone = normalize_phone(phone)

        if dedup:
            existing = self.find_match(email=email, phone=phone, name=name)
            if existing:
                self._merge_into(existing, name=name, email=email, phone=phone,
                                 company=company, title=title, industry=industry,
                                 location=location, tags=tags, notes=notes)
                return existing

        with self._lock:
            conn = self._conn()
            cur = conn.execute("""
                INSERT INTO leads (name, normalized_name, email, normalized_email,
                                   phone, normalized_phone, company, title, industry,
                                   location, source_platform, tags, notes,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, n_name, email, n_email, phone, n_phone,
                  company, title, industry, location, source_platform,
                  json.dumps(tags or []), notes, now, now))
            conn.commit()
            lead_id = cur.lastrowid
            conn.close()
        log.info("New lead #%d: %s (%s)", lead_id, name, source_platform)
        return lead_id

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return self._lead_to_dict(row)

    def update_lead(self, lead_id: int, **fields) -> bool:
        allowed = {"name", "email", "phone", "company", "title", "industry",
                    "location", "status", "score", "notes", "tags",
                    "conversion_value", "conversion_currency", "converted_at",
                    "conversion_external_ref"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        if "name" in updates:
            updates["normalized_name"] = normalize_name(updates["name"])
        if "email" in updates:
            updates["normalized_email"] = normalize_email(updates["email"])
        if "phone" in updates:
            updates["normalized_phone"] = normalize_phone(updates["phone"])
        if "tags" in updates and isinstance(updates["tags"], list):
            updates["tags"] = json.dumps(updates["tags"])

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [lead_id]

        with self._lock:
            conn = self._conn()
            conn.execute(f"UPDATE leads SET {set_clause} WHERE id = ?", values)
            conn.commit()
            conn.close()
        return True

    def mark_conversion(
            self, lead_id: int,
            value: Optional[float] = None,
            currency: str = "USD",
            external_ref: str = "",
            append_note: str = "") -> bool:
        """
        Set lead to converted and optionally record revenue (manual attribution).
        Without value, still marks CRM stage for funnel close-rate.
        Idempotent: if already converted with a value, only appends notes —
        never overwrites an existing conversion_value to prevent duplicate
        auto-REFERRAL triggers from inflating or zeroing revenue.
        """
        lead = self.get_lead(lead_id)
        if not lead:
            return False
        already_converted = lead.get("status") == "converted"
        existing_value = lead.get("conversion_value")
        now = datetime.now(timezone.utc).isoformat()
        note = lead.get("notes") or ""
        if append_note:
            note = (note + "\n" if note else "") + f"[conversion {now}] {append_note}"
        updates: Dict[str, Any] = {"notes": note.strip()}
        if not already_converted:
            updates["status"] = "converted"
            updates["converted_at"] = now
            updates["conversion_currency"] = currency or "USD"
            if value is not None:
                updates["conversion_value"] = float(value)
            if external_ref:
                updates["conversion_external_ref"] = external_ref
        else:
            # 已成交：仅在无金额时写入新金额，避免覆盖人工录入的真实值
            if existing_value is None and value is not None:
                updates["conversion_value"] = float(value)
                updates["conversion_currency"] = currency or "USD"
            if external_ref and not lead.get("conversion_external_ref"):
                updates["conversion_external_ref"] = external_ref
        return self.update_lead(lead_id, **updates)

    def has_dm_sent(self, username: str, platform: str = "tiktok") -> bool:
        """跨设备去重：检查该平台用户名是否已收过外发 DM/auto_reply。"""
        # P7-A: 规范化用户名，兼容 "@mario" 和 "mario" 两种格式
        username = username.lstrip("@").strip() if username else username
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM interactions i "
            "JOIN platform_profiles p ON p.lead_id = i.lead_id "
            "WHERE p.platform = ? AND LOWER(p.username) = LOWER(?) "
            "AND i.platform = ? AND i.action IN ('send_dm','auto_reply','follow_up') "
            "AND i.direction = 'outbound' LIMIT 1",
            (platform, username, platform),
        ).fetchone()
        conn.close()
        return row is not None

    def get_revenue_stats(self) -> Dict[str, Any]:
        """汇总全局成交金额、数量及今日新增。"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._conn()
        total_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(conversion_value),0) "
            "FROM leads WHERE status='converted'"
        ).fetchone()
        today_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(conversion_value),0) "
            "FROM leads WHERE status='converted' AND converted_at >= ?",
            (today,),
        ).fetchone()
        conn.close()
        return {
            "total_conversions": total_row[0] if total_row else 0,
            "total_revenue": round(total_row[1], 2) if total_row else 0.0,
            "today_conversions": today_row[0] if today_row else 0,
            "today_revenue": round(today_row[1], 2) if today_row else 0.0,
        }

    def delete_lead(self, lead_id: int) -> bool:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            conn.commit()
            affected = conn.total_changes
            conn.close()
        return affected > 0

    def cleanup_position_key_leads(self) -> int:
        """P8-B: 删除 name 以 'newfollower_' 开头的垃圾 lead（位置键记录）。
        同时级联删除对应的 platform_profiles 和 interactions（依赖 ON DELETE CASCADE）。
        返回删除的 lead 数量。
        """
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT id FROM leads WHERE name LIKE 'newfollower_%'"
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", ids)
                conn.commit()
            conn.close()
        log.info("[cleanup] 删除位置键垃圾 leads: %d 条", len(ids))
        return len(ids)

    def list_leads(self, status: Optional[str] = None,
                   platform: Optional[str] = None,
                   min_score: Optional[float] = None,
                   search: Optional[str] = None,
                   order_by: str = "score DESC",
                   limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        conditions = []
        params: list = []

        if status:
            conditions.append("l.status = ?")
            params.append(status)
        if min_score is not None:
            conditions.append("l.score >= ?")
            params.append(min_score)
        if search:
            conditions.append("(l.normalized_name LIKE ? OR l.email LIKE ? OR l.company LIKE ?)")
            s = f"%{search.lower()}%"
            params.extend([s, s, s])
        if platform:
            conditions.append("l.id IN (SELECT lead_id FROM platform_profiles WHERE platform = ?)")
            params.append(platform)

        where = " AND ".join(conditions) if conditions else "1=1"
        safe_order = order_by if order_by in (
            "score DESC", "score ASC", "created_at DESC", "created_at ASC",
            "updated_at DESC", "name ASC",
        ) else "score DESC"

        conn = self._conn()
        rows = conn.execute(f"""
            SELECT l.* FROM leads l
            WHERE {where}
            ORDER BY {safe_order}
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
        conn.close()
        return [self._lead_to_dict(r) for r in rows]

    def count_leads(self, status: Optional[str] = None) -> int:
        conn = self._conn()
        if status:
            row = conn.execute("SELECT COUNT(*) FROM leads WHERE status = ?", (status,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM leads").fetchone()
        conn.close()
        return row[0] if row else 0

    # ── Platform Profiles ──────────────────────────────────────────────────

    def add_platform_profile(self, lead_id: int, platform: str,
                             profile_id: str = "", profile_url: str = "",
                             username: str = "", bio: str = "",
                             followers: int = 0, following: int = 0,
                             verified: bool = False) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            cur = conn.execute("""
                INSERT OR REPLACE INTO platform_profiles
                    (lead_id, platform, profile_id, profile_url, username,
                     bio, followers, following, verified, last_checked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (lead_id, platform, profile_id, profile_url, username,
                  bio, followers, following, int(verified), now))
            conn.commit()
            pid = cur.lastrowid
            conn.close()
        return pid

    def get_platform_profiles(self, lead_id: int) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM platform_profiles WHERE lead_id = ?", (lead_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def find_by_profile(self, platform: str, profile_id: str) -> Optional[int]:
        """Find lead_id by platform-specific profile ID."""
        conn = self._conn()
        row = conn.execute(
            "SELECT lead_id FROM platform_profiles WHERE platform = ? AND profile_id = ?",
            (platform, profile_id),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def find_by_platform_username(self, platform: str, username: str) -> Optional[int]:
        # P7-A: 规范化用户名，兼容 "@mario" 和 "mario" 两种格式
        username = username.lstrip("@").strip() if username else username
        """Find lead_id by platform + username (case-insensitive)."""
        if not username:
            return None
        conn = self._conn()
        row = conn.execute(
            "SELECT lead_id FROM platform_profiles "
            "WHERE platform = ? AND LOWER(username) = LOWER(?)",
            (platform, username),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def is_followed_on_platform(self, platform: str, username: str) -> bool:
        """Check if a user on a given platform has been followed (has outbound follow interaction)."""
        lead_id = self.find_by_platform_username(platform, username)
        if not lead_id:
            return False
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM interactions "
            "WHERE lead_id = ? AND platform = ? AND action = 'follow' AND direction = 'outbound' "
            "LIMIT 1",
            (lead_id, platform),
        ).fetchone()
        conn.close()
        return row is not None

    def get_followed_count_by_device(self, platform: str, device_id: str) -> int:
        """Count how many users a specific device has followed on a platform."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(DISTINCT i.lead_id) FROM interactions i "
            "WHERE i.platform = ? AND i.action = 'follow' AND i.direction = 'outbound' "
            "AND json_extract(i.metadata, '$.device_id') = ?",
            (platform, device_id),
        ).fetchone()
        conn.close()
        return row[0] if row else 0

    def get_follow_stats(self, platform: str) -> Dict[str, Any]:
        """Get follow/followback/DM stats for a platform."""
        conn = self._conn()
        total_followed = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM interactions "
            "WHERE platform = ? AND action = 'follow' AND direction = 'outbound'",
            (platform,),
        ).fetchone()[0]
        total_followbacks = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM interactions "
            "WHERE platform = ? AND action = 'follow_back' AND direction = 'inbound'",
            (platform,),
        ).fetchone()[0]
        total_dms = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM interactions "
            "WHERE platform = ? AND action IN ('send_dm', 'auto_reply', 'follow_up') AND direction = 'outbound'",
            (platform,),
        ).fetchone()[0]
        conn.close()
        return {
            "total_followed": total_followed,
            "total_follow_backs": total_followbacks,
            "follow_back_rate": total_followbacks / max(total_followed, 1),
            "total_dms": total_dms,
        }

    # ── Interactions ───────────────────────────────────────────────────────

    def add_interaction(self, lead_id: int, platform: str, action: str,
                        direction: str = "outbound", content: str = "",
                        status: str = "sent",
                        metadata: Optional[Dict[str, Any]] = None,
                        device_id: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            # 去重：120秒内相同 lead+direction+content 不重复插入
            if content and direction in ("inbound", "outbound"):
                dup = conn.execute("""
                    SELECT id FROM interactions
                    WHERE lead_id=? AND direction=? AND content=?
                      AND created_at > datetime('now','-120 seconds')
                    LIMIT 1
                """, (lead_id, direction, content[:500])).fetchone()
                if dup:
                    conn.close()
                    return dup[0]
            cur = conn.execute("""
                INSERT INTO interactions (lead_id, platform, action, direction,
                                          content, status, metadata, created_at, device_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (lead_id, platform, action, direction, content[:500] if content else "", status,
                  json.dumps(metadata or {}), now, device_id or ""))
            conn.commit()
            iid = cur.lastrowid
            conn.close()
        # 每次收到入站信号时自动推进 lead 状态（responded → qualified）
        if direction == "inbound" and action in ("dm_received", "follow_back"):
            self._maybe_advance_lead_status(lead_id, platform)
        return iid

    def _maybe_advance_lead_status(self, lead_id: int, platform: str) -> None:
        """根据历史互动自动推进 lead 状态，只向前进，不回退。

        规则：
          dm_received(inbound)                       → responded
          follow_back(inbound) + dm_received(inbound) → qualified（双向互动=真实兴趣）
        """
        _order = {s: i for i, s in enumerate(LEAD_STATUSES)}
        lead = self.get_lead(lead_id)
        if not lead or lead.get("status") in ("converted", "blacklisted"):
            return
        current = lead.get("status", "new")
        current_ord = _order.get(current, 0)

        conn = self._conn()
        has_follow_back = conn.execute(
            "SELECT 1 FROM interactions WHERE lead_id=? AND platform=? "
            "AND action='follow_back' AND direction='inbound' LIMIT 1",
            (lead_id, platform),
        ).fetchone() is not None
        has_dm_received = conn.execute(
            "SELECT 1 FROM interactions WHERE lead_id=? AND platform=? "
            "AND action='dm_received' AND direction='inbound' LIMIT 1",
            (lead_id, platform),
        ).fetchone() is not None
        # NEEDS_REPLY intent = 对方表达了明确购买意向，直接升为 qualified
        has_needs_reply = conn.execute(
            "SELECT 1 FROM interactions WHERE lead_id=? AND platform=? "
            "AND direction='inbound' "
            "AND json_extract(metadata,'$.intent')='NEEDS_REPLY' LIMIT 1",
            (lead_id, platform),
        ).fetchone() is not None
        conn.close()

        target = current
        if has_dm_received and current_ord < _order["responded"]:
            target = "responded"
        # NEEDS_REPLY 意图 → 跳过 responded，直升 qualified
        if has_needs_reply and _order.get(target, 0) < _order["qualified"]:
            target = "qualified"
        # follow_back + dm_received = 双向互动，升为 qualified
        if has_follow_back and has_dm_received and _order.get(target, 0) < _order["qualified"]:
            target = "qualified"

        if target != current:
            self.update_lead(lead_id, status=target)
            self.update_score(lead_id)
            log.info("[LeadsStore] Auto-advanced %s lead %d: %s → %s",
                     platform, lead_id, current, target)
            # qualified 升级时推 SSE 事件，前端实时可见
            if target == "qualified":
                try:
                    from src.host.event_stream import push_event as _pev_q
                    _pev_q("tiktok.lead_qualified", {
                        "lead_id": lead_id,
                        "platform": platform,
                        "prev_status": current,
                    }, "")
                except Exception:
                    pass

    def get_interactions(self, lead_id: int, platform: Optional[str] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._conn()
        if platform:
            rows = conn.execute(
                "SELECT * FROM interactions WHERE lead_id = ? AND platform = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (lead_id, platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interactions WHERE lead_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def interaction_count(self, lead_id: int, direction: Optional[str] = None,
                          days: int = 30) -> int:
        conn = self._conn()
        cutoff = datetime.now(timezone.utc).isoformat()[:10]
        if direction:
            row = conn.execute(
                "SELECT COUNT(*) FROM interactions "
                "WHERE lead_id = ? AND direction = ? AND created_at >= ?",
                (lead_id, direction, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE lead_id = ? AND created_at >= ?",
                (lead_id, cutoff),
            ).fetchone()
        conn.close()
        return row[0] if row else 0

    # ── Dedup / Matching ───────────────────────────────────────────────────

    def find_match(self, email: str = "", phone: str = "",
                   name: str = "") -> Optional[int]:
        """
        Find an existing lead matching the given identity signals.
        Priority: email > phone > normalized name.
        """
        conn = self._conn()
        # email match (strongest)
        if email:
            n_email = normalize_email(email)
            row = conn.execute(
                "SELECT id FROM leads WHERE normalized_email = ? AND normalized_email != ''",
                (n_email,),
            ).fetchone()
            if row:
                conn.close()
                return row[0]

        # phone match
        if phone:
            n_phone = normalize_phone(phone)
            if len(n_phone) >= 7:
                row = conn.execute(
                    "SELECT id FROM leads WHERE normalized_phone = ? AND normalized_phone != ''",
                    (n_phone,),
                ).fetchone()
                if row:
                    conn.close()
                    return row[0]

        # name match (weakest — only exact normalized match)
        if name:
            n_name = normalize_name(name)
            if len(n_name) >= 4:
                row = conn.execute(
                    "SELECT id FROM leads WHERE normalized_name = ? AND normalized_name != ''",
                    (n_name,),
                ).fetchone()
                if row:
                    conn.close()
                    return row[0]

        conn.close()
        return None

    # ── Scoring ────────────────────────────────────────────────────────────

    def update_score(self, lead_id: int) -> float:
        """Recalculate and update lead score based on interactions and profile."""
        lead = self.get_lead(lead_id)
        if not lead:
            return 0.0

        score = 0.0

        # profile completeness
        if lead.get("email"):
            score += 5
        if lead.get("phone"):
            score += 5
        if lead.get("company"):
            score += 3
        if lead.get("title"):
            score += 2

        # platform presence
        profiles = self.get_platform_profiles(lead_id)
        score += len(profiles) * 5

        # interactions
        interactions = self.get_interactions(lead_id, limit=200)
        for ix in interactions:
            if ix["direction"] == "inbound":
                if ix["action"] in ("message_received", "reply"):
                    score += 10
                else:
                    score += 5
            else:
                if ix["action"] == "send_message":
                    score += 1
                elif ix["action"] in ("follow", "like", "comment"):
                    score += 0.5

        # recency bonus
        if interactions:
            latest = interactions[0]["created_at"]
            try:
                dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - dt).days
                if age_days <= 3:
                    score *= 1.5
                elif age_days <= 7:
                    score *= 1.2
                elif age_days > 30:
                    score *= 0.7
            except (ValueError, TypeError):
                pass

        score = round(score, 1)
        self.update_lead(lead_id, score=score)
        return score

    def bulk_update_scores(self) -> int:
        """Recalculate scores for all active leads."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id FROM leads WHERE status NOT IN ('blacklisted', 'converted')"
        ).fetchall()
        conn.close()
        for row in rows:
            self.update_score(row[0])
        return len(rows)

    # ── Stats ──────────────────────────────────────────────────────────────

    def pipeline_stats(self) -> Dict[str, Any]:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        by_status = {}
        for row in conn.execute("SELECT status, COUNT(*) as cnt FROM leads GROUP BY status"):
            by_status[row[0]] = row[1]
        by_platform = {}
        for row in conn.execute(
            "SELECT platform, COUNT(*) as cnt FROM platform_profiles GROUP BY platform"
        ):
            by_platform[row[0]] = row[1]
        avg_score = conn.execute("SELECT AVG(score) FROM leads").fetchone()[0] or 0.0
        ix_total = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        conn.close()
        return {
            "total_leads": total,
            "by_status": by_status,
            "by_platform": by_platform,
            "avg_score": round(avg_score, 1),
            "total_interactions": ix_total,
        }

    # ── Internal ───────────────────────────────────────────────────────────

    def _merge_into(self, lead_id: int, **fields):
        """Merge new data into existing lead (fill blanks, don't overwrite)."""
        lead = self.get_lead(lead_id)
        if not lead:
            return
        updates = {}
        for key in ("email", "phone", "company", "title", "industry", "location"):
            if fields.get(key) and not lead.get(key):
                updates[key] = fields[key]
        if fields.get("tags"):
            existing = json.loads(lead.get("tags", "[]")) if isinstance(lead.get("tags"), str) else lead.get("tags", [])
            new_tags = list(set(existing + (fields["tags"] if isinstance(fields["tags"], list) else [])))
            updates["tags"] = new_tags
        if fields.get("notes") and not lead.get("notes"):
            updates["notes"] = fields["notes"]
        if updates:
            self.update_lead(lead_id, **updates)

    def get_conversion_funnel(self, platform: str = "tiktok",
                              days: int = 30) -> Dict[str, Any]:
        """
        Build a full conversion funnel from interaction data.

        Stages: discovered → followed → follow_back → chatted → replied → converted
        """
        conn = self._conn()
        try:
            cutoff = ""
            if days > 0:
                from datetime import timedelta
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            def _count_distinct_leads(action: str, direction: str = "outbound",
                                      extra: str = "") -> int:
                sql = ("SELECT COUNT(DISTINCT lead_id) FROM interactions "
                       "WHERE platform = ? AND action = ? AND direction = ?")
                params: list = [platform, action, direction]
                if cutoff:
                    sql += " AND created_at >= ?"
                    params.append(cutoff)
                if extra:
                    sql += f" {extra}"
                return conn.execute(sql, params).fetchone()[0]

            discovered = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE source_platform = ?"
                + (f" AND created_at >= '{cutoff}'" if cutoff else ""),
                (platform,),
            ).fetchone()[0]

            followed = _count_distinct_leads("follow", "outbound")
            follow_back = _count_distinct_leads("follow_back", "inbound")
            # chatted: 所有收到过我方 DM 的 lead（send_dm/auto_reply/follow_up 均算），用 IN + DISTINCT 去重
            _chatted_sql = (
                "SELECT COUNT(DISTINCT lead_id) FROM interactions "
                "WHERE platform = ? AND action IN ('send_dm', 'auto_reply', 'follow_up') AND direction = 'outbound'"
            )
            _chatted_p: list = [platform]
            if cutoff:
                _chatted_sql += " AND created_at >= ?"
                _chatted_p.append(cutoff)
            chatted = conn.execute(_chatted_sql, _chatted_p).fetchone()[0]
            auto_replied = _count_distinct_leads("auto_reply", "outbound")
            follow_up_sent = _count_distinct_leads("follow_up", "outbound")
            # P10-A: 计入 dm_received + message_received 两种 action（收件箱回复）
            _replied_sql = (
                "SELECT COUNT(DISTINCT lead_id) FROM interactions "
                "WHERE platform = ? AND action IN ('dm_received', 'message_received') AND direction = 'inbound'"
            )
            _replied_p: list = [platform]
            if cutoff:
                _replied_sql += " AND created_at >= ?"
                _replied_p.append(cutoff)
            dm_received = conn.execute(_replied_sql, _replied_p).fetchone()[0]

            status_counts = {}
            for status in LEAD_STATUSES:
                row = conn.execute(
                    "SELECT COUNT(*) FROM leads l "
                    "JOIN platform_profiles pp ON l.id = pp.lead_id "
                    "WHERE pp.platform = ? AND l.status = ?",
                    (platform, status),
                ).fetchone()
                status_counts[status] = row[0] if row else 0

            converted = status_counts.get("converted", 0)
            qualified = status_counts.get("qualified", 0)

            # 成交口径：须手工或对接订单回写 conversion_value，否则只有「阶段转化」无金额
            rev_sql = (
                "SELECT COALESCE(SUM(l.conversion_value), 0), "
                "COUNT(CASE WHEN l.conversion_value IS NOT NULL THEN 1 END), "
                "COALESCE(AVG(l.conversion_value), 0) "
                "FROM leads l WHERE l.status = 'converted' AND ("
                "l.source_platform = ? OR l.id IN "
                "(SELECT lead_id FROM platform_profiles WHERE platform = ?))"
            )
            rev_params: list = [platform, platform]
            if cutoff:
                rev_sql += " AND l.created_at >= ?"
                rev_params.append(cutoff)
            rev_row = conn.execute(rev_sql, rev_params).fetchone()
            revenue_total = float(rev_row[0] or 0)
            revenue_count = int(rev_row[1] or 0)
            revenue_avg = float(rev_row[2] or 0)

            return {
                "period_days": days,
                "platform": platform,
                "funnel": {
                    "discovered": discovered,
                    "followed": followed,
                    "follow_back": follow_back,
                    "chatted": chatted,
                    "replied": dm_received,
                    "qualified": qualified,
                    "converted": converted,
                },
                "revenue": {
                    "total_recorded": round(revenue_total, 2),
                    "deals_with_value": revenue_count,
                    "avg_deal": round(revenue_avg, 2),
                    "hint": "回写 POST /leads/{id}/conversion 后才有金额统计",
                },
                "rates": {
                    "follow_rate": round(followed / max(discovered, 1), 4),
                    "followback_rate": round(follow_back / max(followed, 1), 4),
                    "chat_rate": round(chatted / max(follow_back, 1), 4),
                    "reply_rate": round(dm_received / max(chatted, 1), 4),
                    "qualification_rate": round(qualified / max(dm_received, 1), 4),
                    "conversion_rate": round(converted / max(discovered, 1), 4),
                    "overall_funnel": round(converted / max(discovered, 1), 4),
                },
                "engagement": {
                    "auto_replies_sent": auto_replied,
                    "follow_ups_sent": follow_up_sent,
                },
                "status_distribution": status_counts,
            }
        finally:
            conn.close()

    def get_daily_funnel(self, platform: str = "tiktok",
                         days: int = 7) -> List[Dict[str, Any]]:
        """Get per-day funnel data for trend analysis."""
        conn = self._conn()
        results = []

        for i in range(days):
            from datetime import timedelta
            day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
            day_start = f"{day}T00:00:00"
            day_end = f"{day}T23:59:59"

            def _day_count(action: str, direction: str = "outbound") -> int:
                return conn.execute(
                    "SELECT COUNT(DISTINCT lead_id) FROM interactions "
                    "WHERE platform = ? AND action = ? AND direction = ? "
                    "AND created_at >= ? AND created_at <= ?",
                    (platform, action, direction, day_start, day_end),
                ).fetchone()[0]

            results.append({
                "date": day,
                "followed": _day_count("follow", "outbound"),
                "follow_back": _day_count("follow_back", "inbound"),
                "chatted": _day_count("send_dm", "outbound"),
                "replied": _day_count("dm_received", "inbound"),
                "auto_replies": _day_count("auto_reply", "outbound"),
            })

        conn.close()
        return list(reversed(results))

    @staticmethod
    def _lead_to_dict(row) -> Dict[str, Any]:
        d = dict(row)
        if "tags" in d and isinstance(d["tags"], str):
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        return d


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_store: Optional[LeadsStore] = None
_store_lock = threading.Lock()


def get_leads_store(db_path: Optional[str] = None) -> LeadsStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = LeadsStore(db_path)
    return _store
