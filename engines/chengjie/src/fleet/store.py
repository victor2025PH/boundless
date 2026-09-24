"""主控侧 SQLite：节点注册表 / 注册码 / 任务队列 / 心跳摘要（单文件 ``fleet_control.db``）。

范式沿用 ``domains/player_care/commandbus.py``（线程锁 + 单连接 + 幂等 ack），扩展点：
* 节点鉴权：``node_key`` 只存 sha256，明文只在注册响应里出现一次；``rotate_key`` / ``revoke``。
* 注册幂等：同一 ``machine_id`` 再次注册（重装 Agent）→ 同一 ``node_id``、换新 key，不产生分身。
* 任务 TTL：``pull`` / ``list`` 前把过期 queued 标 ``expired``；stop 类优先级最高。
* 心跳历史每节点只保留最近 ``HEARTBEAT_KEEP`` 条，最新一条同时冗余在 nodes.last_heartbeat_json。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .detect import sanitize_instances
from .protocol import (
    ACK_STATUSES, DEFAULT_OFFLINE_AFTER_SEC, DEFAULT_TASK_TTL_SEC, FINAL_STATUSES, LEGACY_ALLOWED_KINDS,
    MAX_PULL_LIMIT, NODE_OFFLINE, NODE_ONLINE, NODE_REVOKED, STATUS_CANCELLED, STATUS_EXPIRED,
    STATUS_PULLED, STATUS_QUEUED, TASK_KINDS, TASK_PRIORITY, TASK_STOP_ACCOUNT, clamp_ttl,
    proto_compatible, sanitize_heartbeat, task_envelope,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_NAME = "fleet_control.db"
HEARTBEAT_KEEP = 200
NODE_ACTIVE = "active"
# 一次性注册码：Crockford base32，至少 12 字符（约 60 bit），默认 15 分钟。
# 已经发出的 8 位数字码在到期前仍可兑（本变更未上生产，库里若没有就没有在途码）。
# 机房密钥是另一条凭证：≥128 bit，只存哈希。
ENROLL_CODE_LEN = 12
ENROLL_CODE_TTL_MIN = 15
ENROLL_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, no I L O U
REVOKED_MACHINE_NOTE = "this machine was revoked"
CODE_FAIL_MAX = 8
CODE_FAIL_WINDOW_SEC = 600
# A whole room shares one egress IP. 8/hour rejected a legitimate batch, so the
# IP cap is high and the per-machine cap (below) stays the tight limit.
PENDING_IP_MAX = 240
PENDING_IP_WINDOW_SEC = 3600
PENDING_MACHINE_MAX = 3
PENDING_MACHINE_WINDOW_SEC = 3600
ROOM_FAIL_MAX = 20
ROOM_FAIL_WINDOW_SEC = 3600
PENDING_TTL_SEC = 24 * 3600
CLAIM_WINDOW_SEC = 15 * 60
PENDING_DEFAULT_GROUP = "pending-default"
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TEXT_UNSAFE = re.compile("[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id        TEXT PRIMARY KEY,
    machine_id     TEXT NOT NULL UNIQUE,
    host_name      TEXT NOT NULL DEFAULT '',
    label          TEXT NOT NULL DEFAULT '',
    group_name     TEXT NOT NULL DEFAULT '',
    key_hash       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    proto_version  INTEGER NOT NULL DEFAULT 0,
    agent_version  TEXT NOT NULL DEFAULT '',
    app_version    TEXT NOT NULL DEFAULT '',
    os             TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    enrolled_at    REAL NOT NULL,
    last_seen      REAL,
    last_heartbeat_json TEXT NOT NULL DEFAULT '{}',
    meta_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS enroll_codes (
    code         TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    group_name   TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    used_at      REAL,
    used_by_node TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS node_tasks (
    task_id      TEXT PRIMARY KEY,
    node_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    target_json  TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    priority     INTEGER NOT NULL DEFAULT 9,
    status       TEXT NOT NULL DEFAULT 'queued',
    ttl_sec      INTEGER NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    pulled_at    REAL,
    acked_at     REAL,
    result_json  TEXT NOT NULL DEFAULT '{}',
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nt_pull ON node_tasks(node_id, status, priority, created_at);
CREATE TABLE IF NOT EXISTS node_heartbeats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT NOT NULL,
    ts           REAL NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nh_node ON node_heartbeats(node_id, ts);
CREATE TABLE IF NOT EXISTS pending_enrollments (
    request_id    TEXT PRIMARY KEY,
    machine_id    TEXT NOT NULL,
    host_name     TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    group_name    TEXT NOT NULL DEFAULT '',
    os            TEXT NOT NULL DEFAULT '',
    agent_version TEXT NOT NULL DEFAULT '',
    app_version   TEXT NOT NULL DEFAULT '',
    client_ip     TEXT NOT NULL DEFAULT '',
    instances_json TEXT NOT NULL DEFAULT '[]',
    proto_version INTEGER NOT NULL DEFAULT 0,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    decided_at    REAL,
    decided_by    TEXT NOT NULL DEFAULT '',
    node_id       TEXT NOT NULL DEFAULT '',
    claim_key     TEXT NOT NULL DEFAULT '',
    claimed_at    REAL,
    enroll_secret_hash TEXT NOT NULL DEFAULT '',
    pairing_code  TEXT NOT NULL DEFAULT '',
    requested_group TEXT NOT NULL DEFAULT '',
    was_revoked   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_machine ON pending_enrollments(machine_id, status);
CREATE TABLE IF NOT EXISTS room_keys (
    key_id       TEXT PRIMARY KEY,
    key_hash     TEXT NOT NULL UNIQUE,
    fingerprint  TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    group_name   TEXT NOT NULL DEFAULT '',
    max_uses     INTEGER NOT NULL,
    uses         INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    revoked_at   REAL
);
CREATE TABLE IF NOT EXISTS enroll_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT NOT NULL DEFAULT '',
    machine_id TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enroll_attempts ON enroll_attempts(kind, ts);
"""


def _hash_key(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _clean_text(value: Any, limit: int) -> str:
    """Drop C0 controls, DEL, and bidi overrides before they land in the console."""
    return _TEXT_UNSAFE.sub("", str(value or ""))[:limit]


def _pairing_code() -> str:
    return "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(6))


def _secret_matches(secret: str, stored_hash: str) -> bool:
    secret = str(secret or "")
    stored = str(stored_hash or "")
    if len(secret) < 16 or len(stored) != 64:
        return False
    return hmac.compare_digest(_hash_key(secret), stored)


def _migrate_pending(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_enrollments)")}
    for name, decl in (
        ("enroll_secret_hash", "TEXT NOT NULL DEFAULT ''"),
        ("pairing_code", "TEXT NOT NULL DEFAULT ''"),
        ("requested_group", "TEXT NOT NULL DEFAULT ''"),
        ("was_revoked", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE pending_enrollments ADD COLUMN {name} {decl}")


def normalize_enroll_code(raw: str) -> str:
    """Strip dashes/spaces, uppercase, and apply Crockford aliases (O→0, I/L→1, U→V)."""
    chars = []
    for ch in str(raw or "").strip().upper():
        if ch in "- \t":
            continue
        if ch in "O":
            ch = "0"
        elif ch in "IL":
            ch = "1"
        elif ch == "U":
            ch = "V"
        chars.append(ch)
    return "".join(chars)


def format_enroll_code(canonical: str) -> str:
    """Group a new-style code as XXXX-XXXX-XXXX. Legacy 8-digit codes stay as stored."""
    c = str(canonical or "")
    if len(c) >= ENROLL_CODE_LEN and "-" not in c and all(ch in ENROLL_CODE_ALPHABET for ch in c):
        return "-".join(c[i:i + 4] for i in range(0, len(c), 4))
    return c


def _ip_bucket(ip: Optional[str]) -> str:
    return (str(ip or "").strip() or "-")[:64]


def _looks_like_room_key(token: str) -> bool:
    if not token.startswith("rk_") or len(token) < 24 or len(token) > 200:
        return False
    return all(c.isalnum() or c in "-_" for c in token)


def _room_usable(row: sqlite3.Row, ts: float) -> bool:
    if row["revoked_at"] is not None:
        return False
    if float(row["expires_at"]) < ts:
        return False
    if int(row["uses"]) >= int(row["max_uses"]):
        return False
    return True


def _load_list(s: Any) -> List[Any]:
    try:
        v = json.loads(s or "[]")
    except Exception:
        return []
    return v if isinstance(v, list) else []


def _loads(s: Any) -> Dict[str, Any]:
    try:
        v = json.loads(s or "{}")
    except Exception:
        return {}
    return v if isinstance(v, dict) else {}


def _dumps(d: Any) -> str:
    return json.dumps(d if isinstance(d, dict) else {}, ensure_ascii=False)


class FleetStore:
    def __init__(self, db_path: Any, *, offline_after_sec: int = DEFAULT_OFFLINE_AFTER_SEC) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.offline_after_sec = max(15, int(offline_after_sec or DEFAULT_OFFLINE_AFTER_SEC))
        self.code_fail_max = CODE_FAIL_MAX
        self.code_fail_window_sec = CODE_FAIL_WINDOW_SEC
        self.pending_ip_max = PENDING_IP_MAX
        self.pending_ip_window_sec = PENDING_IP_WINDOW_SEC
        self.pending_machine_max = PENDING_MACHINE_MAX
        self.pending_machine_window_sec = PENDING_MACHINE_WINDOW_SEC
        self.room_fail_max = ROOM_FAIL_MAX
        self.room_fail_window_sec = ROOM_FAIL_WINDOW_SEC
        self.pending_ttl_sec = PENDING_TTL_SEC
        self.claim_window_sec = CLAIM_WINDOW_SEC
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            _migrate_pending(self._conn)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── 注册码 ────────────────────────────────────────────────────────────
    def create_enroll_code(self, *, label: str = "", group_name: str = "", created_by: str = "",
                           ttl_min: int = ENROLL_CODE_TTL_MIN, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        ttl = max(1, min(7 * 24 * 60, int(ttl_min or ENROLL_CODE_TTL_MIN)))
        with self._lock:
            code = ""
            for _ in range(20):
                code = "".join(secrets.choice(ENROLL_CODE_ALPHABET) for _ in range(ENROLL_CODE_LEN))
                if self._conn.execute("SELECT 1 FROM enroll_codes WHERE code=?", (code,)).fetchone() is None:
                    break
            self._conn.execute(
                "INSERT INTO enroll_codes(code, label, group_name, created_by, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (code, str(label or "")[:80], str(group_name or "")[:80], str(created_by or "")[:80], ts, ts + ttl * 60),
            )
            self._conn.commit()
        return {"code": format_enroll_code(code), "label": label, "group_name": group_name,
                "expires_at": ts + ttl * 60, "created_at": ts}

    def list_enroll_codes(self, *, include_used: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute("SELECT * FROM enroll_codes ORDER BY created_at DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["code"] = format_enroll_code(str(r["code"]))
            d["status"] = "used" if r["used_at"] else ("expired" if r["expires_at"] < ts else "open")
            if include_used or d["status"] == "open":
                out.append(d)
        return out

    # ── 节点注册 / 鉴权 ───────────────────────────────────────────────────
    def enroll(self, *, code: str, machine_id: str, host_name: str = "", proto_version: Any = 0,
               agent_version: str = "", app_version: str = "", os_label: str = "", meta: Optional[Dict[str, Any]] = None,
               now: Optional[float] = None, client_ip: Optional[str] = None,
               enroll_secret: str = "", instances: Any = None) -> Dict[str, Any]:
        """用注册码换 node_key。返回 ``{"ok": True, "node_id", "node_key", ...}`` 或 ``{"ok": False, "error"}``。

        同一 machine_id 的在用节点再次注册 → 复用 node_id、签发新 key。已吊销的节点不复活：
        有效注册码只开一条待批准，并带 ``this machine was revoked``。码在待批准记下之后才作废。

        ``client_ip`` 非 None 时启用失败限速（公开入口必传）。限速期间不消耗仍有效的注册码。
        新码不区分大小写，可带短横线。库里尚未到期的 8 位数字码仍按原样兑。
        """
        ts = float(now if now is not None else time.time())
        code = normalize_enroll_code(code)
        mid = str(machine_id or "").strip()
        if not code or not mid:
            return {"ok": False, "error": "code_and_machine_id_required"}
        if not proto_compatible(proto_version):
            return {"ok": False, "error": "proto_incompatible", "server_proto": _server_proto()}
        with self._lock:
            if client_ip is not None and self._over_limit_locked(
                    ip=_ip_bucket(client_ip), machine_id="", kind="code_fail",
                    window=self.code_fail_window_sec, ip_max=self.code_fail_max, machine_max=0, now=ts):
                return {"ok": False, "error": "rate_limited"}
            row = self._conn.execute("SELECT * FROM enroll_codes WHERE code=?", (code,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] < ts:
                if client_ip is not None:
                    self._record_attempt_locked(ip=_ip_bucket(client_ip), machine_id=mid, kind="code_fail", ts=ts)
                    self._conn.commit()
                return {"ok": False, "error": "invalid_or_expired_code"}
            existing = self._conn.execute("SELECT status FROM nodes WHERE machine_id=?", (mid,)).fetchone()
            if existing is not None and str(existing["status"]) == NODE_REVOKED:
                pending = self.request_pending(
                    machine_id=mid, host_name=host_name, proto_version=proto_version,
                    agent_version=agent_version, app_version=app_version, os_label=os_label,
                    instances=instances, meta=meta,
                    client_ip="" if client_ip is None else str(client_ip),
                    enroll_secret=enroll_secret, requested_group=str(row["group_name"] or ""),
                    was_revoked=True, now=ts)
                if not pending.get("ok"):
                    return pending
                self._conn.execute(
                    "UPDATE enroll_codes SET used_at=?, used_by_node=? WHERE code=?",
                    (ts, "", row["code"]))
                self._conn.commit()
                pending["was_revoked"] = True
                pending["revoked_note"] = REVOKED_MACHINE_NOTE
                return pending
            node_id, key = self._activate_locked(
                machine_id=mid, host_name=host_name, label=row["label"], group_name=row["group_name"],
                proto_version=proto_version, agent_version=agent_version, app_version=app_version,
                os_label=os_label, meta=meta, ts=ts)
            self._conn.execute("UPDATE enroll_codes SET used_at=?, used_by_node=? WHERE code=?", (ts, node_id, row["code"]))
            self._conn.commit()
        return {"ok": True, "node_id": node_id, "node_key": key, "label": row["label"],
                "group_name": row["group_name"], "server_proto": _server_proto(), "status": "active"}

    def request_pending(self, *, machine_id: str, host_name: str = "", proto_version: Any = 0,
                        agent_version: str = "", app_version: str = "", os_label: str = "",
                        instances: Any = None, meta: Optional[Dict[str, Any]] = None,
                        label: str = "", group_name: str = "", client_ip: str = "",
                        enroll_secret: str = "", requested_group: str = "",
                        was_revoked: bool = False,
                        now: Optional[float] = None, ttl_sec: Optional[int] = None) -> Dict[str, Any]:
        """无注册码的安装：记一条待批准，不签发 node_key，不进入节点表。

        ``label`` / ``group_name`` 来自未鉴权请求时忽略。分组只在批准时由管理员指定。
        ``requested_group`` 仅主控内部使用（机房密钥撞上已有节点时记下原组，供控制台展示）。
        同一 machine_id 只有 enroll_secret 哈希相符才复用申请；否则另开一条。
        """
        del label, group_name  # unauthenticated callers must not set these
        ts = float(now if now is not None else time.time())
        mid = str(machine_id or "").strip()
        secret = str(enroll_secret or "")
        if not mid:
            return {"ok": False, "error": "machine_id_required"}
        if len(secret) < 16:
            return {"ok": False, "error": "enroll_secret_required"}
        if not proto_compatible(proto_version):
            return {"ok": False, "error": "proto_incompatible", "server_proto": _server_proto()}
        ip = _ip_bucket(client_ip)
        inst = sanitize_instances(instances)
        ttl = int(ttl_sec if ttl_sec is not None else self.pending_ttl_sec)
        ttl = max(60, min(14 * 24 * 3600, ttl))
        host = _clean_text(host_name, 120)
        os_name = _clean_text(os_label, 80)
        agent_v = _clean_text(agent_version, 40)
        app_v = _clean_text(app_version, 40)
        digest = _hash_key(secret)
        want_group = _clean_text(requested_group, 80)
        with self._lock:
            self._expire_pending_locked(ts)
            open_row = self._conn.execute(
                "SELECT * FROM pending_enrollments WHERE machine_id=? AND status='pending' AND expires_at>=? "
                "AND enroll_secret_hash=? ORDER BY created_at DESC LIMIT 1",
                (mid, ts, digest)).fetchone()
            if open_row is not None:
                flagged = 1 if was_revoked or int(open_row["was_revoked"] or 0) else 0
                self._conn.execute(
                    "UPDATE pending_enrollments SET host_name=?, os=?, agent_version=?, app_version=?, client_ip=?, "
                    "instances_json=?, proto_version=?, meta_json=?, was_revoked=? WHERE request_id=?",
                    (host, os_name, agent_v, app_v, ip[:64], json.dumps(inst, ensure_ascii=False),
                     int(proto_version), _dumps(meta), flagged, open_row["request_id"]))
                self._conn.commit()
                return {"ok": True, "status": "pending", "request_id": open_row["request_id"],
                        "pairing_code": open_row["pairing_code"],
                        "was_revoked": bool(flagged),
                        "revoked_note": REVOKED_MACHINE_NOTE if flagged else "",
                        "expires_at": open_row["expires_at"], "retry_after_sec": 15}
            if self._over_limit_locked(ip=ip, machine_id=mid, kind="pending_new",
                                       window=self.pending_ip_window_sec, ip_max=self.pending_ip_max,
                                       machine_max=self.pending_machine_max, machine_window=self.pending_machine_window_sec,
                                       now=ts):
                return {"ok": False, "error": "rate_limited"}
            request_id = "req_" + secrets.token_urlsafe(18)
            pair = _pairing_code()
            flagged = 1 if was_revoked else 0
            self._conn.execute(
                "INSERT INTO pending_enrollments(request_id, machine_id, host_name, label, group_name, os, "
                "agent_version, app_version, client_ip, instances_json, proto_version, meta_json, status, "
                "created_at, expires_at, enroll_secret_hash, pairing_code, requested_group, was_revoked) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?, ?, ?)",
                (request_id, mid[:80], host, "", "", os_name, agent_v, app_v, ip[:64],
                 json.dumps(inst, ensure_ascii=False), int(proto_version), _dumps(meta), ts, ts + ttl,
                 digest, pair, want_group, flagged))
            self._record_attempt_locked(ip=ip, machine_id=mid, kind="pending_new", ts=ts)
            self._conn.commit()
        return {"ok": True, "status": "pending", "request_id": request_id, "pairing_code": pair,
                "was_revoked": bool(flagged),
                "revoked_note": REVOKED_MACHINE_NOTE if flagged else "",
                "expires_at": ts + ttl, "retry_after_sec": 15}

    def list_pending(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._expire_pending_locked(ts)
            self._wipe_claims_locked(ts)
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM pending_enrollments WHERE status='pending' ORDER BY created_at ASC LIMIT 200").fetchall()
            owned = {
                str(n["machine_id"]): str(n["node_id"])
                for n in self._conn.execute("SELECT machine_id, node_id FROM nodes").fetchall()
            }
        return [self._pending_public(r, owned.get(str(r["machine_id"]), "")) for r in rows]

    def approve_pending(self, request_id: str, *, label: Optional[str] = None, group_name: Optional[str] = None,
                        decided_by: str = "", confirm_rotate: bool = False,
                        now: Optional[float] = None) -> Dict[str, Any]:
        """批准后签发 node_key，明文只放在待领取槽里，等节点来 poll。管理接口不返回明文。

        分组只用管理员这次传入的值；没传则落入 ``pending-default``。未鉴权请求里的分组不采用。
        machine_id 已经有节点时必须 ``confirm_rotate``，否则拒绝且不换 key。
        """
        ts = float(now if now is not None else time.time())
        rid = str(request_id or "").strip()
        with self._lock:
            self._expire_pending_locked(ts)
            row = self._conn.execute("SELECT * FROM pending_enrollments WHERE request_id=?", (rid,)).fetchone()
            if row is None:
                self._conn.commit()
                return {"ok": False, "error": "not_pending"}
            if row["status"] == "expired" or (row["status"] == "pending" and float(row["expires_at"]) < ts):
                self._conn.commit()
                return {"ok": False, "error": "expired"}
            if row["status"] not in ("pending", "approved"):
                self._conn.commit()
                return {"ok": False, "error": "not_pending"}
            if row["status"] == "approved":
                self._conn.commit()
                return {"ok": True, "status": "approved", "node_id": row["node_id"], "already": True}
            existing = self._conn.execute(
                "SELECT node_id FROM nodes WHERE machine_id=?", (row["machine_id"],)).fetchone()
            if existing is not None and not confirm_rotate:
                self._conn.commit()
                nid = str(existing["node_id"])
                return {"ok": False, "error": "confirm_rotate", "existing_node_id": nid,
                        "warning": f"approving will rotate key of {nid}"}
            use_label = _clean_text(label, 80) if label else ""
            if not use_label:
                use_label = _clean_text(row["host_name"], 80)
            supplied = "" if group_name is None else str(group_name).strip()
            use_group = _clean_text(supplied, 80) if supplied else PENDING_DEFAULT_GROUP
            meta = _loads(row["meta_json"])
            meta["instances"] = _load_list(row["instances_json"])
            meta["enrolled_via"] = "approval"
            meta["client_ip"] = row["client_ip"]
            node_id, key = self._activate_locked(
                machine_id=row["machine_id"], host_name=row["host_name"], label=use_label, group_name=use_group,
                proto_version=row["proto_version"], agent_version=row["agent_version"], app_version=row["app_version"],
                os_label=row["os"], meta=meta, ts=ts)
            self._conn.execute(
                "UPDATE pending_enrollments SET status='approved', label=?, group_name=?, node_id=?, claim_key=?, "
                "claimed_at=NULL, decided_at=?, decided_by=? WHERE request_id=?",
                (use_label, use_group, node_id, key, ts, str(decided_by or "")[:80], rid))
            self._conn.commit()
        return {"ok": True, "status": "approved", "node_id": node_id, "group_name": use_group}

    def reject_pending(self, request_id: str, *, decided_by: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        rid = str(request_id or "").strip()
        with self._lock:
            row = self._conn.execute("SELECT * FROM pending_enrollments WHERE request_id=?", (rid,)).fetchone()
            if row is None or row["status"] != "pending":
                return {"ok": False, "error": "not_pending"}
            self._conn.execute(
                "UPDATE pending_enrollments SET status='rejected', claim_key='', decided_at=?, decided_by=? WHERE request_id=?",
                (ts, str(decided_by or "")[:80], rid))
            self._conn.commit()
        return {"ok": True, "status": "rejected"}

    def poll_pending(self, request_id: str, machine_id: str, *, enroll_secret: str = "",
                     now: Optional[float] = None) -> Dict[str, Any]:
        """节点来领结果。必须带上安装时的 enroll_secret。批准后的 node_key 在领取窗口内可重复取，过窗即擦掉。"""
        ts = float(now if now is not None else time.time())
        rid = str(request_id or "").strip()
        mid = str(machine_id or "").strip()
        with self._lock:
            self._expire_pending_locked(ts)
            self._wipe_claims_locked(ts)
            row = self._conn.execute("SELECT * FROM pending_enrollments WHERE request_id=?", (rid,)).fetchone()
            if row is None or str(row["machine_id"]) != mid or not _secret_matches(enroll_secret, row["enroll_secret_hash"]):
                self._conn.commit()
                return {"ok": False, "status": "unknown", "error": "unknown_request"}
            status = row["status"]
            if status == "pending":
                self._conn.commit()
                return {"ok": True, "status": "pending", "request_id": rid, "retry_after_sec": 15,
                        "expires_at": row["expires_at"]}
            if status == "rejected":
                self._conn.commit()
                return {"ok": False, "status": "rejected", "error": "rejected"}
            if status == "expired":
                self._conn.commit()
                return {"ok": False, "status": "expired", "error": "expired"}
            if status == "approved":
                key = str(row["claim_key"] or "")
                claimed = float(row["claimed_at"] or 0)
                if not key or (claimed and ts - claimed >= self.claim_window_sec):
                    self._conn.commit()
                    return {"ok": False, "status": "already_claimed", "error": "already_claimed"}
                if not claimed:
                    self._conn.execute("UPDATE pending_enrollments SET claimed_at=? WHERE request_id=?", (ts, rid))
                self._conn.commit()
                return {"ok": True, "status": "active", "node_id": row["node_id"], "node_key": key,
                        "label": row["label"], "group_name": row["group_name"], "server_proto": _server_proto()}
            self._conn.commit()
            return {"ok": False, "status": "unknown", "error": "unknown_request"}

    def create_room_key(self, *, label: str = "", group_name: str = "", max_uses: int = 50, ttl_hours: int = 168,
                        created_by: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        """签发机房密钥。明文只在这次返回里出现；库里只留 sha256 与指纹。"""
        ts = float(now if now is not None else time.time())
        uses = max(1, min(500, int(max_uses or 1)))
        hours = max(1, min(24 * 90, int(ttl_hours or 168)))
        token = "rk_" + secrets.token_urlsafe(32)
        key_id = "rk_id_" + secrets.token_hex(8)
        digest = _hash_key(token)
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_keys(key_id, key_hash, fingerprint, label, group_name, max_uses, uses, created_by, "
                "created_at, expires_at) VALUES (?,?,?,?,?,?,0,?,?,?)",
                (key_id, digest, digest[:12], str(label or "")[:80], str(group_name or "")[:80], uses,
                 str(created_by or "")[:80], ts, ts + hours * 3600))
            self._conn.commit()
        return {"key_id": key_id, "room_key": token, "fingerprint": digest[:12], "label": str(label or ""),
                "group_name": str(group_name or ""), "max_uses": uses, "uses": 0, "created_at": ts,
                "expires_at": ts + hours * 3600, "download_path": "/dl/" + token}

    def list_room_keys(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute("SELECT * FROM room_keys ORDER BY created_at DESC LIMIT 100").fetchall()
        return [self._room_public(r, now=ts) for r in rows]

    def revoke_room_key(self, key_id: str, *, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE room_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL", (ts, str(key_id or "")))
            self._conn.commit()
        return cur.rowcount > 0

    def room_key_for_download(self, token: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """下载链接校验。不返回明文（调用方手里已经有 URL 上的 token）。"""
        ts = float(now if now is not None else time.time())
        token = str(token or "").strip()
        if not _looks_like_room_key(token):
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM room_keys WHERE key_hash=?", (_hash_key(token),)).fetchone()
        if row is None or not _room_usable(row, ts):
            return None
        return self._room_public(row, now=ts)

    def redeem_room_key(self, token: str, *, machine_id: str, host_name: str = "", proto_version: Any = 0,
                        agent_version: str = "", app_version: str = "", os_label: str = "",
                        meta: Optional[Dict[str, Any]] = None, client_ip: Optional[str] = None,
                        enroll_secret: str = "", instances: Any = None,
                        now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        token = str(token or "").strip()
        mid = str(machine_id or "").strip()
        if not token or not mid:
            return {"ok": False, "error": "room_key_and_machine_id_required"}
        if not proto_compatible(proto_version):
            return {"ok": False, "error": "proto_incompatible", "server_proto": _server_proto()}
        if not _looks_like_room_key(token):
            return self._room_fail(client_ip, mid, ts, "invalid_room_key")
        with self._lock:
            if client_ip is not None and self._over_limit_locked(
                    ip=_ip_bucket(client_ip), machine_id="", kind="room_fail",
                    window=self.room_fail_window_sec, ip_max=self.room_fail_max, machine_max=0, now=ts):
                return {"ok": False, "error": "rate_limited"}
            row = self._conn.execute("SELECT * FROM room_keys WHERE key_hash=?", (_hash_key(token),)).fetchone()
            if row is None:
                if client_ip is not None:
                    self._record_attempt_locked(ip=_ip_bucket(client_ip), machine_id=mid, kind="room_fail", ts=ts)
                    self._conn.commit()
                return {"ok": False, "error": "invalid_room_key"}
            if row["revoked_at"] is not None:
                return {"ok": False, "error": "revoked"}
            if float(row["expires_at"]) < ts:
                return {"ok": False, "error": "expired"}
            if int(row["uses"]) >= int(row["max_uses"]):
                return {"ok": False, "error": "exhausted"}
            existing = self._conn.execute("SELECT * FROM nodes WHERE machine_id=?", (mid,)).fetchone()
            room_group = str(row["group_name"] or "")
            if existing is not None and (
                str(existing["status"]) == NODE_REVOKED
                or str(existing["group_name"] or "") != room_group
            ):
                # Do not consume a use and do not rotate or revive the node.
                return self.request_pending(
                    machine_id=mid, host_name=host_name, proto_version=proto_version,
                    agent_version=agent_version, app_version=app_version, os_label=os_label,
                    instances=instances, meta=meta, client_ip="" if client_ip is None else str(client_ip),
                    enroll_secret=enroll_secret, requested_group=room_group,
                    was_revoked=str(existing["status"]) == NODE_REVOKED, now=ts)
            cur = self._conn.execute(
                "UPDATE room_keys SET uses=uses+1 WHERE key_id=? AND uses<max_uses "
                "AND revoked_at IS NULL AND expires_at>=?",
                (row["key_id"], ts))
            if cur.rowcount != 1:
                fresh = self._conn.execute("SELECT * FROM room_keys WHERE key_id=?", (row["key_id"],)).fetchone()
                self._conn.commit()
                if fresh is None:
                    return {"ok": False, "error": "invalid_room_key"}
                if fresh["revoked_at"] is not None:
                    return {"ok": False, "error": "revoked"}
                if float(fresh["expires_at"]) < ts:
                    return {"ok": False, "error": "expired"}
                return {"ok": False, "error": "exhausted"}
            merged = dict(meta or {})
            merged["enrolled_via"] = "room_key"
            merged["room_key_id"] = row["key_id"]
            node_id, key = self._activate_locked(
                machine_id=mid, host_name=host_name, label=row["label"], group_name=row["group_name"],
                proto_version=proto_version, agent_version=agent_version, app_version=app_version,
                os_label=os_label, meta=merged, ts=ts)
            self._conn.commit()
            uses_left = int(row["max_uses"]) - int(row["uses"]) - 1
        return {"ok": True, "status": "active", "node_id": node_id, "node_key": key, "label": row["label"],
                "group_name": row["group_name"], "server_proto": _server_proto(), "uses_left": uses_left}

    def _room_fail(self, client_ip: Optional[str], machine_id: str, ts: float, error: str) -> Dict[str, Any]:
        if client_ip is None:
            return {"ok": False, "error": error}
        with self._lock:
            if self._over_limit_locked(ip=_ip_bucket(client_ip), machine_id="", kind="room_fail",
                                       window=self.room_fail_window_sec, ip_max=self.room_fail_max, machine_max=0, now=ts):
                return {"ok": False, "error": "rate_limited"}
            self._record_attempt_locked(ip=_ip_bucket(client_ip), machine_id=machine_id, kind="room_fail", ts=ts)
            self._conn.commit()
        return {"ok": False, "error": error}

    def _activate_locked(self, *, machine_id: str, host_name: str, label: str, group_name: str, proto_version: Any,
                         agent_version: str, app_version: str, os_label: str, meta: Optional[Dict[str, Any]],
                         ts: float):
        key = "nk_" + secrets.token_hex(24)
        label = _clean_text(label, 80)
        group = _clean_text(group_name, 80)
        host = _clean_text(host_name, 120)
        agent_v = _clean_text(agent_version, 40)
        app_v = _clean_text(app_version, 40)
        os_name = _clean_text(os_label, 80)
        existing = self._conn.execute("SELECT * FROM nodes WHERE machine_id=?", (machine_id,)).fetchone()
        if existing is not None:
            node_id = existing["node_id"]
            self._conn.execute(
                "UPDATE nodes SET key_hash=?, status=?, host_name=?, label=CASE WHEN ?<>'' THEN ? ELSE label END, "
                "group_name=CASE WHEN ?<>'' THEN ? ELSE group_name END, proto_version=?, agent_version=?, "
                "app_version=?, os=?, enrolled_at=?, last_seen=?, meta_json=? WHERE node_id=?",
                (_hash_key(key), NODE_ACTIVE, host, label, label, group, group,
                 int(proto_version), agent_v, app_v, os_name, ts, ts, _dumps(meta), node_id),
            )
        else:
            node_id = f"n_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO nodes(node_id, machine_id, host_name, label, group_name, key_hash, status, proto_version, "
                "agent_version, app_version, os, created_at, enrolled_at, last_seen, meta_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, machine_id, host, label, group, _hash_key(key),
                 NODE_ACTIVE, int(proto_version), agent_v, app_v, os_name, ts, ts, ts, _dumps(meta)),
            )
        return node_id, key

    def _over_limit_locked(self, *, ip: str, machine_id: str, kind: str, window: int, ip_max: int,
                           machine_max: int, now: float, machine_window: Optional[int] = None) -> bool:
        since = now - int(window)
        if ip_max and ip:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM enroll_attempts WHERE kind=? AND ts>=? AND ip=?",
                (kind, since, ip)).fetchone()["n"]
            if int(n) >= int(ip_max):
                return True
        mw = int(machine_window if machine_window is not None else window)
        if machine_max and machine_id:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM enroll_attempts WHERE kind=? AND ts>=? AND machine_id=?",
                (kind, now - mw, machine_id)).fetchone()["n"]
            if int(n) >= int(machine_max):
                return True
        return False

    def _record_attempt_locked(self, *, ip: str, machine_id: str, kind: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO enroll_attempts(ip, machine_id, kind, ts) VALUES (?,?,?,?)",
            (str(ip or "")[:64], str(machine_id or "")[:80], kind, ts))
        self._conn.execute("DELETE FROM enroll_attempts WHERE ts<?", (ts - 2 * 86400,))

    def _expire_pending_locked(self, ts: float) -> None:
        self._conn.execute(
            "UPDATE pending_enrollments SET status='expired', claim_key='' WHERE status='pending' AND expires_at<?",
            (ts,))

    def _wipe_claims_locked(self, ts: float) -> None:
        cutoff = ts - self.claim_window_sec
        self._conn.execute(
            "UPDATE pending_enrollments SET claim_key='' WHERE status='approved' AND claim_key<>'' AND ("
            "(claimed_at IS NOT NULL AND claimed_at>0 AND claimed_at<?) OR "
            "(decided_at IS NOT NULL AND decided_at>0 AND decided_at<?))",
            (cutoff, cutoff))

    def _pending_public(self, row: sqlite3.Row, existing_node_id: str = "") -> Dict[str, Any]:
        group = str(row["group_name"] or "")
        nid = str(existing_node_id or "")
        revoked = bool(int(row["was_revoked"] or 0))
        return {
            "request_id": row["request_id"],
            "machine_id": row["machine_id"],
            "host_name": row["host_name"],
            "label": row["label"],
            "group_name": group,
            "requested_group": str(row["requested_group"] or ""),
            "effective_group": group or PENDING_DEFAULT_GROUP,
            "pairing_code": str(row["pairing_code"] or ""),
            "existing_node_id": nid,
            "was_revoked": revoked,
            "revoked_note": REVOKED_MACHINE_NOTE if revoked else "",
            "warning": f"approving will rotate key of {nid}" if nid else "",
            "os": row["os"],
            "agent_version": row["agent_version"],
            "app_version": row["app_version"],
            "client_ip": row["client_ip"],
            "instances": _load_list(row["instances_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _room_public(row: sqlite3.Row, *, now: float) -> Dict[str, Any]:
        if row["revoked_at"] is not None:
            status = "revoked"
        elif float(row["expires_at"]) < now:
            status = "expired"
        elif int(row["uses"]) >= int(row["max_uses"]):
            status = "exhausted"
        else:
            status = "open"
        return {
            "key_id": row["key_id"], "fingerprint": row["fingerprint"], "label": row["label"],
            "group_name": row["group_name"], "max_uses": int(row["max_uses"]), "uses": int(row["uses"]),
            "created_at": row["created_at"], "expires_at": row["expires_at"], "revoked_at": row["revoked_at"],
            "status": status,
        }

    def authenticate(self, node_key: str) -> Optional[Dict[str, Any]]:
        k = str(node_key or "").strip()
        if not k.startswith("nk_"):
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE key_hash=? AND status=?", (_hash_key(k), NODE_ACTIVE)).fetchone()
        return self._node(row) if row is not None else None

    def revoke(self, node_id: str, *, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute("UPDATE nodes SET status=? WHERE node_id=?", (NODE_REVOKED, str(node_id)))
            self._conn.execute(
                "UPDATE node_tasks SET status=?, detail='node_revoked', acked_at=? WHERE node_id=? AND status IN (?,?)",
                (STATUS_CANCELLED, ts, str(node_id), STATUS_QUEUED, STATUS_PULLED),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_node(self, node_id: str, *, label: Optional[str] = None, group_name: Optional[str] = None) -> bool:
        sets, args = [], []
        if label is not None:
            sets.append("label=?"); args.append(str(label)[:80])
        if group_name is not None:
            sets.append("group_name=?"); args.append(str(group_name)[:80])
        if not sets:
            return False
        args.append(str(node_id))
        with self._lock:
            cur = self._conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE node_id=?", args)
            self._conn.commit()
        return cur.rowcount > 0

    # ── 心跳 ──────────────────────────────────────────────────────────────
    def heartbeat(self, node_id: str, body: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        hb = sanitize_heartbeat(body)
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET last_seen=?, last_heartbeat_json=?, "
                "proto_version=COALESCE(?, proto_version), agent_version=CASE WHEN ?<>'' THEN ? ELSE agent_version END, "
                "app_version=CASE WHEN ?<>'' THEN ? ELSE app_version END, host_name=CASE WHEN ?<>'' THEN ? ELSE host_name END "
                "WHERE node_id=?",
                (ts, _dumps(hb), _int_or_none(hb.get("proto_version")),
                 _clean_text(hb.get("agent_version"), 40), _clean_text(hb.get("agent_version"), 40),
                 _clean_text(hb.get("app_version"), 40), _clean_text(hb.get("app_version"), 40),
                 _clean_text(hb.get("host_name"), 120), _clean_text(hb.get("host_name"), 120), str(node_id)),
            )
            self._conn.execute("INSERT INTO node_heartbeats(node_id, ts, summary_json) VALUES (?,?,?)",
                               (str(node_id), ts, _dumps(_hb_summary(hb))))
            self._conn.execute(
                "DELETE FROM node_heartbeats WHERE node_id=? AND id NOT IN "
                "(SELECT id FROM node_heartbeats WHERE node_id=? ORDER BY ts DESC LIMIT ?)",
                (str(node_id), str(node_id), HEARTBEAT_KEEP),
            )
            self._conn.commit()
        return {"ok": True, "server_time": ts, "server_proto": _server_proto()}

    def heartbeat_history(self, node_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT ts, summary_json FROM node_heartbeats WHERE node_id=? ORDER BY ts DESC LIMIT ?",
                                      (str(node_id), max(1, min(HEARTBEAT_KEEP, int(limit))))).fetchall()
        return [{"ts": r["ts"], **_loads(r["summary_json"])} for r in rows]

    # ── 任务 ──────────────────────────────────────────────────────────────
    def enqueue(self, node_id: str, kind: str, *, payload: Optional[Dict[str, Any]] = None,
                target: Optional[Dict[str, Any]] = None, ttl_sec: Any = DEFAULT_TASK_TTL_SEC,
                created_by: str = "", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """给节点下任务。节点不存在 / 已吊销 / kind 非法 → None。stop_account 入队时作废同号未领的其它任务。"""
        kind = str(kind or "").strip().lower()
        if kind not in TASK_KINDS:
            return None
        ts = float(now if now is not None else time.time())
        ttl = clamp_ttl(ttl_sec)
        with self._lock:
            node = self._conn.execute("SELECT status FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()
            if node is None or node["status"] != NODE_ACTIVE:
                return None
            payload = dict(payload or {})
            target = dict(target or {})
            if kind == TASK_STOP_ACCOUNT and target.get("phone"):
                self._conn.execute(
                    "UPDATE node_tasks SET status=?, detail='superseded_by_stop', acked_at=? "
                    "WHERE node_id=? AND status=? AND kind<>? AND json_extract(target_json, '$.phone')=?",
                    (STATUS_CANCELLED, ts, str(node_id), STATUS_QUEUED, TASK_STOP_ACCOUNT, str(target["phone"])),
                )
            tid = f"t_{uuid.uuid4().hex[:16]}"
            self._conn.execute(
                "INSERT INTO node_tasks(task_id, node_id, kind, target_json, payload_json, priority, status, ttl_sec, "
                "created_by, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, str(node_id), kind, _dumps(target), _dumps(payload), TASK_PRIORITY.get(kind, 9), STATUS_QUEUED,
                 ttl, str(created_by or "")[:80], ts, ts + ttl),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM node_tasks WHERE task_id=?", (tid,)).fetchone()
        return self._task_record(row)

    def _expire_locked(self, ts: float) -> None:
        self._conn.execute("UPDATE node_tasks SET status=?, detail='ttl_expired', acked_at=? WHERE status=? AND expires_at<?",
                           (STATUS_EXPIRED, ts, STATUS_QUEUED, ts))

    def pull(self, node_id: str, *, limit: int = MAX_PULL_LIMIT, node_proto: Any = None,
             now: Optional[float] = None) -> List[Dict[str, Any]]:
        """节点领任务（stop 最先），标 pulled。协议不兼容的节点只给 LEGACY_ALLOWED_KINDS。"""
        ts = float(now if now is not None else time.time())
        try:
            limit = max(1, min(MAX_PULL_LIMIT, int(limit)))
        except (TypeError, ValueError):
            limit = MAX_PULL_LIMIT
        legacy = node_proto is not None and not proto_compatible(node_proto)
        with self._lock:
            self._expire_locked(ts)
            rows = self._conn.execute(
                "SELECT * FROM node_tasks WHERE node_id=? AND status=? ORDER BY priority ASC, created_at ASC LIMIT ?",
                (str(node_id), STATUS_QUEUED, limit * 3 if legacy else limit),
            ).fetchall()
            if legacy:
                rows = [r for r in rows if r["kind"] in LEGACY_ALLOWED_KINDS][:limit]
            ids = [r["task_id"] for r in rows]
            if ids:
                self._conn.executemany("UPDATE node_tasks SET status=?, pulled_at=? WHERE task_id=?",
                                       [(STATUS_PULLED, ts, i) for i in ids])
            self._conn.commit()
        return [self._task_envelope(r) for r in rows]

    def has_queued(self, node_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM node_tasks WHERE node_id=? AND status=? LIMIT 1",
                                     (str(node_id), STATUS_QUEUED)).fetchone()
        return row is not None

    def ack(self, task_id: str, *, node_id: str, status: str, result: Optional[Dict[str, Any]] = None,
            detail: str = "", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """幂等回执：已终态不改；task 不属于该节点 → None（防串号）。"""
        tid = str(task_id or "").strip()
        status = str(status or "").strip().lower()
        if not tid or status not in ACK_STATUSES:
            return None
        ts = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute("SELECT * FROM node_tasks WHERE task_id=? AND node_id=?", (tid, str(node_id))).fetchone()
            if row is None:
                return None
            if row["status"] not in FINAL_STATUSES:
                self._conn.execute(
                    "UPDATE node_tasks SET status=?, detail=?, result_json=?, acked_at=? WHERE task_id=?",
                    (status, str(detail or "")[:500], _dumps(result), ts, tid),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM node_tasks WHERE task_id=?", (tid,)).fetchone()
        return self._task_record(row)

    def cancel(self, task_id: str, *, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute("UPDATE node_tasks SET status=?, detail='cancelled_by_operator', acked_at=? "
                                     "WHERE task_id=? AND status=?", (STATUS_CANCELLED, ts, str(task_id), STATUS_QUEUED))
            self._conn.commit()
        return cur.rowcount > 0

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM node_tasks WHERE task_id=?", (str(task_id),)).fetchone()
        return self._task_record(row) if row is not None else None

    def list_tasks(self, *, node_id: str = "", status: str = "", kind: str = "", limit: int = 100,
                   now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        sql, args = "SELECT * FROM node_tasks WHERE 1=1", []
        if node_id:
            sql += " AND node_id=?"; args.append(node_id)
        if status:
            sql += " AND status=?"; args.append(status)
        if kind:
            sql += " AND kind=?"; args.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit))))
        with self._lock:
            self._expire_locked(ts)
            self._conn.commit()
            rows = self._conn.execute(sql, args).fetchall()
        return [self._task_record(r) for r in rows]

    # ── 查询 / 汇总 ───────────────────────────────────────────────────────
    def get_node(self, node_id: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()
        return self._node(row, now=now) if row is not None else None

    def list_nodes(self, *, group_name: str = "", include_revoked: bool = True,
                   now: Optional[float] = None) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM nodes WHERE 1=1", []
        if group_name:
            sql += " AND group_name=?"; args.append(group_name)
        if not include_revoked:
            sql += " AND status=?"; args.append(NODE_ACTIVE)
        sql += " ORDER BY group_name, label, host_name"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._node(r, now=now) for r in rows]

    def overview(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        nodes = self.list_nodes(now=ts)
        by_state: Dict[str, int] = {}
        groups: Dict[str, Dict[str, int]] = {}
        accounts_total = 0
        accounts_online = 0
        health_sum: Dict[str, int] = {}
        for n in nodes:
            by_state[n["state"]] = by_state.get(n["state"], 0) + 1
            g = groups.setdefault(n["group_name"] or "默认", {"nodes": 0, "online": 0})
            g["nodes"] += 1
            if n["state"] == NODE_ONLINE:
                g["online"] += 1
            hb = n.get("last_heartbeat") or {}
            acc = hb.get("accounts") if isinstance(hb.get("accounts"), dict) else {}
            accounts_total += _int(acc.get("total"))
            accounts_online += _int(acc.get("online"))
            fh = hb.get("fleet_health") if isinstance(hb.get("fleet_health"), dict) else {}
            for k, v in (fh.get("by_state") or {}).items() if isinstance(fh.get("by_state"), dict) else []:
                health_sum[str(k)] = health_sum.get(str(k), 0) + _int(v)
        with self._lock:
            self._expire_locked(ts)
            self._conn.commit()
            trows = self._conn.execute("SELECT status, COUNT(*) AS n FROM node_tasks GROUP BY status").fetchall()
            recent_fail = self._conn.execute(
                "SELECT COUNT(*) AS n FROM node_tasks WHERE status IN ('failed','rejected','expired') AND acked_at>?",
                (ts - 86400,)).fetchone()["n"]
        return {
            "generated_at": ts,
            "nodes": {"total": len(nodes), **by_state},
            "groups": groups,
            "accounts": {"total": accounts_total, "online": accounts_online},
            "fleet_health": {"by_state": health_sum},
            "tasks": {"by_status": {r["status"]: int(r["n"]) for r in trows}, "failed_24h": int(recent_fail)},
            "open_enroll_codes": len(self.list_enroll_codes(now=ts)),
            "pending_enrollments": len(self.list_pending(now=ts)),
            "offline_after_sec": self.offline_after_sec,
        }

    # ── 形状 ──────────────────────────────────────────────────────────────
    def _node(self, row: sqlite3.Row, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        if row["status"] == NODE_REVOKED:
            state = NODE_REVOKED
        elif row["last_seen"] is not None and ts - float(row["last_seen"]) <= self.offline_after_sec:
            state = NODE_ONLINE
        else:
            state = NODE_OFFLINE
        return {
            "node_id": row["node_id"],
            "machine_id": row["machine_id"],
            "host_name": row["host_name"],
            "label": row["label"],
            "group_name": row["group_name"],
            "status": row["status"],
            "state": state,
            "proto_version": int(row["proto_version"] or 0),
            "proto_compatible": proto_compatible(row["proto_version"]),
            "agent_version": row["agent_version"],
            "app_version": row["app_version"],
            "os": row["os"],
            "created_at": row["created_at"],
            "enrolled_at": row["enrolled_at"],
            "last_seen": row["last_seen"],
            "last_heartbeat": _loads(row["last_heartbeat_json"]),
            "meta": _loads(row["meta_json"]),
        }

    @staticmethod
    def _task_envelope(row: sqlite3.Row) -> Dict[str, Any]:
        return task_envelope(task_id=row["task_id"], kind=row["kind"], node_id=row["node_id"],
                             payload=_loads(row["payload_json"]), target=_loads(row["target_json"]),
                             ttl_sec=int(row["ttl_sec"]), created_at=float(row["created_at"]))

    @classmethod
    def _task_record(cls, row: sqlite3.Row) -> Dict[str, Any]:
        rec = cls._task_envelope(row)
        rec.update({
            "status": row["status"], "detail": row["detail"], "created_by": row["created_by"],
            "created_at": row["created_at"], "pulled_at": row["pulled_at"], "acked_at": row["acked_at"],
            "result": _loads(row["result_json"]),
        })
        return rec


def _server_proto() -> int:
    from .protocol import PROTO_VERSION

    return PROTO_VERSION


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _hb_summary(hb: Dict[str, Any]) -> Dict[str, Any]:
    """心跳历史只留小摘要（账号数 / 在线数 / 实例状态 / 错误数），完整心跳只在 nodes 表留最新一份。"""
    acc = hb.get("accounts") if isinstance(hb.get("accounts"), dict) else {}
    inst = hb.get("instances") if isinstance(hb.get("instances"), list) else []
    metrics = hb.get("metrics") if isinstance(hb.get("metrics"), dict) else {}
    return {
        "accounts_total": _int(acc.get("total")),
        "accounts_online": _int(acc.get("online")),
        "instances_up": sum(1 for i in inst if isinstance(i, dict) and i.get("up")),
        "instances": len(inst),
        "errors": len(hb.get("errors") or []) if isinstance(hb.get("errors"), list) else 0,
        "cpu_pct": metrics.get("cpu_pct"),
        "mem_pct": metrics.get("mem_pct"),
    }


# ── 进程级单例（与 player_care.commandbus.get_outbox 同范式） ──────────────────
_store: Optional[FleetStore] = None
_store_sig: str = ""


def set_store(store: Optional[FleetStore]) -> None:
    global _store, _store_sig
    _store = store
    _store_sig = "injected" if store is not None else ""


def resolve_fleet_cfg(cfg_root: Any) -> Dict[str, Any]:
    """根 config 的 ``fleet_control:`` 段（全部有缺省）。"""
    root = cfg_root
    if hasattr(root, "config"):
        root = getattr(root, "config") or {}
    if not isinstance(root, dict):
        root = {}
    fc = root.get("fleet_control") if isinstance(root.get("fleet_control"), dict) else {}
    dl = fc.get("download") if isinstance(fc.get("download"), dict) else {}
    return {
        "db_path": str(fc.get("db_path") or ""),
        "offline_after_sec": _int(fc.get("offline_after_sec")) or DEFAULT_OFFLINE_AFTER_SEC,
        "heartbeat_sec": _int(fc.get("heartbeat_sec")) or 30,
        "enroll_code_ttl_min": _int(fc.get("enroll_code_ttl_min")) or ENROLL_CODE_TTL_MIN,
        "pending_ttl_sec": _int(fc.get("pending_ttl_sec")) or PENDING_TTL_SEC,
        "public_url": str(fc.get("public_url") or ""),
        "download": {
            "version": str(dl.get("version") or ""),
            "installer_url": str(dl.get("installer_url") or ""),
            "sha256": str(dl.get("sha256") or ""),
            "changelog_url": str(dl.get("changelog_url") or ""),
            "agent_zip_url": str(dl.get("agent_zip_url") or ""),
            "manifest_url": str(dl.get("manifest_url") or ""),
            "install_script_url": str(dl.get("install_script_url") or ""),
            "setup_url": str(dl.get("setup_url") or ""),
            "setup_sha256": str(dl.get("setup_sha256") or ""),
        },
    }


_manifest_cache: Dict[str, Any] = {"url": "", "at": 0.0, "data": None}
MANIFEST_TTL_SEC = 60


def _fetch_manifest(url: str) -> Optional[Dict[str, Any]]:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def resolve_download(cfg: Dict[str, Any], *, now: Optional[float] = None,
                     fetch: Callable[[str], Optional[Dict[str, Any]]] = _fetch_manifest) -> Dict[str, Any]:
    """下载元数据：``download.manifest_url``（build_agent.py 的 manifest.json，publish 后自动生效）优先，
    显式填写的 ``version / installer_url / sha256`` 覆盖 manifest；60s 缓存，拉不到就用配置里的静态值。"""
    dl = dict(cfg.get("download") or {})
    url = str(dl.get("manifest_url") or "")
    if not url:
        return _with_setup_url(dl)
    ts = float(now if now is not None else time.time())
    if _manifest_cache["url"] != url or ts - float(_manifest_cache["at"]) > MANIFEST_TTL_SEC:
        _manifest_cache.update({"url": url, "at": ts, "data": fetch(url)})
    m = _manifest_cache["data"] or {}
    merged = {
        "version": str(m.get("version") or ""),
        "installer_url": str(m.get("url") or ""),
        "sha256": str(m.get("sha256") or ""),
        "install_script_url": str(m.get("installer") or ""),
        "setup_url": str(m.get("setup_url") or ""),
        "setup_sha256": str(m.get("setup_sha256") or ""),
    }
    for k, v in merged.items():
        if v and not dl.get(k):
            dl[k] = v
    return _with_setup_url(dl)


def _with_setup_url(dl: Dict[str, Any]) -> Dict[str, Any]:
    """主按钮只在发布流程写了 setup_url 时出现。不从 chatx-agent.exe 猜一个还没上传的安装包地址。"""
    return dl


def get_store(cfg_root: Any) -> Optional[FleetStore]:
    global _store, _store_sig
    if _store_sig == "injected":
        return _store
    cfg = resolve_fleet_cfg(cfg_root)
    db_path = Path(cfg["db_path"]) if cfg["db_path"] else None
    cfg_path = getattr(cfg_root, "config_path", None)
    if not cfg_path and (db_path is None or not db_path.is_absolute()):
        return None
    cfg_dir = Path(cfg_path).parent if cfg_path else Path(".")
    if db_path is None:
        db_path = cfg_dir / DEFAULT_DB_NAME
    elif not db_path.is_absolute():
        db_path = cfg_dir / db_path
    sig = str(db_path)
    if _store is None or sig != _store_sig:
        try:
            _store = FleetStore(db_path, offline_after_sec=cfg["offline_after_sec"])
        except Exception:
            logger.warning("[fleet] 主控库打不开 %s", db_path, exc_info=True)
            return None
        _store_sig = sig
    return _store


__all__ = ["FleetStore", "DEFAULT_DB_NAME", "NODE_ACTIVE", "set_store", "get_store", "resolve_fleet_cfg"]
