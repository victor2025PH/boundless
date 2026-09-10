"""TikTok × huoke（获客）桥——脑手分离 opt-in（TikTok 线续做 E，2026-09-10；默认关，零痕迹）。

huoke 是兄弟仓的真机自动化产品（TikTok App 养号 / 评论区巡检 / 回评论）；本桥只做三件事，**评论回复 API 归 TK-2**：

1. **线索进线** ``POST /api/tiktok/huoke/leads``：huoke 侧 ``reply_engine=chengjie`` 时把评论区**高意向线索**（评论原文、作者、
   视频、意向分）按 ``make_message`` 形状送来 → 本机 ``emit_incoming`` 落收件箱（会话键 ``tiktok:comment:<user_id>``，
   ``account_id`` = huoke 设备账号，``source.mode="personal_rpa"``），账号经 ``leadbus_account.register_lead_account``
   幂等登记（``personal_rpa`` 不在 ``ORCHESTRATED_MODES`` → 本机编排器**绝不接管**）。按 ``comment_id`` 幂等。
   与 leadbus「[线索捕获]」占位符不同：这里的 ``text`` 是**评论原文**，是真实用户发言 → 允许起草。
2. **起草**：落库后调 ``protocol_bridge.maybe_auto_reply`` → 既有 reply hook（``DraftService.auto_generate_draft`` /
   人设产线）——**不另起一套大脑**（D-TK-6：两颗大脑必然人格分裂）。低于 ``min_intent`` 的线索不进线（huoke 侧本地
   规则继续处理），避免评论区噪音淹没收件箱。
3. **回传接口**（hand-back）：坐席审过 / 自动放行的回复经 ``send_via_adapters`` 回落到本模块的 ``TikTokHuokeAdapter``
   （编排器不拥有 personal_rpa 账号 → 必然回落）→ 进**受控出站队列**；huoke ``GET /api/tiktok/huoke/handback`` 认领、
   真机回评论、``POST /api/tiktok/huoke/handback/ack`` 回执 → 成功则 out 回显进线程；失败留痕。队列入口闸门：
   ``channel_policy.text_block_reason("tiktok")`` + 评论 150 字（TikTok 评论上限）+ 每号日上限（TK-1 §8 新号 10–20）+
   **同一入站只准回一条**（对方没再说话就别追第二条）。

回复窗字段：huoke 回执可带设备侧窗口状态（可能仍用 TK-1 规划旧名 ``window_sent_count`` 等）→ 经
``channel_policy.normalize_window_fields`` 归一到 DY 口径再存（D 段决议：不写进消息 ``source``）。

开关 ``tiktok.huoke_bridge.enabled``（默认 false）：不开 → 不挂路由、不追加适配器、不建状态库。全程 ``api_auth``（Bearer）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover
    Request = Any  # type: ignore[misc,assignment]
    Depends = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM = "tiktok"
SOURCE = "huoke"
KIND_COMMENT = "comment"
CHAT_PREFIX = f"{PLATFORM}:{KIND_COMMENT}:"

LEADS_ROUTE = "/api/tiktok/huoke/leads"
HANDBACK_ROUTE = "/api/tiktok/huoke/handback"
HANDBACK_ACK_ROUTE = "/api/tiktok/huoke/handback/ack"
STATUS_ROUTE = "/api/tiktok/huoke/status"

COMMENT_MAX_LEN = 150          # TikTok 评论字数上限（平台事实，非策略参数）
DEFAULT_MIN_INTENT = 0.6
DEFAULT_DAILY_CAP = 20         # TK-1 §8：新号 10–20 起
DEFAULT_CLAIM_TTL_SEC = 600.0  # huoke 认领后 10 分钟没回执 → 可被重新认领
SEEN_TTL_SEC = 7 * 24 * 3600.0

REASON_LOW_INTENT = "low_intent"
REASON_ONE_REPLY = "policy_one_reply_per_inbound"
REASON_DAILY_CAP = "policy_daily_cap"
REASON_TOO_LONG = "policy_comment_too_long"
REASON_NOT_BRIDGED = "not_bridged"


def bridge_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    blk = ((config or {}).get("tiktok") or {}).get("huoke_bridge") or {}
    if not isinstance(blk, dict):
        blk = {}

    def _f(k: str, d: float) -> float:
        try:
            return float(blk.get(k, d))
        except (TypeError, ValueError):
            return d

    return {
        "enabled": bool(blk.get("enabled", False)),
        "min_intent": max(0.0, min(1.0, _f("min_intent", DEFAULT_MIN_INTENT))),
        "daily_cap": max(1, int(_f("daily_cap", DEFAULT_DAILY_CAP))),
        "max_reply_len": max(1, min(COMMENT_MAX_LEN, int(_f("max_reply_len", COMMENT_MAX_LEN)))),
        "claim_ttl_sec": max(30.0, _f("claim_ttl_sec", DEFAULT_CLAIM_TTL_SEC)),
        "state_db_path": str(blk.get("state_db_path") or ""),
    }


def bridge_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return bridge_cfg(config)["enabled"]


def chat_key_for(user_id: str) -> str:
    return f"{CHAT_PREFIX}{str(user_id or '').strip()}"


def is_bridge_chat(chat_key: str) -> bool:
    return str(chat_key or "").startswith(CHAT_PREFIX)


# ── 状态库 ─────────────────────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS seen_comments(key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lead_accounts(
  account_id TEXT PRIMARY KEY, device_id TEXT NOT NULL DEFAULT '', first_ts REAL NOT NULL, last_ts REAL NOT NULL,
  leads_total INTEGER NOT NULL DEFAULT 0, last_window TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS chat_ctx(
  account_id TEXT NOT NULL, chat_key TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
  last_comment_id TEXT NOT NULL DEFAULT '', last_video_id TEXT NOT NULL DEFAULT '', last_inbound_ts REAL NOT NULL DEFAULT 0,
  out_since_inbound INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account_id, chat_key));
CREATE TABLE IF NOT EXISTS outbound(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, chat_key TEXT NOT NULL, text TEXT NOT NULL,
  comment_id TEXT NOT NULL DEFAULT '', video_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
  username TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued', created_at REAL NOT NULL,
  claimed_at REAL NOT NULL DEFAULT 0, claimed_by TEXT NOT NULL DEFAULT '', acked_at REAL NOT NULL DEFAULT 0,
  external_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_outbound_status ON outbound(status, account_id);
"""


class TikTokHuokeStateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self.default_path()
        if self.path != ":memory:":
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            except Exception:
                pass
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._conn.executescript(_DDL)
        self._conn.commit()

    @staticmethod
    def default_path() -> str:
        try:
            from src.licensing.data_paths import config_dir
            return str(config_dir() / "tiktok_huoke_bridge_state.db")
        except Exception:
            return os.path.join("config", "tiktok_huoke_bridge_state.db")

    # 幂等
    def seen(self, key: str, *, now: Optional[float] = None) -> bool:
        t = float(now if now is not None else time.time())
        with self._lock:
            if self._conn.execute("SELECT 1 FROM seen_comments WHERE key=?", (str(key),)).fetchone():
                return True
            self._conn.execute("INSERT OR IGNORE INTO seen_comments(key, ts) VALUES(?,?)", (str(key), t))
            self._conn.execute("DELETE FROM seen_comments WHERE ts < ?", (t - SEEN_TTL_SEC,))
            self._conn.commit()
        return False

    # 账号
    def touch_account(self, account_id: str, device_id: str = "", *, now: Optional[float] = None, leads: int = 0) -> None:
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO lead_accounts(account_id, device_id, first_ts, last_ts, leads_total) VALUES(?,?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET last_ts=excluded.last_ts, leads_total=lead_accounts.leads_total+excluded.leads_total, "
                "device_id=CASE WHEN excluded.device_id<>'' THEN excluded.device_id ELSE lead_accounts.device_id END",
                (str(account_id), str(device_id or ""), t, t, int(leads)))
            self._conn.commit()

    def account(self, account_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM lead_accounts WHERE account_id=?", (str(account_id),)).fetchone()
        if not row:
            return {}
        d = dict(row)
        try:
            d["last_window"] = json.loads(d.get("last_window") or "{}")
        except Exception:
            d["last_window"] = {}
        return d

    def put_window(self, account_id: str, window: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("UPDATE lead_accounts SET last_window=? WHERE account_id=?",
                               (json.dumps(window or {}, ensure_ascii=False), str(account_id)))
            self._conn.commit()

    # 会话上下文（同一入站只回一条）
    def record_inbound(self, account_id: str, chat_key: str, *, user_id: str = "", username: str = "",
                       comment_id: str = "", video_id: str = "", ts: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_ctx(account_id, chat_key, user_id, username, last_comment_id, last_video_id, last_inbound_ts, out_since_inbound) "
                "VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(account_id, chat_key) DO UPDATE SET "
                "user_id=CASE WHEN excluded.user_id<>'' THEN excluded.user_id ELSE chat_ctx.user_id END, "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE chat_ctx.username END, "
                "last_comment_id=excluded.last_comment_id, last_video_id=excluded.last_video_id, "
                "last_inbound_ts=MAX(excluded.last_inbound_ts, chat_ctx.last_inbound_ts), out_since_inbound=0",
                (str(account_id), str(chat_key), str(user_id or ""), str(username or ""), str(comment_id or ""),
                 str(video_id or ""), float(ts or 0)))
            self._conn.commit()

    def ctx(self, account_id: str, chat_key: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM chat_ctx WHERE account_id=? AND chat_key=?",
                                     (str(account_id), str(chat_key))).fetchone()
        return dict(row) if row else {}

    # 出站队列
    def sent_today(self, account_id: str, *, now: Optional[float] = None) -> int:
        t = float(now if now is not None else time.time())
        day0 = t - (t % 86400.0)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM outbound WHERE account_id=? AND status IN ('queued','claimed','sent') AND created_at>=?",
                (str(account_id), day0)).fetchone()
        return int(row["n"] if row else 0)

    def enqueue(self, account_id: str, chat_key: str, text: str, *, ctx: Dict[str, Any], now: Optional[float] = None) -> int:
        t = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbound(account_id, chat_key, text, comment_id, video_id, user_id, username, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,'queued',?)",
                (str(account_id), str(chat_key), str(text), str(ctx.get("last_comment_id") or ""),
                 str(ctx.get("last_video_id") or ""), str(ctx.get("user_id") or ""), str(ctx.get("username") or ""), t))
            self._conn.execute("UPDATE chat_ctx SET out_since_inbound=out_since_inbound+1 WHERE account_id=? AND chat_key=?",
                               (str(account_id), str(chat_key)))
            self._conn.commit()
            return int(cur.lastrowid)

    def claim(self, device_id: str, *, account_id: str = "", limit: int = 10, claim_ttl_sec: float = DEFAULT_CLAIM_TTL_SEC,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
        t = float(now if now is not None else time.time())
        with self._lock:
            # 过期认领回收
            self._conn.execute("UPDATE outbound SET status='queued', claimed_at=0, claimed_by='' WHERE status='claimed' AND claimed_at<?",
                               (t - float(claim_ttl_sec),))
            q = "SELECT * FROM outbound WHERE status='queued'"
            args: List[Any] = []
            if account_id:
                q += " AND account_id=?"
                args.append(str(account_id))
            else:
                q += " AND account_id IN (SELECT account_id FROM lead_accounts WHERE device_id=?)"
                args.append(str(device_id))
            q += " ORDER BY id ASC LIMIT ?"
            args.append(max(1, min(50, int(limit))))
            rows = [dict(r) for r in self._conn.execute(q, args).fetchall()]
            for r in rows:
                self._conn.execute("UPDATE outbound SET status='claimed', claimed_at=?, claimed_by=? WHERE id=?",
                                   (t, str(device_id), r["id"]))
                r.update(status="claimed", claimed_at=t, claimed_by=str(device_id))
            self._conn.commit()
        return rows

    def ack(self, item_id: int, *, ok: bool, external_id: str = "", error: str = "", now: Optional[float] = None
            ) -> Optional[Dict[str, Any]]:
        t = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute("SELECT * FROM outbound WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                return None
            if row["status"] in ("sent", "failed"):
                return dict(row)  # 重复回执幂等
            self._conn.execute(
                "UPDATE outbound SET status=?, acked_at=?, external_id=?, error=? WHERE id=?",
                ("sent" if ok else "failed", t, str(external_id or ""), str(error or "")[:200], int(item_id)))
            if not ok:
                self._conn.execute("UPDATE chat_ctx SET out_since_inbound=MAX(out_since_inbound-1,0) WHERE account_id=? AND chat_key=?",
                                   (row["account_id"], row["chat_key"]))
            self._conn.commit()
            row2 = self._conn.execute("SELECT * FROM outbound WHERE id=?", (int(item_id),)).fetchone()
        return dict(row2) if row2 else None

    def summary(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM outbound GROUP BY status").fetchall()
            acc = self._conn.execute("SELECT COUNT(*) AS n FROM lead_accounts").fetchone()
        out = {"queued": 0, "claimed": 0, "sent": 0, "failed": 0}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        out["accounts"] = int(acc["n"] if acc else 0)
        return out

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_STORE: Optional[TikTokHuokeStateStore] = None
_STORE_LOCK = threading.Lock()


def get_state_store(path: Optional[str] = None) -> TikTokHuokeStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = TikTokHuokeStateStore(path)
        return _STORE


def _reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


# ── 线索进线 ───────────────────────────────────────────────────────────────────────────────
def _lead_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    r = raw if isinstance(raw, dict) else {}
    try:
        intent = float(r.get("intent_score") if r.get("intent_score") is not None else r.get("intent") or 0.0)
    except (TypeError, ValueError):
        intent = 0.0
    try:
        ts = float(r.get("ts") or r.get("create_time") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 1e12:
        ts /= 1000.0
    return {
        "comment_id": str(r.get("comment_id") or r.get("id") or "").strip(),
        "video_id": str(r.get("video_id") or r.get("aweme_id") or "").strip(),
        "user_id": str(r.get("user_id") or r.get("uid") or r.get("username") or "").strip(),
        "username": str(r.get("username") or r.get("unique_id") or "").strip(),
        "name": str(r.get("name") or r.get("nickname") or r.get("username") or "").strip(),
        "text": str(r.get("text") or r.get("comment") or "").strip(),
        "avatar": str(r.get("avatar_url") or r.get("avatar") or ""),
        "lang": str(r.get("lang") or ""),
        "intent": max(0.0, min(1.0, intent)),
        "ts": ts,
    }


async def ingest_leads(payload: Dict[str, Any], *, config: Optional[Dict[str, Any]],
                       state: Optional[TikTokHuokeStateStore] = None, now: Optional[float] = None,
                       emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                       auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None,
                       registry: Any = None) -> Tuple[int, Dict[str, Any]]:
    """``{"device_id", "account_id", "leads":[{comment_id, video_id, user_id, username, name, text, ts, intent_score, lang}]}``
    → 逐条：意向闸 → comment_id 幂等 → 账号登记（personal_rpa）→ emit → 起草钩子。返回 ``(status, 统计)``。"""
    cfg = bridge_cfg(config)
    t_now = float(now if now is not None else time.time())
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    account_id = str(payload.get("account_id") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    leads = payload.get("leads")
    if not account_id or not isinstance(leads, list):
        return 400, {"error": "account_id / leads 必填"}
    st = state or get_state_store(cfg["state_db_path"] or None)
    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    from src.integrations.protocol_bridge import make_message
    try:
        from src.integrations.leadbus_account import register_lead_account
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        register_lead_account(registry, PLATFORM, account_id, label=f"TikTok 真机 {account_id}")
    except Exception:
        logger.debug("[tiktok-huoke] 账号登记跳过（不阻断进线）", exc_info=True)

    stats = {"accepted": 0, "dup": 0, "low_intent": 0, "invalid": 0, "drafted": 0}
    for raw in leads:
        f = _lead_fields(raw)
        if not f["comment_id"] or not f["user_id"] or not f["text"]:
            stats["invalid"] += 1
            continue
        if f["intent"] < cfg["min_intent"]:
            stats["low_intent"] += 1
            continue
        if st.seen(f"cmt:{account_id}:{f['comment_id']}", now=t_now):
            stats["dup"] += 1
            continue
        chat_key = chat_key_for(f["user_id"])
        ts = f["ts"] or t_now
        st.record_inbound(account_id, chat_key, user_id=f["user_id"], username=f["username"],
                          comment_id=f["comment_id"], video_id=f["video_id"], ts=ts)
        source = {
            "source": SOURCE, "kind": KIND_COMMENT, "mode": "personal_rpa", "reply_engine": "chengjie",
            "account_id": account_id, "device_id": device_id, "comment_id": f["comment_id"], "video_id": f["video_id"],
            "intent_score": f["intent"], "lang": f["lang"],
        }
        msg = make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key, text=f["text"], name=f["name"],
                           ts=ts, msg_id=f"cmt:{f['comment_id']}", direction="in", username=f["username"],
                           avatar_url=f["avatar"], source=source)
        emit(msg)
        stats["accepted"] += 1
        try:
            await auto_reply(msg)
            stats["drafted"] += 1
        except Exception:
            logger.debug("[tiktok-huoke] 起草钩子异常", exc_info=True)
    st.touch_account(account_id, device_id, now=t_now, leads=stats["accepted"])
    return 200, {"ok": True, **stats}


# ── 回传（hand-back）队列入口 ──────────────────────────────────────────────────────────────
def enqueue_reply(account_id: str, chat_key: str, text: str, *, config: Optional[Dict[str, Any]],
                  state: Optional[TikTokHuokeStateStore] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """坐席 / 自动链的回复 → 出站队列。闸门：账号必须经本桥进过线、chat_key 是评论会话、channel_policy 文本规则、
    评论 150 字、同一入站只回一条、每号日上限。返回 ``{"ok", "item_id"}`` 或 ``{"ok": False, "reason", "status"}``。"""
    cfg = bridge_cfg(config)
    t_now = float(now if now is not None else time.time())
    st = state or get_state_store(cfg["state_db_path"] or None)
    if not is_bridge_chat(chat_key) or not st.account(account_id):
        return {"ok": False, "reason": REASON_NOT_BRIDGED, "status": 400}
    txt = str(text or "")
    try:
        from src.inbox.channel_policy import text_block_reason
        blk = text_block_reason(PLATFORM, txt, first_message=False)
    except Exception:
        blk = ""
    if blk:
        return {"ok": False, "reason": blk, "status": 409}
    if len(txt) > cfg["max_reply_len"]:
        return {"ok": False, "reason": f"{REASON_TOO_LONG}:{len(txt)}>{cfg['max_reply_len']}", "status": 409}
    ctx = st.ctx(account_id, chat_key)
    if not ctx or float(ctx.get("last_inbound_ts") or 0) <= 0:
        return {"ok": False, "reason": REASON_NOT_BRIDGED, "status": 400}
    if int(ctx.get("out_since_inbound") or 0) >= 1:
        return {"ok": False, "reason": REASON_ONE_REPLY, "status": 409}
    if st.sent_today(account_id, now=t_now) >= cfg["daily_cap"]:
        return {"ok": False, "reason": f"{REASON_DAILY_CAP}:{cfg['daily_cap']}", "status": 429}
    item_id = st.enqueue(account_id, chat_key, txt, ctx=ctx, now=t_now)
    return {"ok": True, "item_id": item_id, "queued": True, "handback": True}


def _echo_sent(item: Dict[str, Any], *, emit: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
    if emit is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit
        emit = _emit
    from src.integrations.protocol_bridge import make_message
    emit(make_message(platform=PLATFORM, account_id=str(item["account_id"]), chat_key=str(item["chat_key"]),
                      text=str(item["text"]), ts=float(item.get("acked_at") or time.time()),
                      msg_id=str(item.get("external_id") or f"hb:{item['id']}"), direction="out",
                      source={"source": SOURCE, "kind": KIND_COMMENT, "mode": "personal_rpa", "handback_item": int(item["id"]),
                              "comment_id": str(item.get("comment_id") or ""), "video_id": str(item.get("video_id") or ""),
                              "echo": True}))


def ack_handback(payload: Dict[str, Any], *, config: Optional[Dict[str, Any]], state: Optional[TikTokHuokeStateStore] = None,
                 now: Optional[float] = None, emit: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Tuple[int, Dict[str, Any]]:
    """``{"item_id", "ok", "external_id"?, "error"?, "window"?}`` → sent（out 回显）/ failed（留痕）；``window`` 归一到 DY 名。"""
    cfg = bridge_cfg(config)
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        return 400, {"error": "item_id 必填"}
    st = state or get_state_store(cfg["state_db_path"] or None)
    ok = bool(payload.get("ok"))
    before = st.summary()
    item = st.ack(item_id, ok=ok, external_id=str(payload.get("external_id") or ""), error=str(payload.get("error") or ""), now=now)
    if item is None:
        return 404, {"error": "unknown_item"}
    already = st.summary() == before
    window: Dict[str, Any] = {}
    if isinstance(payload.get("window"), dict):
        from src.inbox.channel_policy import normalize_window_fields
        window = normalize_window_fields(payload.get("window"))
        st.put_window(str(item["account_id"]), window)
    if ok and not already:
        try:
            _echo_sent(item, emit=emit)
        except Exception:
            logger.debug("[tiktok-huoke] 回显失败", exc_info=True)
    return 200, {"ok": True, "status": item["status"], "dup": already, "window": window}


# ── 收件箱适配器（send 回落路径）─────────────────────────────────────────────────────────
class TikTokHuokeAdapter:
    """``send_via_adapters`` 回落到此：只接管经本桥进过线的评论会话 → 出站队列；其余维持旧 400（「不支持的平台」）。
    ``collect_chats`` 不出数（会话已由 emit 落 inbox_store，聚合按 store 读）；``status`` 报队列摘要。"""

    platform = PLATFORM
    _tiktok_huoke_bridge = True

    def __init__(self, config_getter: Callable[[], Dict[str, Any]]) -> None:
        self._cfg = config_getter

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        return []

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        try:
            cfg = self._cfg() or {}
            if not bridge_enabled(cfg):
                return {}
            return {"tiktok_huoke": {"ok": True, **get_state_store(bridge_cfg(cfg)["state_db_path"] or None).summary()}}
        except Exception:
            return {}

    async def send(self, request: Any, account_id: str, chat_key: str, text: str) -> Dict[str, Any]:
        from src.inbox.channel_adapters import ChannelSendError
        cfg = self._cfg() or {}
        if not bridge_enabled(cfg) or not is_bridge_chat(chat_key):
            raise ChannelSendError(400, f"不支持的平台: {PLATFORM}")
        res = enqueue_reply(account_id, chat_key, text, config=cfg)
        if not res.get("ok"):
            reason = str(res.get("reason") or REASON_NOT_BRIDGED)
            if reason == REASON_NOT_BRIDGED:
                raise ChannelSendError(400, f"不支持的平台: {PLATFORM}")
            raise ChannelSendError(int(res.get("status") or 409), f"tiktok huoke handback blocked: {reason}", reason_code=reason)
        return {"ok": True, "queued": True, "handback": True, "item_id": res["item_id"], "message_id": ""}


def install_adapter(adapters: List[Any], config_getter: Callable[[], Dict[str, Any]]) -> bool:
    """幂等追加适配器到收件箱注册表（``unified_inbox_aggregate._INBOX_ADAPTERS``）。"""
    if any(getattr(a, "_tiktok_huoke_bridge", False) for a in adapters):
        return False
    adapters.append(TikTokHuokeAdapter(config_getter))
    return True


# ── 路由 ───────────────────────────────────────────────────────────────────────────────────
def register_tiktok_huoke_routes(app: Any, config_manager: Any) -> bool:
    """``tiktok.huoke_bridge.enabled`` 才挂：leads 进线 / handback 认领 / ack 回执 / status，全程 ``api_auth``；并追加适配器。"""
    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    if not bridge_enabled(_cfg()) or JSONResponse is None:
        return False
    if any(getattr(r, "path", "") == LEADS_ROUTE for r in getattr(app, "routes", [])):
        return True
    api_auth = getattr(getattr(app, "state", None), "api_auth", None)
    deps = [Depends(api_auth)] if (api_auth is not None and Depends is not None) else []

    @app.post(LEADS_ROUTE, dependencies=deps)
    async def tiktok_huoke_leads(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = await ingest_leads(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.get(HANDBACK_ROUTE, dependencies=deps)
    async def tiktok_huoke_handback(request: Request, device_id: str = "", account_id: str = "", limit: int = 10):
        if not device_id and not account_id:
            return JSONResponse({"error": "device_id 或 account_id 必填"}, status_code=400)
        cfg = bridge_cfg(_cfg())
        st = get_state_store(cfg["state_db_path"] or None)
        items = st.claim(device_id, account_id=account_id, limit=limit, claim_ttl_sec=cfg["claim_ttl_sec"])
        return {"ok": True, "items": [{k: v for k, v in it.items() if k != "error"} for it in items],
                "policy": {"max_reply_len": cfg["max_reply_len"], "daily_cap": cfg["daily_cap"]}}

    @app.post(HANDBACK_ACK_ROUTE, dependencies=deps)
    async def tiktok_huoke_handback_ack(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = ack_handback(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.get(STATUS_ROUTE, dependencies=deps)
    async def tiktok_huoke_status(request: Request):
        cfg = bridge_cfg(_cfg())
        return {"ok": True, "enabled": True, **get_state_store(cfg["state_db_path"] or None).summary(),
                "policy": {"min_intent": cfg["min_intent"], "max_reply_len": cfg["max_reply_len"], "daily_cap": cfg["daily_cap"]}}

    try:
        from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
        install_adapter(_INBOX_ADAPTERS, _cfg)
    except Exception:
        logger.debug("[tiktok-huoke] 适配器追加跳过", exc_info=True)
    logger.info("[tiktok-huoke] 桥已挂载 %s / %s / %s / %s", LEADS_ROUTE, HANDBACK_ROUTE, HANDBACK_ACK_ROUTE, STATUS_ROUTE)
    return True


__all__ = [
    "PLATFORM", "SOURCE", "KIND_COMMENT", "CHAT_PREFIX", "COMMENT_MAX_LEN",
    "LEADS_ROUTE", "HANDBACK_ROUTE", "HANDBACK_ACK_ROUTE", "STATUS_ROUTE",
    "REASON_ONE_REPLY", "REASON_DAILY_CAP", "REASON_TOO_LONG", "REASON_NOT_BRIDGED",
    "bridge_cfg", "bridge_enabled", "chat_key_for", "is_bridge_chat",
    "TikTokHuokeStateStore", "get_state_store", "ingest_leads", "enqueue_reply", "ack_handback",
    "TikTokHuokeAdapter", "install_adapter", "register_tiktok_huoke_routes",
]
