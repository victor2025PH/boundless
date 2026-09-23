"""主控侧 SQLite：节点注册表 / 注册码 / 任务队列 / 心跳摘要（单文件 ``fleet_control.db``）。

范式沿用 ``domains/player_care/commandbus.py``（线程锁 + 单连接 + 幂等 ack），扩展点：
* 节点鉴权：``node_key`` 只存 sha256，明文只在注册响应里出现一次；``rotate_key`` / ``revoke``。
* 注册幂等：同一 ``machine_id`` 再次注册（重装 Agent）→ 同一 ``node_id``、换新 key，不产生分身。
* 任务 TTL：``pull`` / ``list`` 前把过期 queued 标 ``expired``；stop 类优先级最高。
* 心跳历史每节点只保留最近 ``HEARTBEAT_KEEP`` 条，最新一条同时冗余在 nodes.last_heartbeat_json。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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
"""


def _hash_key(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


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
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── 注册码 ────────────────────────────────────────────────────────────
    def create_enroll_code(self, *, label: str = "", group_name: str = "", created_by: str = "",
                           ttl_min: int = 60, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        ttl = max(1, min(7 * 24 * 60, int(ttl_min or 60)))
        with self._lock:
            for _ in range(20):
                code = f"{secrets.randbelow(10 ** 8):08d}"
                if self._conn.execute("SELECT 1 FROM enroll_codes WHERE code=?", (code,)).fetchone() is None:
                    break
            self._conn.execute(
                "INSERT INTO enroll_codes(code, label, group_name, created_by, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (code, str(label or "")[:80], str(group_name or "")[:80], str(created_by or "")[:80], ts, ts + ttl * 60),
            )
            self._conn.commit()
        return {"code": code, "label": label, "group_name": group_name, "expires_at": ts + ttl * 60, "created_at": ts}

    def list_enroll_codes(self, *, include_used: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute("SELECT * FROM enroll_codes ORDER BY created_at DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["status"] = "used" if r["used_at"] else ("expired" if r["expires_at"] < ts else "open")
            if include_used or d["status"] == "open":
                out.append(d)
        return out

    # ── 节点注册 / 鉴权 ───────────────────────────────────────────────────
    def enroll(self, *, code: str, machine_id: str, host_name: str = "", proto_version: Any = 0,
               agent_version: str = "", app_version: str = "", os_label: str = "", meta: Optional[Dict[str, Any]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        """用注册码换 node_key。返回 ``{"ok": True, "node_id", "node_key", ...}`` 或 ``{"ok": False, "error"}``。

        同一 machine_id 重复注册 → 复用 node_id、签发新 key（旧 key 立即失效）；被吊销的节点
        也可凭新注册码复活（运维显式发码即授权）。"""
        ts = float(now if now is not None else time.time())
        code = str(code or "").strip()
        mid = str(machine_id or "").strip()
        if not code or not mid:
            return {"ok": False, "error": "code_and_machine_id_required"}
        if not proto_compatible(proto_version):
            return {"ok": False, "error": "proto_incompatible", "server_proto": _server_proto()}
        with self._lock:
            row = self._conn.execute("SELECT * FROM enroll_codes WHERE code=?", (code,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] < ts:
                return {"ok": False, "error": "invalid_or_expired_code"}
            key = "nk_" + secrets.token_hex(24)
            existing = self._conn.execute("SELECT * FROM nodes WHERE machine_id=?", (mid,)).fetchone()
            if existing is not None:
                node_id = existing["node_id"]
                self._conn.execute(
                    "UPDATE nodes SET key_hash=?, status=?, host_name=?, label=CASE WHEN ?<>'' THEN ? ELSE label END, "
                    "group_name=CASE WHEN ?<>'' THEN ? ELSE group_name END, proto_version=?, agent_version=?, "
                    "app_version=?, os=?, enrolled_at=?, last_seen=?, meta_json=? WHERE node_id=?",
                    (_hash_key(key), NODE_ACTIVE, str(host_name or "")[:120], row["label"], row["label"],
                     row["group_name"], row["group_name"], int(proto_version), str(agent_version or "")[:40],
                     str(app_version or "")[:40], str(os_label or "")[:80], ts, ts, _dumps(meta), node_id),
                )
            else:
                node_id = f"n_{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    "INSERT INTO nodes(node_id, machine_id, host_name, label, group_name, key_hash, status, proto_version, "
                    "agent_version, app_version, os, created_at, enrolled_at, last_seen, meta_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (node_id, mid, str(host_name or "")[:120], row["label"], row["group_name"], _hash_key(key),
                     NODE_ACTIVE, int(proto_version), str(agent_version or "")[:40], str(app_version or "")[:40],
                     str(os_label or "")[:80], ts, ts, ts, _dumps(meta)),
                )
            self._conn.execute("UPDATE enroll_codes SET used_at=?, used_by_node=? WHERE code=?", (ts, node_id, code))
            self._conn.commit()
        return {"ok": True, "node_id": node_id, "node_key": key, "label": row["label"],
                "group_name": row["group_name"], "server_proto": _server_proto()}

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
                 str(hb.get("agent_version") or "")[:40], str(hb.get("agent_version") or "")[:40],
                 str(hb.get("app_version") or "")[:40], str(hb.get("app_version") or "")[:40],
                 str(hb.get("host_name") or "")[:120], str(hb.get("host_name") or "")[:120], str(node_id)),
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
        "enroll_code_ttl_min": _int(fc.get("enroll_code_ttl_min")) or 60,
        "public_url": str(fc.get("public_url") or ""),
        "download": {
            "version": str(dl.get("version") or ""),
            "installer_url": str(dl.get("installer_url") or ""),
            "sha256": str(dl.get("sha256") or ""),
            "changelog_url": str(dl.get("changelog_url") or ""),
            "agent_zip_url": str(dl.get("agent_zip_url") or ""),
        },
    }


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
