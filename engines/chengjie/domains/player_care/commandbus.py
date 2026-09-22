"""player_care commandbus 发送端（B4）——智聊 → 智拓手机 的外呼指令出箱。

契约：huoke ``docs/CHATX_COMMANDBUS_CONTRACT.md``（只读，本文件不改它）。方向与
leadbus / replybus 相反：**智聊产出指令、手机拉取执行**。契约里智聊侧要实现的只有
两个端点（见 ``web/routes.py``）：

    GET  /api/commandbus/pull?account=<手机侧标识>&limit=N → {"available": true, "commands": [...]}
    POST /api/commandbus/ack {"command_id", "status": done|failed|rejected, "detail"} → {"available": true}

本文件只做「发送端」：把指令写进本域自己的 SQLite 出箱（``player_commandbus.db``，
与 contacts.db 同目录、不动核心表），供 pull 按 account 取、ack 幂等回执。**绝不直接发消息**
（执行权归手机，防双发）。

指令种类（契约目前只写了 greet / reply；本域按老板要求增加三种玩家关怀指令，信封形状
与契约一致：``command_id / kind / phone / account / ...``；待老板同步进契约）：

* ``reengage``：沉默玩家轻触达。``messages`` 是话术池（普通朋友口吻，不催充、不报数字），
  手机侧套自己的养号 / 时段窗 / 配额。
* ``stop``：对方说 STOP → 手机侧停止对该号一切主动触达。**STOP 一律先发 stop**：
  入箱 stop 时同号所有未拉走的 reengage / note 全部作废（cancelled），stop 排最前；
  之后再对该号 enqueue reengage 一律拒绝（``stopped`` 名单），直到显式 ``clear_stop``。
* ``note``：给手机侧执行人的备注（如「对方说这周忙，别打扰」）。不含任何发送动作。

fail-soft：出箱打不开 / 写失败 → 返回 None，不抛到主流程。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KIND_REENGAGE = "reengage"
KIND_STOP = "stop"
KIND_NOTE = "note"
KINDS = (KIND_REENGAGE, KIND_STOP, KIND_NOTE)

STATUS_QUEUED = "queued"
STATUS_PULLED = "pulled"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
ACK_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_REJECTED)
_FINAL = (STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED)

# stop 永远排在其它指令前面（数值越小越先拉）
_PRIORITY = {KIND_STOP: 0, KIND_NOTE: 5, KIND_REENGAGE: 9}

# reengage 话术池：普通朋友问候，一句话、不提游戏、不带任何数字 / 链接；手机侧随机选一条。
# 契约 greet.messages 同形（可含 {name}）。
_REENGAGE_POOL = {
    "tl": [
        "Uy {name}, kumusta? Ang tagal nating hindi nag-usap.",
        "Hi po, musta na? Busy ba lately?",
        "Kumusta na? Naalala lang kita, ha.",
    ],
    "en": [
        "Hey {name}, how've you been? Been a while.",
        "Hi, how's it going lately?",
        "Just thought of you — all good on your end?",
    ],
    "zh": [
        "嘿，最近怎么样？好久没聊了。",
        "在忙吗？想起你了，问一句。",
    ],
}


def reengage_pool(lang: str = "") -> List[str]:
    """按语言给 reengage 话术池；未知 / 空语言 → 菲 + 英 混合。"""
    key = str(lang or "").strip().lower()[:2]
    if key in _REENGAGE_POOL:
        return list(_REENGAGE_POOL[key])
    return list(_REENGAGE_POOL["tl"]) + list(_REENGAGE_POOL["en"])


DEFAULT_DB_NAME = "player_commandbus.db"
DEFAULT_PULL_LIMIT = 20
MAX_PULL_LIMIT = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_commands (
    command_id   TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    priority     INTEGER NOT NULL DEFAULT 9,
    status       TEXT NOT NULL DEFAULT 'queued',
    detail       TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    pulled_at    REAL,
    acked_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_pc_pull ON player_commands(account, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_pc_phone ON player_commands(phone, status);
CREATE TABLE IF NOT EXISTS player_stops (
    phone      TEXT PRIMARY KEY,
    account    TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


# 对方明确要求别再发（英 / 菲 / 中）。整句只含这些词（允许标点 / 客气语）才算，
# 避免「stop na yung laro ko」之类的正常聊天被误判。
STOP_RE = re.compile(
    r"^\W*(?:please\s+|pls\s+|paki\s*|请\s*|麻烦\s*)?"
    r"(?:stop|unsubscribe|unsub|tanggalin|tanggalin\s+mo\s+ako|huwag\s+(?:na|mo\s+na)\s+(?:ako\s+)?(?:i-?message|i-?chat|kulitin|istorbohin)|"
    r"wag\s+(?:na|mo\s+na)\s+(?:ako\s+)?(?:i-?message|i-?chat|kulitin|istorbohin)|"
    r"别再发(?:了|消息)?|不要再发(?:了|消息)?|别发了|不要发了|取消订阅|退订|别烦我|不要烦我)"
    r"\W*(?:po|na|please|pls|ha|了|吧)?\W*$",
    re.IGNORECASE,
)


def is_stop_message(text: Any) -> bool:
    """对方这句是否是 STOP（整句匹配，宁漏勿误）。"""
    return bool(STOP_RE.match(str(text or "").strip()))


def resolve_commandbus_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``player_care.commandbus`` 段，全部有缺省。预设默认 **关**（开了但手机侧没排
    ``chatx_command_poll`` 只会在出箱里积压）；智拓侧 executor 已对齐，在 config_player 里打开即生效。"""
    root = cfg_root
    if hasattr(root, "config"):
        root = getattr(root, "config") or {}
    if not isinstance(root, dict):
        root = {}
    pc = root.get("player_care") if isinstance(root.get("player_care"), dict) else {}
    cb = pc.get("commandbus") if isinstance(pc.get("commandbus"), dict) else {}
    return {
        "enabled": bool(cb.get("enabled", False)),
        "db_path": str(cb.get("db_path") or ""),
        "reengage_on_dormant": bool(cb.get("reengage_on_dormant", True)),
        "stop_on_keyword": bool(cb.get("stop_on_keyword", True)),
        "pull_limit": max(1, min(MAX_PULL_LIMIT, int(cb.get("pull_limit", DEFAULT_PULL_LIMIT) or DEFAULT_PULL_LIMIT))),
    }


class CommandOutbox:
    """本域出箱：enqueue（发送端）/ pull / ack（供契约端点）。线程安全、单文件 SQLite。"""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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

    # ── 发送端 ────────────────────────────────────────────────────────────
    def is_stopped(self, phone: str) -> bool:
        p = str(phone or "").strip()
        if not p:
            return False
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM player_stops WHERE phone=?", (p,)).fetchone()
        return row is not None

    def clear_stop(self, phone: str) -> bool:
        p = str(phone or "").strip()
        if not p:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM player_stops WHERE phone=?", (p,))
            self._conn.commit()
        return cur.rowcount > 0

    def enqueue(
        self,
        kind: str,
        *,
        account: str,
        phone: str = "",
        payload: Optional[Dict[str, Any]] = None,
        created_by: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """写一条指令。返回信封；被 stopped 名单拒绝 / 参数不合法 → None。

        * ``stop``：同号未拉走的其它指令全部 cancelled，并把该号记入 stopped 名单；
          已有 queued 的 stop 不重复入箱（返回已有那条）。
        * ``reengage``：同号 stopped → 拒绝；同号已有 queued reengage → 不重复（返回已有）。
        """
        kind = str(kind or "").strip().lower()
        account = str(account or "").strip()
        phone = str(phone or "").strip()
        ts = float(now if now is not None else time.time())
        if kind not in KINDS or not account:
            return None
        if kind != KIND_NOTE and not phone:
            return None
        payload = dict(payload or {})
        with self._lock:
            if kind == KIND_STOP:
                self._conn.execute(
                    "UPDATE player_commands SET status=?, detail='superseded_by_stop', acked_at=? "
                    "WHERE phone=? AND status=? AND kind<>?",
                    (STATUS_CANCELLED, ts, phone, STATUS_QUEUED, KIND_STOP),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO player_stops(phone, account, reason, created_at) VALUES (?,?,?,?)",
                    (phone, account, str(payload.get("reason") or ""), ts),
                )
                dup = self._conn.execute(
                    "SELECT * FROM player_commands WHERE phone=? AND kind=? AND status=? LIMIT 1",
                    (phone, KIND_STOP, STATUS_QUEUED),
                ).fetchone()
                if dup is not None:
                    self._conn.commit()
                    return self._envelope(dup)
            elif kind == KIND_REENGAGE:
                if self._conn.execute("SELECT 1 FROM player_stops WHERE phone=?", (phone,)).fetchone():
                    return None
                dup = self._conn.execute(
                    "SELECT * FROM player_commands WHERE phone=? AND kind=? AND status=? LIMIT 1",
                    (phone, KIND_REENGAGE, STATUS_QUEUED),
                ).fetchone()
                if dup is not None:
                    return self._envelope(dup)
            cid = f"c_{uuid.uuid4().hex[:16]}"
            self._conn.execute(
                "INSERT INTO player_commands(command_id, kind, account, phone, payload_json, priority, "
                "status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, kind, account, phone, json.dumps(payload, ensure_ascii=False),
                 _PRIORITY.get(kind, 9), STATUS_QUEUED, str(created_by or ""), ts),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM player_commands WHERE command_id=?", (cid,)).fetchone()
        return self._envelope(row)

    # ── 契约端点 ──────────────────────────────────────────────────────────
    def pull(self, account: str, limit: int = DEFAULT_PULL_LIMIT, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """手机侧拉「指派给这台」的 queued 指令（stop 最先），并标 pulled。account 空 → []。"""
        account = str(account or "").strip()
        if not account:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = DEFAULT_PULL_LIMIT
        limit = max(1, min(MAX_PULL_LIMIT, limit))
        ts = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM player_commands WHERE account=? AND status=? "
                "ORDER BY priority ASC, created_at ASC LIMIT ?",
                (account, STATUS_QUEUED, limit),
            ).fetchall()
            ids = [r["command_id"] for r in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE player_commands SET status=?, pulled_at=? WHERE command_id=?",
                    [(STATUS_PULLED, ts, i) for i in ids],
                )
                self._conn.commit()
        return [self._envelope(r) for r in rows]

    def ack(self, command_id: str, status: str, detail: str = "", *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """幂等回执：已终态的同 command_id 再 ack 不改状态（返回当前记录）；未知 id → None。"""
        cid = str(command_id or "").strip()
        status = str(status or "").strip().lower()
        if not cid or status not in ACK_STATUSES:
            return None
        ts = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute("SELECT * FROM player_commands WHERE command_id=?", (cid,)).fetchone()
            if row is None:
                return None
            if row["status"] not in _FINAL:
                self._conn.execute(
                    "UPDATE player_commands SET status=?, detail=?, acked_at=? WHERE command_id=?",
                    (status, str(detail or "")[:500], ts, cid),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM player_commands WHERE command_id=?", (cid,)).fetchone()
        return self._record(row)

    # ── 查询 ──────────────────────────────────────────────────────────────
    def get(self, command_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM player_commands WHERE command_id=?", (str(command_id),)).fetchone()
        return self._record(row) if row is not None else None

    def list_recent(self, *, account: str = "", status: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM player_commands WHERE 1=1"
        args: List[Any] = []
        if account:
            sql += " AND account=?"
            args.append(account)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._record(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, status, COUNT(*) AS n FROM player_commands GROUP BY kind, status"
            ).fetchall()
            stopped = self._conn.execute("SELECT COUNT(*) AS n FROM player_stops").fetchone()["n"]
        by_kind: Dict[str, Dict[str, int]] = {}
        by_status: Dict[str, int] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
            by_status[r["status"]] = by_status.get(r["status"], 0) + int(r["n"])
        return {"by_kind": by_kind, "by_status": by_status, "stopped": int(stopped)}

    # ── 形状 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _payload(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            p = json.loads(row["payload_json"] or "{}")
        except Exception:
            p = {}
        return p if isinstance(p, dict) else {}

    @classmethod
    def _envelope(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """手机只消费的信封（契约 §3 形状）：command_id / kind / phone / account + 种类字段。"""
        env: Dict[str, Any] = {
            "command_id": row["command_id"],
            "kind": row["kind"],
            "account": row["account"],
            "phone": row["phone"],
        }
        env.update(cls._payload(row))
        env.setdefault("dry_run", False)
        return env

    @classmethod
    def _record(cls, row: sqlite3.Row) -> Dict[str, Any]:
        rec = cls._envelope(row)
        rec.update({
            "status": row["status"],
            "detail": row["detail"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "pulled_at": row["pulled_at"],
            "acked_at": row["acked_at"],
        })
        return rec


# ── 进程级单例（与 profile.get_profile_service 同范式） ───────────────────────
_outbox: Optional[CommandOutbox] = None
_outbox_sig: str = ""


def set_outbox(ob: Optional[CommandOutbox]) -> None:
    global _outbox, _outbox_sig
    _outbox = ob
    _outbox_sig = "injected" if ob is not None else ""


def get_outbox(cfg_root: Any, *, require_enabled: bool = True) -> Optional[CommandOutbox]:
    """按配置解析出箱路径。``require_enabled=True``（发送端）时 commandbus 未开 → None；
    契约端点 pull/ack 用 ``require_enabled=False``（未开也返回空指令，fail-soft）。"""
    global _outbox, _outbox_sig
    if _outbox_sig == "injected":
        return _outbox
    cfg = resolve_commandbus_cfg(cfg_root)
    if require_enabled and not cfg["enabled"]:
        return None
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
    if _outbox is None or sig != _outbox_sig:
        try:
            _outbox = CommandOutbox(db_path)
        except Exception:
            logger.warning("[player_care] commandbus 出箱打不开 %s", db_path, exc_info=True)
            return None
        _outbox_sig = sig
    return _outbox


# ── 发送端便捷函数（sync / hooks 调；全部 fail-soft） ─────────────────────────
def send_reengage(ob: Optional[CommandOutbox], *, account: str, phone: str, messages: List[str],
                  reason: str = "", created_by: str = "player_sync", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if ob is None:
        return None
    pool = [str(m).strip() for m in (messages or []) if str(m or "").strip()]
    try:
        return ob.enqueue(KIND_REENGAGE, account=account, phone=phone, created_by=created_by, now=now,
                          payload={"messages": pool, "reason": str(reason or "")})
    except Exception:
        logger.debug("[player_care] reengage 入箱失败", exc_info=True)
        return None


def send_stop(ob: Optional[CommandOutbox], *, account: str, phone: str, reason: str = "",
              created_by: str = "player_care", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if ob is None:
        return None
    try:
        return ob.enqueue(KIND_STOP, account=account, phone=phone, created_by=created_by, now=now,
                          payload={"reason": str(reason or "")})
    except Exception:
        logger.debug("[player_care] stop 入箱失败", exc_info=True)
        return None


def send_note(ob: Optional[CommandOutbox], *, account: str, text: str, phone: str = "",
              created_by: str = "operator", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if ob is None or not str(text or "").strip():
        return None
    try:
        return ob.enqueue(KIND_NOTE, account=account, phone=phone, created_by=created_by, now=now,
                          payload={"text": str(text).strip()[:1000]})
    except Exception:
        logger.debug("[player_care] note 入箱失败", exc_info=True)
        return None


__all__ = [
    "KINDS", "KIND_REENGAGE", "KIND_STOP", "KIND_NOTE", "ACK_STATUSES",
    "CommandOutbox", "resolve_commandbus_cfg", "get_outbox", "set_outbox",
    "send_reengage", "send_stop", "send_note", "reengage_pool", "STOP_RE", "is_stop_message",
]
