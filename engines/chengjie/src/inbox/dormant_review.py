# -*- coding: utf-8 -*-
"""「沉寂会话待你决定」清单（P-2 B / C / D · #259 #252 · D-P1，2026-09-08）。

三件事收在一个模块（都围着同一个判据「这条入站是不是 72 小时以前的旧消息」）：

1. **年龄闸配置与判定**：``max_inbound_age_hours(config)``（``inbox.auto_draft.max_inbound_age_hours``，
   默认 72，≤0 关）、``inbound_age_hours(store, cid, text)``。``drafts.auto_generate_draft`` 入口
   唯一一处调用 ``check_stale_inbound`` —— 超龄不起草、进清单、记
   ``[draft] skip=stale_inbound conv=… age_h=…``。
2. **清单持久层** ``DormantReviewStore``（``dormant_review.db`` 与 inbox.db 同目录，零 store.py
   改动）：``dormant_review`` 行 = 待坐席决定的旧会话（来源 stale_inbound / backfill_unreplied），
   三动作 ignore / manual / draft；``login_review`` 行 = 登录同步完成后待弹一次的确认框。
3. **回填结算观察者**：``note_backfill`` 收集本次登录回填的客户入站，静默 8s 后 ``settle_backfill``
   —— 跑停联登录扫描（E，``stop_contact.login_scan_backfill``）、把「最后一条是客户入站且未回复」
   的会话进清单、若该账号已是全自动则登记一条 login_review（前端轮询到即弹两栏确认框一次）。

``split_targets`` 给切档确认框（C）分栏：active（最近 3 天有来信）/ dormant（旧消息，去清单）/
frozen（已停联）/ self_chat（我自己）。``draft_preview`` = 三按钮之「让 AI 起草预览」与确认框
逐行预览共用的一条产线（``generate_persona_reply`` → **永远 review** 的 L1 稿，与 regenerate 同口径）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_NAME = "dormant_review.db"
DEFAULT_MAX_INBOUND_AGE_HOURS = 72.0
#: 回填结算静默窗（最后一条回填到达后多少秒视为「同步完成」）
SETTLE_DEBOUNCE_SEC = 8.0
#: 忽略动作打的会话标签（数据值）
IGNORED_TAG = "dormant:ignored"
REASON_STALE = "stale_inbound"
REASON_BACKFILL = "backfill_unreplied"
ACTIONS = ("ignore", "manual", "draft")
_STATUS_OPEN = "pending"

_DDL = """
CREATE TABLE IF NOT EXISTS dormant_review (
    conversation_id TEXT PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT '',
    account_id      TEXT NOT NULL DEFAULT '',
    chat_key        TEXT NOT NULL DEFAULT '',
    display_name    TEXT NOT NULL DEFAULT '',
    inbound_ts      REAL NOT NULL DEFAULT 0,
    inbound_text    TEXT NOT NULL DEFAULT '',
    age_h           REAL NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    added_ts        REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_ts      REAL NOT NULL DEFAULT 0,
    decided_by      TEXT NOT NULL DEFAULT '',
    draft_id        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dr_status ON dormant_review(status, platform, account_id);
CREATE TABLE IF NOT EXISTS login_review (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    created_ts  REAL NOT NULL DEFAULT 0,
    dormant     INTEGER NOT NULL DEFAULT 0,
    frozen      INTEGER NOT NULL DEFAULT 0,
    self_chat   INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    acked_ts    REAL NOT NULL DEFAULT 0,
    acked_by    TEXT NOT NULL DEFAULT ''
);
"""


# ── 配置 / 年龄判定 ─────────────────────────────────────────────────────────

def max_inbound_age_hours(config: Optional[Dict[str, Any]] = None) -> float:
    """``inbox.auto_draft.max_inbound_age_hours``（默认 72；≤0 = 关闭年龄闸）。热读。"""
    raw: Any = None
    try:
        if config is None:
            from src.compliance.runtime import runtime_config
            config = runtime_config() or {}
        raw = (((config or {}).get("inbox") or {}).get("auto_draft") or {}).get(
            "max_inbound_age_hours")
    except Exception:
        raw = None
    if raw is None:
        return DEFAULT_MAX_INBOUND_AGE_HOURS
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INBOUND_AGE_HOURS


def latest_inbound(store: Any, conversation_id: str, text: str = "") -> Dict[str, Any]:
    """触发拟稿的那条入站行（正文逐字相同优先，否则最新入站）；无 → {}。绝不抛。"""
    if store is None or not conversation_id:
        return {}
    try:
        rows = store.list_recent_messages(conversation_id, limit=10) or []
    except Exception:
        return {}
    want = str(text or "").strip()
    newest: Dict[str, Any] = {}
    for r in rows:
        if str(r.get("direction") or "in") != "in":
            continue
        if want and str(r.get("text") or "").strip() == want:
            return dict(r)
        if float(r.get("ts") or 0) >= float(newest.get("ts") or 0):
            newest = dict(r)
    return newest


def inbound_age_hours(store: Any, conversation_id: str, text: str = "",
                      now: Optional[float] = None) -> Optional[float]:
    """入站年龄（小时）；判不出（无行 / ts=0 / 合成 ts）→ None。"""
    row = latest_inbound(store, conversation_id, text)
    ts = float(row.get("ts") or 0) if row else 0.0
    if ts <= 0 or int(row.get("approx_ts") or 0):
        return None
    return max(0.0, (float(now if now is not None else time.time()) - ts) / 3600.0)


def check_stale_inbound(store: Any, conv: Dict[str, Any], text: str, *,
                        config: Optional[Dict[str, Any]] = None,
                        now: Optional[float] = None) -> Optional[float]:
    """drafts.auto_generate_draft 入口唯一调用点：超龄 → 返回 age_h（调用方不起草），
    同时写清单 + 日志；未超龄 / 关闭 / 判不出 / 清单预览稿（conv 带 dormant_review=True）→ None。"""
    try:
        if (conv or {}).get("dormant_review"):
            return None
        limit_h = max_inbound_age_hours(config)
        if limit_h <= 0:
            return None
        cid = str((conv or {}).get("conversation_id") or "")
        age = inbound_age_hours(store, cid, text, now=now)
        if age is None or age <= limit_h:
            return None
        logger.info("[draft] skip=stale_inbound conv=%s age_h=%.1f limit_h=%.0f",
                    cid, age, limit_h)
        row = latest_inbound(store, cid, text)
        record_dormant(store, conv, text=str(row.get("text") or text or ""),
                       inbound_ts=float(row.get("ts") or 0), age_h=age,
                       reason=REASON_STALE)
        return age
    except Exception:
        logger.debug("[dormant] 年龄闸判定异常（放行）", exc_info=True)
        return None


# ── 持久层 ─────────────────────────────────────────────────────────────────

class DormantReviewStore:
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

    # 清单
    def upsert_pending(self, *, conversation_id: str, platform: str, account_id: str,
                       chat_key: str, display_name: str, inbound_ts: float,
                       inbound_text: str, age_h: float, reason: str,
                       now: Optional[float] = None) -> bool:
        """加入待决定（已 ignored / manual / drafted 的不复活，除非入站更新了）。返回是否新加。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        ts = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, inbound_ts FROM dormant_review WHERE conversation_id=?",
                (cid,)).fetchone()
            if cur is not None:
                if str(cur["status"]) != _STATUS_OPEN and float(inbound_ts or 0) <= float(
                        cur["inbound_ts"] or 0):
                    return False
                self._conn.execute(
                    "UPDATE dormant_review SET display_name=CASE WHEN ?!='' THEN ? ELSE display_name END,"
                    " inbound_ts=?, inbound_text=?, age_h=?, reason=?, added_ts=?, status='pending',"
                    " decided_ts=0, decided_by='', draft_id='' WHERE conversation_id=?",
                    (str(display_name or ""), str(display_name or ""), float(inbound_ts or 0),
                     str(inbound_text or "")[:200], float(age_h or 0), str(reason or ""), ts, cid))
                self._conn.commit()
                return str(cur["status"]) != _STATUS_OPEN
            self._conn.execute(
                "INSERT INTO dormant_review(conversation_id, platform, account_id, chat_key,"
                " display_name, inbound_ts, inbound_text, age_h, reason, added_ts, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'pending')",
                (cid, str(platform or "").lower(), str(account_id or ""), str(chat_key or ""),
                 str(display_name or ""), float(inbound_ts or 0), str(inbound_text or "")[:200],
                 float(age_h or 0), str(reason or ""), ts))
            self._conn.commit()
            return True

    def get(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dormant_review WHERE conversation_id=?",
                                     (str(conversation_id or ""),)).fetchone()
        return dict(row) if row is not None else None

    def list_pending(self, *, platform: str = "", account_id: str = "",
                     limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dormant_review WHERE status='pending'"
        args: List[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(str(platform).lower())
        if account_id:
            sql += " AND account_id=?"
            args.append(str(account_id))
        sql += " ORDER BY inbound_ts DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit or 200))))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count_pending(self, *, platform: str = "", account_id: str = "") -> int:
        sql = "SELECT COUNT(*) FROM dormant_review WHERE status='pending'"
        args: List[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(str(platform).lower())
        if account_id:
            sql += " AND account_id=?"
            args.append(str(account_id))
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()[0] or 0)

    def decide(self, conversation_id: str, status: str, *, by: str = "",
               draft_id: str = "", now: Optional[float] = None) -> bool:
        cid = str(conversation_id or "").strip()
        if not cid or status not in ("ignored", "manual", "drafted", "pending"):
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE dormant_review SET status=?, decided_ts=?, decided_by=?, draft_id=?"
                " WHERE conversation_id=?",
                (status, float(now if now is not None else time.time()), str(by or "")[:40],
                 str(draft_id or "")[:120], cid))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def drop(self, conversation_id: str) -> None:
        """客户又来真消息 → 这条不再「沉寂」，从清单移除（由 note_real_inbound 调用）。"""
        with self._lock:
            self._conn.execute("DELETE FROM dormant_review WHERE conversation_id=? AND status='pending'",
                               (str(conversation_id or ""),))
            self._conn.commit()

    # 登录确认框
    def add_login_review(self, *, platform: str, account_id: str, dormant: int, frozen: int,
                         self_chat: int, active: int, now: Optional[float] = None) -> int:
        """同账号已有 pending 的 → 更新计数不重复加行；返回行 id。"""
        ts = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM login_review WHERE platform=? AND account_id=? AND status='pending'",
                (str(platform).lower(), str(account_id))).fetchone()
            if cur is not None:
                self._conn.execute(
                    "UPDATE login_review SET created_ts=?, dormant=?, frozen=?, self_chat=?, active=?"
                    " WHERE id=?", (ts, int(dormant), int(frozen), int(self_chat), int(active),
                                    int(cur["id"])))
                self._conn.commit()
                return int(cur["id"])
            c = self._conn.execute(
                "INSERT INTO login_review(platform, account_id, created_ts, dormant, frozen,"
                " self_chat, active) VALUES (?,?,?,?,?,?,?)",
                (str(platform).lower(), str(account_id), ts, int(dormant), int(frozen),
                 int(self_chat), int(active)))
            self._conn.commit()
            return int(c.lastrowid or 0)

    def list_login_reviews(self, *, pending_only: bool = True, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM login_review"
        if pending_only:
            sql += " WHERE status='pending'"
        sql += " ORDER BY created_ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def ack_login_review(self, review_id: int, *, by: str = "", now: Optional[float] = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE login_review SET status='shown', acked_ts=?, acked_by=? WHERE id=? AND status='pending'",
                (float(now if now is not None else time.time()), str(by or "")[:40], int(review_id)))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_INSTANCES: Dict[str, DormantReviewStore] = {}
_INST_LOCK = threading.Lock()


def _db_path(store: Any) -> Optional[Path]:
    p = getattr(store, "_db_path", None)
    if p is None:
        return None
    try:
        return Path(str(p)).parent / DB_NAME
    except Exception:
        return None


def get_dormant_store(store: Any = None, *, db_path: Any = None) -> DormantReviewStore:
    path = Path(str(db_path)) if db_path else _db_path(store)
    key = str(path) if path else ":memory:"
    with _INST_LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            try:
                inst = DormantReviewStore(path)
            except Exception:
                logger.debug("[dormant] 打开 %s 失败，回落内存", key, exc_info=True)
                inst = DormantReviewStore(None)
            _INSTANCES[key] = inst
        return inst


def reset_for_tests() -> None:
    global _PENDING_BACKFILL, _SETTLE_TIMERS
    with _INST_LOCK:
        for inst in _INSTANCES.values():
            inst.close()
        _INSTANCES.clear()
    with _BF_LOCK:
        for t in _SETTLE_TIMERS.values():
            try:
                t.cancel()
            except Exception:
                pass
        _SETTLE_TIMERS = {}
        _PENDING_BACKFILL = {}


def record_dormant(store: Any, conv: Dict[str, Any], *, text: str, inbound_ts: float,
                   age_h: float, reason: str, now: Optional[float] = None) -> bool:
    """写一条待决定（B 段年龄闸 / A 段回填结算共用）。绝不抛。"""
    try:
        cid = str((conv or {}).get("conversation_id") or "")
        parts = cid.split(":", 2)
        plat = str(conv.get("platform") or (parts[0] if len(parts) == 3 else ""))
        acct = str(conv.get("account_id") or (parts[1] if len(parts) == 3 else "default"))
        ck = str(conv.get("chat_key") or (parts[2] if len(parts) == 3 else ""))
        ok = get_dormant_store(store).upsert_pending(
            conversation_id=cid, platform=plat, account_id=acct, chat_key=ck,
            display_name=str(conv.get("display_name") or conv.get("name") or ""),
            inbound_ts=float(inbound_ts or 0), inbound_text=str(text or ""),
            age_h=float(age_h or 0), reason=str(reason or ""), now=now)
        if ok:
            logger.info("[dormant] action=listed conv=%s reason=%s age_h=%.1f", cid, reason, age_h)
        return ok
    except Exception:
        logger.debug("[dormant] 清单写入失败", exc_info=True)
        return False


def note_real_inbound(store: Any, conversation_id: str) -> None:
    """客户又开口了 → 从清单撤下（由起草链在年龄闸放行时顺手调）。"""
    try:
        get_dormant_store(store).drop(conversation_id)
    except Exception:
        pass


# ── 回填结算观察者 ─────────────────────────────────────────────────────────
_BF_LOCK = threading.Lock()
_PENDING_BACKFILL: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
_SETTLE_TIMERS: Dict[Tuple[str, str], threading.Timer] = {}


def note_backfill(store: Any, *, platform: str, account_id: str, conversation_id: str,
                  chat_key: str, name: str, ts: float, direction: str, text: str,
                  debounce_sec: Optional[float] = None) -> None:
    """每条回填消息（含出站）记一笔；静默 debounce 后 ``settle_backfill``。
    ``debounce_sec=0`` → 只记不排（测试 / 调用方自己 settle）。绝不抛。"""
    try:
        key = (str(platform or "").lower(), str(account_id or ""))
        cid = str(conversation_id or "")
        if not key[0] or not key[1] or not cid:
            return
        with _BF_LOCK:
            bucket = _PENDING_BACKFILL.setdefault(key, {})
            cur = bucket.get(cid)
            tsf = float(ts or 0)
            # 只留该会话回填里**最新**的那条（判「最后一条是不是客户入站」用）
            if cur is None or tsf >= float(cur.get("ts") or 0):
                bucket[cid] = {"conversation_id": cid, "chat_key": str(chat_key or ""),
                               "name": str(name or ""), "ts": tsf,
                               "direction": str(direction or "in"), "text": str(text or "")}
            # 客户入站文本全部留一份给停联扫描（同会话多条：保最长命中面）
            if str(direction or "in") == "in" and text:
                bucket[cid].setdefault("in_texts", []).append(str(text)[:300])
            d = SETTLE_DEBOUNCE_SEC if debounce_sec is None else float(debounce_sec)
            if d <= 0:
                return
            old = _SETTLE_TIMERS.pop(key, None)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
            t = threading.Timer(d, settle_backfill, args=(store, key[0], key[1]))
            t.daemon = True
            _SETTLE_TIMERS[key] = t
            t.start()
    except Exception:
        logger.debug("[dormant] note_backfill 失败", exc_info=True)


def _account_is_auto(store: Any, platform: str, account_id: str) -> Tuple[bool, int]:
    """账号是否「已是全自动」：任一会话显式 auto_ai，或账号层决策为 auto_ai。返回 (是否, auto 会话数)。"""
    n_auto = 0
    try:
        prefix = f"{platform}:{account_id}:"
        for r in (store.list_automation_mode_rows() or []):
            if str(r.get("conversation_id") or "").startswith(prefix) and \
                    str(r.get("automation_mode") or "") == "auto_ai":
                n_auto += 1
    except Exception:
        n_auto = 0
    if n_auto > 0:
        return True, n_auto
    try:
        from src.inbox.account_mode_onboarding import decided_mode
        if str(decided_mode(platform, account_id) or "") == "auto_ai":
            return True, 0
    except Exception:
        pass
    return False, 0


def settle_backfill(store: Any, platform: str, account_id: str,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """登录 / 重连历史同步结束：停联扫描 + 沉寂入清单 + 登录确认框登记。可重入、绝不抛。"""
    key = (str(platform or "").lower(), str(account_id or ""))
    with _BF_LOCK:
        bucket = _PENDING_BACKFILL.pop(key, {}) or {}
        _SETTLE_TIMERS.pop(key, None)
    out: Dict[str, Any] = {"platform": key[0], "account_id": key[1], "conversations": len(bucket),
                           "dormant": 0, "scan": {}, "login_review_id": 0}
    if not bucket or store is None:
        return out
    ts_now = float(now if now is not None else time.time())
    # E：停联扫描（所有回填的客户入站文本）
    items: List[Dict[str, Any]] = []
    for cid, it in bucket.items():
        for t in it.get("in_texts") or []:
            items.append({"conversation_id": cid, "chat_key": it.get("chat_key"),
                          "name": it.get("name"), "text": t, "ts": it.get("ts")})
    try:
        from src.inbox.stop_contact import login_scan_backfill
        out["scan"] = login_scan_backfill(store, key[0], key[1], items, now=ts_now)
    except Exception:
        logger.debug("[dormant] login_scan 失败", exc_info=True)
    frozen_cids = set((out.get("scan") or {}).get("frozen") or [])
    # D：最后一条是客户入站且未回复 → 清单（冻结的 / 自聊不进）
    try:
        from src.inbox.normalizer import is_self_chat
        from src.inbox.stop_contact import frozen_reason
    except Exception:
        is_self_chat = lambda *a, **k: False  # noqa: E731
        frozen_reason = lambda *a, **k: ""    # noqa: E731
    for cid, it in bucket.items():
        if str(it.get("direction") or "in") != "in":
            continue
        try:
            if is_self_chat(key[0], key[1], str(it.get("chat_key") or "")):
                continue
            if cid in frozen_cids or frozen_reason(store, cid):
                continue
            # 库里该会话最后一条若已是我们的出站（回填只带了部分历史）→ 已回复，不进
            last = latest_last_message(store, cid)
            if last and str(last.get("direction") or "in") == "out":
                continue
            tsf = float(it.get("ts") or 0)
            age = max(0.0, (ts_now - tsf) / 3600.0) if tsf > 0 else 0.0
            if record_dormant(store, {"conversation_id": cid, "platform": key[0],
                                      "account_id": key[1], "chat_key": it.get("chat_key"),
                                      "display_name": it.get("name")},
                              text=str(it.get("text") or ""), inbound_ts=tsf, age_h=age,
                              reason=REASON_BACKFILL, now=ts_now):
                out["dormant"] += 1
        except Exception:
            logger.debug("[dormant] 回填会话判定失败 cid=%s", cid, exc_info=True)
    # C：账号已是全自动 → 登录确认框登记一次（前端轮询到即弹；不弹 = 默认不碰旧会话）
    try:
        is_auto, n_auto = _account_is_auto(store, key[0], key[1])
        if is_auto:
            sp = split_targets(store, key[0], key[1], None, now=ts_now)
            c = sp.get("counts") or {}
            if int(c.get("dormant") or 0) or int(c.get("frozen") or 0) or int(c.get("self_chat") or 0):
                out["login_review_id"] = get_dormant_store(store).add_login_review(
                    platform=key[0], account_id=key[1], dormant=int(c.get("dormant") or 0),
                    frozen=int(c.get("frozen") or 0), self_chat=int(c.get("self_chat") or 0),
                    active=int(c.get("active") or 0), now=ts_now)
    except Exception:
        logger.debug("[dormant] login_review 登记失败", exc_info=True)
    logger.info("[dormant] backfill_settled account=%s:%s conversations=%d dormant=%d "
                "stop_contact_hits=%s login_review=%s", key[0], key[1], out["conversations"],
                out["dormant"], (out.get("scan") or {}).get("hits", "-"),
                out["login_review_id"] or "-")
    return out


def latest_last_message(store: Any, conversation_id: str) -> Dict[str, Any]:
    try:
        rows = store.list_recent_messages(conversation_id, limit=1) or []
        return dict(rows[-1]) if rows else {}
    except Exception:
        return {}


# ── 切档确认框分栏（C） ────────────────────────────────────────────────────

def split_targets(store: Any, platform: str, account_id: str,
                  targets: Optional[Iterable[str]] = None, *,
                  config: Optional[Dict[str, Any]] = None, now: Optional[float] = None,
                  sample_limit: int = 300) -> Dict[str, Any]:
    """把账号会话分成 active / dormant / frozen / self_chat 四栏（含计数 + 样例行）。

    ``targets``=None → 该账号全部会话（登录确认框）；否则只分这些 cid（切档 plan 的 targets）。
    active = 最近 ``max_inbound_age_hours`` 内有客户来信；dormant = 其余（``unreplied`` 标出
    「最后一条是客户入站」——只有这些进清单）；frozen = 已停联（会话冻结或名单在场）；
    self_chat = 我自己。群 / 频道不在此分（plan 已单独跳过）。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "") or "default"
    ts_now = float(now if now is not None else time.time())
    limit_h = max_inbound_age_hours(config)
    if limit_h <= 0:
        limit_h = DEFAULT_MAX_INBOUND_AGE_HOURS
    out: Dict[str, Any] = {"active": [], "dormant": [], "frozen": [], "self_chat": [],
                           "counts": {"active": 0, "dormant": 0, "dormant_unreplied": 0,
                                      "frozen": 0, "self_chat": 0}, "limit_h": limit_h}
    if store is None or not plat:
        return out
    try:
        convs = store.list_conversations(limit=500, platform=plat, account_id=acct) or []
    except TypeError:
        convs = [c for c in (store.list_conversations(limit=500) or [])
                 if str(c.get("platform") or "") == plat
                 and str(c.get("account_id") or "default") == acct]
    except Exception:
        return out
    want = set(str(t) for t in targets) if targets is not None else None
    try:
        from src.inbox.account_blocklist import get_blocklist
        blocked = set(get_blocklist(store).blocked_peers(plat, acct))
    except Exception:
        blocked = set()
    try:
        from src.inbox.normalizer import is_self_chat
        from src.inbox.stop_contact import frozen_reason
    except Exception:
        return out
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        if not cid or (want is not None and cid not in want):
            continue
        ck = str(c.get("chat_key") or "")
        last_ts = float(c.get("last_ts") or 0)
        last_in = float(c.get("last_in_ts") or 0)
        row = {"conversation_id": cid, "chat_key": ck,
               "name": str(c.get("display_name") or ck),
               "last_ts": last_ts, "last_in_ts": last_in,
               "age_h": round((ts_now - last_in) / 3600.0, 1) if last_in > 0 else None,
               "unreplied": bool(last_in > 0 and last_in >= last_ts),
               "preview": str(c.get("last_text") or "")[:80]}
        if is_self_chat(plat, acct, ck):
            bucket = "self_chat"
        elif ck in blocked or frozen_reason(store, cid) == "stop_contact":
            bucket = "frozen"
        elif last_in > 0 and (ts_now - last_in) <= limit_h * 3600.0:
            bucket = "active"
        else:
            bucket = "dormant"
            if row["unreplied"]:
                out["counts"]["dormant_unreplied"] += 1
        out["counts"][bucket] += 1
        if len(out[bucket]) < int(sample_limit):
            out[bucket].append(row)
    return out


# ── 三按钮 / 预览 ──────────────────────────────────────────────────────────

def apply_action(store: Any, conversation_id: str, action: str, *, actor: str = "human",
                 config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """ignore / manual（同步）。draft 由路由走 ``draft_preview``（异步 LLM）再 ``mark_drafted``。"""
    cid = str(conversation_id or "").strip()
    act = str(action or "").strip().lower()
    out: Dict[str, Any] = {"ok": False, "conversation_id": cid, "action": act}
    if store is None or not cid or act not in ACTIONS:
        out["error"] = "bad_request"
        return out
    ds = get_dormant_store(store)
    if act == "ignore":
        try:
            tags = [str(t) for t in (store.get_conv_tags(cid) or [])]
            if IGNORED_TAG not in tags:
                store.set_conv_tags(cid, tags + [IGNORED_TAG])
        except Exception:
            logger.debug("[dormant] 忽略标签写入失败", exc_info=True)
        ds.decide(cid, "ignored", by=actor)
        out["ok"] = True
    elif act == "manual":
        try:
            try:
                store.set_automation_mode(cid, "manual", source=f"dormant:{actor}"[:80])
            except TypeError:
                store.set_automation_mode(cid, "manual")
        except Exception:
            logger.debug("[dormant] 档位切手动失败", exc_info=True)
        ds.decide(cid, "manual", by=actor)
        out["ok"] = True
    else:
        out["ok"] = True   # draft：路由随后调 draft_preview
    logger.info("[dormant] action=%s conv=%s by=%s", act, cid, actor or "-")
    return out


async def draft_preview(app: Any, store: Any, conversation_id: str, *,
                        actor: str = "human", source: str = "dormant") -> Dict[str, Any]:
    """为一个旧会话生成**一条 review 稿**（L1，永不进自动投递）——与 /api/drafts/{id}/regenerate
    同一条产线（``generate_persona_reply``）。返回 {ok, draft_id, draft_text, risk_level}。"""
    cid = str(conversation_id or "").strip()
    out: Dict[str, Any] = {"ok": False, "conversation_id": cid, "draft_id": "", "draft_text": ""}
    if store is None or not cid:
        out["error"] = "no_store"
        return out
    parts = cid.split(":", 2)
    if len(parts) != 3:
        out["error"] = "bad_cid"
        return out
    platform, account_id, chat_key = parts
    try:
        from src.inbox.stop_contact import frozen_reason
        if frozen_reason(store, cid):
            out["error"] = "frozen"
            logger.info("[dormant] action=draft conv=%s skipped=frozen", cid)
            return out
    except Exception:
        pass
    from src.inbox.persona_reply import generate_persona_reply, normalize_history
    rows: List[Dict[str, Any]] = []
    try:
        from src.ai.context_depth import history_fetch_limit as _hfl
        rows = store.list_recent_messages(cid, limit=_hfl(None, 30)) or []
    except Exception:
        rows = []
    msgs = [{"direction": str(r.get("direction") or "in"),
             "text": str(r.get("text") or r.get("original_text") or "").strip()}
            for r in rows if str(r.get("text") or r.get("original_text") or "").strip()]
    history, last_inbound = normalize_history(msgs)
    if not last_inbound:
        rec = get_dormant_store(store).get(cid) or {}
        last_inbound = str(rec.get("inbound_text") or "")
    if not last_inbound:
        out["error"] = "no_context"
        return out
    inbound_mid = ""
    for r in reversed(rows):
        if str(r.get("direction") or "") == "in" and str(r.get("message_id") or "").strip():
            inbound_mid = str(r.get("message_id"))
            break
    res = await generate_persona_reply(
        app=app, platform=platform, chat_key=chat_key, last_inbound=last_inbound,
        history=history, conversation_id=cid, account_id=account_id, inbound_msg_id=inbound_mid)
    reply = str((res or {}).get("reply") or "").strip()
    if not (res or {}).get("ok") or not reply:
        out["error"] = "generate_failed"
        return out
    import uuid
    from src.ai.chat_assistant_service import quick_analyze
    from src.inbox.drafts import _max_risk, keyword_risk_level, risk_to_autopilot
    analysis = quick_analyze(reply)
    risk_level = _max_risk(analysis.get("risk_level", "low"), keyword_risk_level(reply))
    # 永远 review：只产 L1 / L3 / L4，绝不 L2 进自动投递批次
    autopilot = risk_to_autopilot(risk_level, "review")
    chat_name = ""
    try:
        chat_name = str((store.get_conversation(cid) or {}).get("display_name") or "")
    except Exception:
        chat_name = ""
    draft_id = store.upsert_draft({
        "source_kind": "inbox", "source_id": f"{source}_" + uuid.uuid4().hex,
        "conversation_id": cid, "platform": platform, "account_id": account_id,
        "chat_key": chat_key, "chat_name": chat_name, "peer_text": last_inbound,
        "draft_text": reply, "draft_lang": str(res.get("reply_lang") or ""),
        "risk_level": risk_level, "risk_reasons": analysis.get("risk_reasons") or [],
        "autopilot_level": autopilot, "status": "pending",
        "created_at": time.time(), "trace_id": f"{source}:{cid}",
    })
    try:
        store.record_draft_audit(draft_id, autopilot_level=autopilot, action=f"{source}_preview",
                                 agent_id=actor, reason="dormant_review", risk_level=risk_level,
                                 conversation_id=cid)
    except Exception:
        pass
    try:
        get_dormant_store(store).decide(cid, "drafted", by=actor, draft_id=str(draft_id))
    except Exception:
        pass
    logger.info("[dormant] action=draft conv=%s draft=%s level=%s by=%s", cid, draft_id,
                autopilot, actor or "-")
    out.update({"ok": True, "draft_id": str(draft_id), "draft_text": reply,
                "risk_level": risk_level, "autopilot_level": autopilot})
    return out


def snapshot(store: Any, *, platform: str = "", account_id: str = "",
             limit: int = 200) -> Dict[str, Any]:
    """前端横幅 + 登录确认框轮询一次拿全：清单 + 计数 + 待弹的登录确认。"""
    ds = get_dormant_store(store)
    return {
        "items": ds.list_pending(platform=platform, account_id=account_id, limit=limit),
        "count": ds.count_pending(platform=platform, account_id=account_id),
        "total": ds.count_pending(),
        "login_reviews": ds.list_login_reviews(),
        "limit_h": max_inbound_age_hours(),
    }


__all__ = [
    "DEFAULT_MAX_INBOUND_AGE_HOURS", "SETTLE_DEBOUNCE_SEC", "IGNORED_TAG", "ACTIONS",
    "REASON_STALE", "REASON_BACKFILL", "max_inbound_age_hours", "inbound_age_hours",
    "latest_inbound", "check_stale_inbound", "DormantReviewStore", "get_dormant_store",
    "record_dormant", "note_real_inbound", "note_backfill", "settle_backfill",
    "split_targets", "apply_action", "draft_preview", "snapshot", "reset_for_tests",
]
