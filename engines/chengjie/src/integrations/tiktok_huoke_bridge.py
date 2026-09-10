"""TikTok × huoke（获客真机）桥——脑手分离 opt-in（TikTok 线 E 段 2026-09-10 评论桥；TK-3 ①-B 同日加 **私信通道**）。

huoke 是兄弟仓的真机自动化产品（TikTok App 养号 / 评论区巡检 / 私信收发）。**TikTok 没有任何官方接口给个人号收发私信**，
所以「个人号私信」只能走真机——本桥让 huoke 只做「手」（读收件箱 / 真机发送），智聊做「脑」（收件箱 / 起草 / 审核 / 闸门）。
两条通道共用一套账号（``account_id`` = huoke 设备上登录的 TikTok 账号，mode ``personal_rpa``，**不在** ``ORCHESTRATED_MODES``
→ 本机编排器绝不接管）、一套回传队列、一套认领 / 回执协议：

- ``kind=comment``（E 段）：评论区高意向线索 ``POST /api/tiktok/huoke/leads`` → 会话键 ``tiktok:comment:<user_id>``；回评论公开可见。
- ``kind=dm``（TK-3）：私信 ``POST /api/tiktok/huoke/dm`` → 会话键 ``tiktok:user:<username>``（与 TK-1 官方私信 worker 同键——
  同一买家从官方号 / 个人号进来对得上人）；huoke 自己发的首触也按 ``direction=out`` 回传进来做上下文。

**起草**：落库后调 ``protocol_bridge.maybe_auto_reply`` → 既有 reply hook（``DraftService`` / 人设产线）——不另起大脑（D-TK-6 / D-P2）。

**回传（hand-back）**：坐席审过 / 自动放行的回复经 ``send_via_adapters`` 回落到 ``TikTokHuokeAdapter``（编排器不拥有
personal_rpa 账号 → 必然回落）→ ``enqueue_reply`` 闸门 → 队列；huoke ``GET /api/tiktok/huoke/handback`` 认领 → 真机发送 →
``POST /api/tiktok/huoke/handback/ack``。入队即写一条 out 镜像（无勾＝待真机），回执成功 → 该镜像 ``status=sent``；
失败 → ``record_failed_outbound`` 留痕（前端「发送失败 + 一键重发」）。

**私信闸门（D-P3 / D-P4）**：智聊只接管**对方开口之后**的对话——会话无对方入站（huoke 首触后对方未回）→ 一条都不发
（``policy_message_request_pending``）；对方 72h 未回 → 自动链停、人工只可再补 1 条（``policy_peer_silent``）；每号日上限按
**账号时区**切日（``policy_daily_cap``）；``work_hours_gate`` 夜间静默只拦自动链（``policy_quiet_hours``）；文本经
``channel_policy.text_block_reason("tiktok", mode="personal_rpa")``（首条禁链、≤1000 字）。评论闸门见 E 段（150 字、一入站一条）。

**设备与健康**：``POST /api/tiktok/huoke/devices`` 绑定设备 ↔ 账号（时区、日上限），认领不再依赖「先进线」；huoke 每次认领 /
进线 / 绑定都是一次心跳 → 15 分钟没心跳 ``reconnecting``、2 小时 ``expired``（账号 chip 变黄「真机离线」），写进
``platform_session_health``；``sweep_health()`` 供巡检定时扫。

回复窗字段：huoke 回执可带设备侧窗口状态（可能仍用 TK-1 规划旧名）→ ``channel_policy.normalize_window_fields`` 归一后存。
开关 ``tiktok.huoke_bridge.enabled``（默认 false）：不挂路由、不追加适配器、不建状态库。全程 ``api_auth``（Bearer）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
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
MODE = "personal_rpa"
SOURCE = "huoke"
KIND_COMMENT = "comment"
KIND_DM = "dm"
CHAT_PREFIX = f"{PLATFORM}:{KIND_COMMENT}:"
DM_CHAT_PREFIX = f"{PLATFORM}:user:"

LEADS_ROUTE = "/api/tiktok/huoke/leads"
DM_ROUTE = "/api/tiktok/huoke/dm"
DEVICES_ROUTE = "/api/tiktok/huoke/devices"
HANDBACK_ROUTE = "/api/tiktok/huoke/handback"
HANDBACK_ACK_ROUTE = "/api/tiktok/huoke/handback/ack"
HANDBACK_PENDING_ROUTE = "/api/tiktok/huoke/handback/pending"  # TK-3：只看不认领，huoke 轮询器决定是否唤醒真机
STATUS_ROUTE = "/api/tiktok/huoke/status"

COMMENT_MAX_LEN = 150          # TikTok 评论字数上限（平台事实，非策略参数）
DM_MAX_LEN = 1000
DEFAULT_MIN_INTENT = 0.6
DEFAULT_DAILY_CAP = 20         # 评论：TK-1 §8 新号 10–20 起
DEFAULT_DM_DAILY_CAP = 15      # 私信：与 huoke config/apps/tiktok.yaml send_dm.daily 同值（双保险以严者为准）
DEFAULT_PEER_SILENT_HOURS = 72.0
DEFAULT_MIN_GAP_SEC = 20.0     # 同一账号两条私信认领的最小间隔（随机大间隔由真机侧加）
DEFAULT_CLAIM_TTL_SEC = 600.0  # huoke 认领后 10 分钟没回执 → 可被重新认领
HEARTBEAT_RECONNECTING_SEC = 15 * 60.0
HEARTBEAT_EXPIRED_SEC = 2 * 3600.0
SEEN_TTL_SEC = 7 * 24 * 3600.0

REASON_LOW_INTENT = "low_intent"
REASON_ONE_REPLY = "policy_one_reply_per_inbound"
REASON_DAILY_CAP = "policy_daily_cap"
REASON_TOO_LONG = "policy_comment_too_long"
REASON_NOT_BRIDGED = "not_bridged"
REASON_REQUEST_PENDING = "policy_message_request_pending"
REASON_PEER_SILENT = "policy_peer_silent"
REASON_QUIET_HOURS = "policy_quiet_hours"
REASON_DEVICE_OFFLINE = "device_offline"


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
        "dm_daily_cap": max(1, int(_f("dm_daily_cap", DEFAULT_DM_DAILY_CAP))),
        "max_reply_len": max(1, min(COMMENT_MAX_LEN, int(_f("max_reply_len", COMMENT_MAX_LEN)))),
        "dm_max_len": max(1, min(DM_MAX_LEN, int(_f("dm_max_len", DM_MAX_LEN)))),
        "peer_silent_hours": max(1.0, _f("peer_silent_hours", DEFAULT_PEER_SILENT_HOURS)),
        "min_gap_sec": max(0.0, _f("min_gap_sec", DEFAULT_MIN_GAP_SEC)),
        "claim_ttl_sec": max(30.0, _f("claim_ttl_sec", DEFAULT_CLAIM_TTL_SEC)),
        "quiet_hours_block_auto": bool(blk.get("quiet_hours_block_auto", True)),
        "state_db_path": str(blk.get("state_db_path") or ""),
    }


def bridge_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return bridge_cfg(config)["enabled"]


def chat_key_for(user_id: str) -> str:
    return f"{CHAT_PREFIX}{str(user_id or '').strip()}"


def dm_chat_key_for(username: str) -> str:
    return f"{DM_CHAT_PREFIX}{str(username or '').strip().lstrip('@')}"


def is_bridge_chat(chat_key: str) -> bool:
    return str(chat_key or "").startswith(CHAT_PREFIX)


def is_dm_chat(chat_key: str) -> bool:
    return str(chat_key or "").startswith(DM_CHAT_PREFIX)


def kind_of_chat(chat_key: str) -> str:
    if is_bridge_chat(chat_key):
        return KIND_COMMENT
    if is_dm_chat(chat_key):
        return KIND_DM
    return ""


def _local_day_start(ts: float, tz_name: str) -> float:
    """账号时区的当日零点（epoch 秒）；坏时区 → 本机时区。"""
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    try:
        dt = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts).astimezone()
        day0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return day0.timestamp()
    except Exception:
        return ts - (ts % 86400.0)


def heartbeat_state(last_seen_ts: float, now: Optional[float] = None) -> str:
    """真机心跳 → 会话健康态：``authorized`` / ``reconnecting``（>15 分钟）/ ``expired``（>2 小时）；从未心跳 → ``unknown``。"""
    if not last_seen_ts:
        return "unknown"
    gap = float(now if now is not None else time.time()) - float(last_seen_ts)
    if gap > HEARTBEAT_EXPIRED_SEC:
        return "expired"
    if gap > HEARTBEAT_RECONNECTING_SEC:
        return "reconnecting"
    return "authorized"


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
# TK-3 增列（幂等 ALTER；老库升级零停机）
_MIGRATIONS = (
    ("lead_accounts", "username", "TEXT NOT NULL DEFAULT ''"),
    ("lead_accounts", "timezone", "TEXT NOT NULL DEFAULT ''"),
    ("lead_accounts", "last_seen_ts", "REAL NOT NULL DEFAULT 0"),
    ("lead_accounts", "dm_daily_cap", "INTEGER NOT NULL DEFAULT 0"),
    ("lead_accounts", "health", "TEXT NOT NULL DEFAULT ''"),
    ("chat_ctx", "kind", "TEXT NOT NULL DEFAULT 'comment'"),
    ("chat_ctx", "is_mutual", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_ctx", "is_follower", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_ctx", "thread_type", "TEXT NOT NULL DEFAULT ''"),
    ("chat_ctx", "last_out_ts", "REAL NOT NULL DEFAULT 0"),
    ("chat_ctx", "peer_name", "TEXT NOT NULL DEFAULT ''"),
    ("outbound", "kind", "TEXT NOT NULL DEFAULT 'comment'"),
    ("outbound", "origin", "TEXT NOT NULL DEFAULT 'auto'"),
)


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
        for table, col, decl in _MIGRATIONS:
            try:
                cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:
                logger.debug("[tiktok-huoke] 增列跳过 %s.%s", table, col, exc_info=True)
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

    # 账号 / 设备
    def touch_account(self, account_id: str, device_id: str = "", *, now: Optional[float] = None, leads: int = 0,
                      username: str = "", timezone: str = "", dm_daily_cap: Optional[int] = None,
                      heartbeat: bool = True) -> None:
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO lead_accounts(account_id, device_id, first_ts, last_ts, leads_total, username, timezone, last_seen_ts, dm_daily_cap) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET last_ts=excluded.last_ts, leads_total=lead_accounts.leads_total+excluded.leads_total, "
                "device_id=CASE WHEN excluded.device_id<>'' THEN excluded.device_id ELSE lead_accounts.device_id END, "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE lead_accounts.username END, "
                "timezone=CASE WHEN excluded.timezone<>'' THEN excluded.timezone ELSE lead_accounts.timezone END, "
                "last_seen_ts=MAX(excluded.last_seen_ts, lead_accounts.last_seen_ts), "
                "dm_daily_cap=CASE WHEN excluded.dm_daily_cap>0 THEN excluded.dm_daily_cap ELSE lead_accounts.dm_daily_cap END",
                (str(account_id), str(device_id or ""), t, t, int(leads), str(username or ""), str(timezone or ""),
                 t if heartbeat else 0.0, int(dm_daily_cap or 0)))
            self._conn.commit()

    def heartbeat(self, device_id: str, *, now: Optional[float] = None) -> List[str]:
        """设备所有账号记一次心跳；返回账号列表。"""
        t = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute("SELECT account_id FROM lead_accounts WHERE device_id=?", (str(device_id),)).fetchall()
            self._conn.execute("UPDATE lead_accounts SET last_seen_ts=MAX(last_seen_ts, ?) WHERE device_id=?", (t, str(device_id)))
            self._conn.commit()
        return [str(r["account_id"]) for r in rows]

    def set_health(self, account_id: str, health: str) -> str:
        """写健康态，返回上一态（供「变化才上报」）。"""
        with self._lock:
            row = self._conn.execute("SELECT health FROM lead_accounts WHERE account_id=?", (str(account_id),)).fetchone()
            prev = str(row["health"]) if row else ""
            self._conn.execute("UPDATE lead_accounts SET health=? WHERE account_id=?", (str(health), str(account_id)))
            self._conn.commit()
        return prev

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

    def accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lead_accounts ORDER BY account_id").fetchall()
        return [dict(r) for r in rows]

    def put_window(self, account_id: str, window: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("UPDATE lead_accounts SET last_window=? WHERE account_id=?",
                               (json.dumps(window or {}, ensure_ascii=False), str(account_id)))
            self._conn.commit()

    # 会话上下文
    def record_inbound(self, account_id: str, chat_key: str, *, user_id: str = "", username: str = "",
                       comment_id: str = "", video_id: str = "", ts: float = 0.0, kind: str = KIND_COMMENT,
                       is_mutual: Optional[bool] = None, is_follower: Optional[bool] = None, thread_type: str = "",
                       peer_name: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_ctx(account_id, chat_key, user_id, username, last_comment_id, last_video_id, last_inbound_ts, "
                "out_since_inbound, kind, is_mutual, is_follower, thread_type, peer_name) VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?) "
                "ON CONFLICT(account_id, chat_key) DO UPDATE SET "
                "user_id=CASE WHEN excluded.user_id<>'' THEN excluded.user_id ELSE chat_ctx.user_id END, "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE chat_ctx.username END, "
                "peer_name=CASE WHEN excluded.peer_name<>'' THEN excluded.peer_name ELSE chat_ctx.peer_name END, "
                "last_comment_id=CASE WHEN excluded.last_comment_id<>'' THEN excluded.last_comment_id ELSE chat_ctx.last_comment_id END, "
                "last_video_id=CASE WHEN excluded.last_video_id<>'' THEN excluded.last_video_id ELSE chat_ctx.last_video_id END, "
                "last_inbound_ts=MAX(excluded.last_inbound_ts, chat_ctx.last_inbound_ts), out_since_inbound=0, kind=excluded.kind, "
                "is_mutual=CASE WHEN ?>=0 THEN excluded.is_mutual ELSE chat_ctx.is_mutual END, "
                "is_follower=CASE WHEN ?>=0 THEN excluded.is_follower ELSE chat_ctx.is_follower END, "
                "thread_type=CASE WHEN excluded.thread_type<>'' THEN excluded.thread_type ELSE chat_ctx.thread_type END",
                (str(account_id), str(chat_key), str(user_id or ""), str(username or ""), str(comment_id or ""),
                 str(video_id or ""), float(ts or 0), str(kind), int(bool(is_mutual)), int(bool(is_follower)), str(thread_type or ""),
                 str(peer_name or ""), 1 if is_mutual is not None else -1, 1 if is_follower is not None else -1))
            self._conn.commit()

    def record_outbound_echo(self, account_id: str, chat_key: str, *, ts: float, kind: str = KIND_DM, username: str = "",
                             is_mutual: Optional[bool] = None, is_follower: Optional[bool] = None) -> None:
        """huoke 自己发的首触 / 历史出站回传进来：只建上下文，不算作我方回复配额。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_ctx(account_id, chat_key, username, kind, last_out_ts, is_mutual, is_follower) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id, chat_key) DO UPDATE SET last_out_ts=MAX(excluded.last_out_ts, chat_ctx.last_out_ts), "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE chat_ctx.username END, "
                "is_mutual=CASE WHEN ?>=0 THEN excluded.is_mutual ELSE chat_ctx.is_mutual END, "
                "is_follower=CASE WHEN ?>=0 THEN excluded.is_follower ELSE chat_ctx.is_follower END",
                (str(account_id), str(chat_key), str(username or ""), str(kind), float(ts or 0), int(bool(is_mutual)),
                 int(bool(is_follower)), 1 if is_mutual is not None else -1, 1 if is_follower is not None else -1))
            self._conn.commit()

    def ctx(self, account_id: str, chat_key: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM chat_ctx WHERE account_id=? AND chat_key=?",
                                     (str(account_id), str(chat_key))).fetchone()
        return dict(row) if row else {}

    # 出站队列
    def sent_today(self, account_id: str, *, now: Optional[float] = None, kind: str = "", tz_name: str = "") -> int:
        t = float(now if now is not None else time.time())
        day0 = _local_day_start(t, tz_name)
        q = "SELECT COUNT(*) AS n FROM outbound WHERE account_id=? AND status IN ('queued','claimed','sent') AND created_at>=?"
        args: List[Any] = [str(account_id), day0]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return int(row["n"] if row else 0)

    def last_sent_ts(self, account_id: str, kind: str = "") -> float:
        q = "SELECT MAX(acked_at) AS t FROM outbound WHERE account_id=? AND status='sent'"
        args: List[Any] = [str(account_id)]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return float(row["t"] or 0) if row else 0.0

    def enqueue(self, account_id: str, chat_key: str, text: str, *, ctx: Dict[str, Any], now: Optional[float] = None,
                kind: str = KIND_COMMENT, origin: str = "auto") -> int:
        t = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbound(account_id, chat_key, text, comment_id, video_id, user_id, username, status, created_at, kind, origin) "
                "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                (str(account_id), str(chat_key), str(text), str(ctx.get("last_comment_id") or ""),
                 str(ctx.get("last_video_id") or ""), str(ctx.get("user_id") or ""), str(ctx.get("username") or ""), t,
                 str(kind), str(origin or "auto")))
            self._conn.execute("UPDATE chat_ctx SET out_since_inbound=out_since_inbound+1 WHERE account_id=? AND chat_key=?",
                               (str(account_id), str(chat_key)))
            self._conn.commit()
            return int(cur.lastrowid)

    def claim(self, device_id: str, *, account_id: str = "", limit: int = 10, claim_ttl_sec: float = DEFAULT_CLAIM_TTL_SEC,
              now: Optional[float] = None, min_gap_sec: float = 0.0) -> List[Dict[str, Any]]:
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
            picked: List[Dict[str, Any]] = []
            # 私信：同一账号同时只允许 1 条在途（claimed 未回执的算在途）
            dm_seen_acc: set = {str(r["account_id"]) for r in self._conn.execute(
                "SELECT DISTINCT account_id FROM outbound WHERE status='claimed' AND kind='dm'").fetchall()}
            for r in rows:
                if r.get("kind") == KIND_DM:
                    # 私信：同一账号一次只认领 1 条，且与上一条送达间隔 ≥ min_gap（节奏；真机侧再加随机大间隔）
                    if r["account_id"] in dm_seen_acc:
                        continue
                    if min_gap_sec > 0:
                        last = self._conn.execute("SELECT MAX(acked_at) AS t FROM outbound WHERE account_id=? AND status='sent' AND kind='dm'",
                                                  (r["account_id"],)).fetchone()
                        if last and float(last["t"] or 0) > t - float(min_gap_sec):
                            continue
                    dm_seen_acc.add(r["account_id"])
                self._conn.execute("UPDATE outbound SET status='claimed', claimed_at=?, claimed_by=? WHERE id=?",
                                   (t, str(device_id), r["id"]))
                r.update(status="claimed", claimed_at=t, claimed_by=str(device_id))
                picked.append(r)
            self._conn.commit()
        return picked

    def pending(self, device_id: str = "", *, account_id: str = "", now: Optional[float] = None,
                claim_ttl_sec: float = DEFAULT_CLAIM_TTL_SEC) -> Dict[str, Any]:
        """TK-3 只看不认领：待发 / 在途条数（先回收过期认领，数字与 ``claim`` 视角一致）。

        huoke 轮询器据此决定要不要唤醒真机——队列空就不建设备任务、不占设备锁。
        """
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute("UPDATE outbound SET status='queued', claimed_at=0, claimed_by='' WHERE status='claimed' AND claimed_at<?",
                               (t - float(claim_ttl_sec),))
            q = "SELECT status, kind, COUNT(*) AS n, MIN(created_at) AS oldest FROM outbound WHERE status IN ('queued','claimed')"
            args: List[Any] = []
            if account_id:
                q += " AND account_id=?"
                args.append(str(account_id))
            elif device_id:
                q += " AND account_id IN (SELECT account_id FROM lead_accounts WHERE device_id=?)"
                args.append(str(device_id))
            q += " GROUP BY status, kind"
            rows = [dict(r) for r in self._conn.execute(q, args).fetchall()]
            self._conn.commit()
        out: Dict[str, Any] = {"queued": 0, "claimed": 0, "queued_dm": 0, "queued_comment": 0, "oldest_wait_sec": 0}
        for r in rows:
            n = int(r["n"] or 0)
            out[str(r["status"])] = out.get(str(r["status"]), 0) + n
            if r["status"] == "queued":
                out["queued_dm" if r["kind"] == KIND_DM else "queued_comment"] += n
                wait = t - float(r["oldest"] or t)
                out["oldest_wait_sec"] = max(out["oldest_wait_sec"], int(wait))
        return out

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
            if ok:
                self._conn.execute("UPDATE chat_ctx SET last_out_ts=MAX(last_out_ts, ?) WHERE account_id=? AND chat_key=?",
                                   (t, row["account_id"], row["chat_key"]))
            else:
                self._conn.execute("UPDATE chat_ctx SET out_since_inbound=MAX(out_since_inbound-1,0) WHERE account_id=? AND chat_key=?",
                                   (row["account_id"], row["chat_key"]))
            self._conn.commit()
            row2 = self._conn.execute("SELECT * FROM outbound WHERE id=?", (int(item_id),)).fetchone()
        return dict(row2) if row2 else None

    def item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM outbound WHERE id=?", (int(item_id),)).fetchone()
        return dict(row) if row else None

    def summary(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM outbound GROUP BY status").fetchall()
            acc = self._conn.execute("SELECT COUNT(*) AS n FROM lead_accounts").fetchone()
            kinds = self._conn.execute("SELECT kind, COUNT(*) AS n FROM outbound WHERE status IN ('queued','claimed') GROUP BY kind").fetchall()
            oldest = self._conn.execute("SELECT MIN(created_at) AS t FROM outbound WHERE status='queued'").fetchone()
        out = {"queued": 0, "claimed": 0, "sent": 0, "failed": 0}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        out["accounts"] = int(acc["n"] if acc else 0)
        for r in kinds:
            out[f"pending_{r['kind']}"] = int(r["n"])
        out["oldest_queued_ts"] = int(float(oldest["t"] or 0)) if oldest else 0
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


# ── 健康（真机心跳 → 会话健康表）────────────────────────────────────────────────────────────
def report_health(st: TikTokHuokeStateStore, account_id: str, *, now: Optional[float] = None,
                  health_sink: Any = None) -> str:
    """按心跳算健康态，变化才写 ``platform_session_health``；返回当前态。"""
    acc = st.account(account_id)
    if not acc:
        return "unknown"
    state = heartbeat_state(float(acc.get("last_seen_ts") or 0), now)
    if state == "unknown":
        return state
    prev = st.set_health(account_id, state)
    if prev == state:
        return state
    try:
        h = health_sink
        if h is None:
            from src.integrations.platform_session_health import get_platform_session_health
            h = get_platform_session_health()
        if state == "authorized":
            h.record(PLATFORM, account_id, "authorized")
        elif state == "reconnecting":
            h.record(PLATFORM, account_id, "reconnecting", detail="[rc:other] TikTok 真机 15 分钟未心跳（huoke 未认领）")
        else:
            h.record(PLATFORM, account_id, "expired", detail="[rc:other] TikTok 真机离线超 2 小时，请检查 huoke 设备")
    except Exception:
        logger.debug("[tiktok-huoke] 会话健康上报失败", exc_info=True)
    return state


def sweep_health(*, config: Optional[Dict[str, Any]] = None, state: Optional[TikTokHuokeStateStore] = None,
                 now: Optional[float] = None, health_sink: Any = None) -> Dict[str, str]:
    """巡检用：扫所有桥账号的心跳态（桥未开 → ``{}``）。"""
    if not bridge_enabled(config) and state is None:
        return {}
    st = state or get_state_store(bridge_cfg(config)["state_db_path"] or None)
    return {a["account_id"]: report_health(st, a["account_id"], now=now, health_sink=health_sink) for a in st.accounts()}


# ── 设备绑定 ───────────────────────────────────────────────────────────────────────────────
def bind_device(payload: Dict[str, Any], *, config: Optional[Dict[str, Any]], state: Optional[TikTokHuokeStateStore] = None,
                now: Optional[float] = None, registry: Any = None, health_sink: Any = None) -> Tuple[int, Dict[str, Any]]:
    """``{"device_id", "account_id", "username"?, "timezone"?, "dm_daily_cap"?}`` → 设备 ↔ 账号绑定 + personal_rpa 登记 + 心跳。"""
    cfg = bridge_cfg(config)
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    account_id = str(payload.get("account_id") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    if not account_id or not device_id:
        return 400, {"error": "device_id / account_id 必填"}
    tz = str(payload.get("timezone") or "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            return 400, {"error": f"bad_timezone:{tz}"}
    try:
        cap = int(payload.get("dm_daily_cap") or 0)
    except (TypeError, ValueError):
        cap = 0
    st = state or get_state_store(cfg["state_db_path"] or None)
    st.touch_account(account_id, device_id, now=now, username=str(payload.get("username") or ""), timezone=tz,
                     dm_daily_cap=min(cap, cfg["dm_daily_cap"]) if cap > 0 else None)
    _register_account(account_id, registry)
    health = report_health(st, account_id, now=now, health_sink=health_sink)
    acc = st.account(account_id)
    return 200, {"ok": True, "account_id": account_id, "device_id": device_id, "timezone": acc.get("timezone") or "",
                 "dm_daily_cap": int(acc.get("dm_daily_cap") or 0) or cfg["dm_daily_cap"], "health": health}


def _register_account(account_id: str, registry: Any) -> None:
    try:
        from src.integrations.leadbus_account import register_lead_account
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        register_lead_account(registry, PLATFORM, account_id, label=f"TikTok 真机 {account_id}")
    except Exception:
        logger.debug("[tiktok-huoke] 账号登记跳过（不阻断进线）", exc_info=True)


# ── 线索进线（评论）───────────────────────────────────────────────────────────────────────
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
    _register_account(account_id, registry)

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
                          comment_id=f["comment_id"], video_id=f["video_id"], ts=ts, kind=KIND_COMMENT, peer_name=f["name"])
        source = {
            "source": SOURCE, "kind": KIND_COMMENT, "mode": MODE, "reply_engine": "chengjie",
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
    report_health(st, account_id, now=t_now)
    return 200, {"ok": True, **stats}


# ── 私信进线（TK-3）────────────────────────────────────────────────────────────────────────
def _dm_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    r = raw if isinstance(raw, dict) else {}
    try:
        ts = float(r.get("ts") or r.get("create_time") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 1e12:
        ts /= 1000.0
    rel = r.get("relation") if isinstance(r.get("relation"), dict) else {}

    def _opt_bool(*keys: str) -> Optional[bool]:
        for k in keys:
            for d in (rel, r):
                if k in d and d[k] is not None:
                    return bool(d[k])
        return None

    direction = str(r.get("direction") or "in").strip().lower()
    return {
        "msg_id": str(r.get("msg_id") or r.get("message_id") or r.get("id") or "").strip(),
        "peer_username": str(r.get("peer_username") or r.get("username") or r.get("contact") or "").strip().lstrip("@"),
        "peer_name": str(r.get("peer_name") or r.get("name") or r.get("nickname") or "").strip(),
        "text": str(r.get("text") or r.get("message") or "").strip(),
        "media_type": str(r.get("media_type") or "").strip().lower(),
        "avatar": str(r.get("avatar_url") or r.get("avatar") or ""),
        "lang": str(r.get("lang") or ""),
        "direction": "out" if direction in ("out", "outbound", "sent", "me") else "in",
        "is_mutual": _opt_bool("is_mutual", "mutual"),
        "is_follower": _opt_bool("is_follower", "follower", "follows_me"),
        "thread_type": str(r.get("thread_type") or rel.get("thread_type") or "").strip().lower(),
        "ts": ts,
    }


async def ingest_dm(payload: Dict[str, Any], *, config: Optional[Dict[str, Any]],
                    state: Optional[TikTokHuokeStateStore] = None, now: Optional[float] = None,
                    emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                    auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None,
                    registry: Any = None) -> Tuple[int, Dict[str, Any]]:
    """``{"device_id", "account_id", "username"?, "timezone"?, "messages":[{msg_id, peer_username, peer_name, text, ts,
    direction: in|out, relation:{is_mutual, is_follower}, thread_type: request|chat, media_type?, lang?}]}``
    → 逐条：msg_id 幂等 → 上下文（关系态 / 最近入站 / 出站）→ emit → 入站起草。``direction=out`` 是 huoke 自己发的
    （首触 / 历史），只做回显与上下文，**不**计入我方配额。"""
    cfg = bridge_cfg(config)
    t_now = float(now if now is not None else time.time())
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    account_id = str(payload.get("account_id") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    messages = payload.get("messages")
    if not account_id or not isinstance(messages, list):
        return 400, {"error": "account_id / messages 必填"}
    st = state or get_state_store(cfg["state_db_path"] or None)
    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    from src.integrations.protocol_bridge import make_message
    _register_account(account_id, registry)
    st.touch_account(account_id, device_id, now=t_now, username=str(payload.get("username") or ""),
                     timezone=str(payload.get("timezone") or ""))

    stats = {"accepted": 0, "echo": 0, "dup": 0, "invalid": 0, "drafted": 0}
    for raw in messages:
        f = _dm_fields(raw)
        if not f["msg_id"] or not f["peer_username"] or (not f["text"] and not f["media_type"]):
            stats["invalid"] += 1
            continue
        if st.seen(f"dm:{account_id}:{f['msg_id']}", now=t_now):
            stats["dup"] += 1
            continue
        chat_key = dm_chat_key_for(f["peer_username"])
        ts = f["ts"] or t_now
        incoming = f["direction"] == "in"
        if incoming:
            st.record_inbound(account_id, chat_key, username=f["peer_username"], ts=ts, kind=KIND_DM,
                              is_mutual=f["is_mutual"], is_follower=f["is_follower"], thread_type=f["thread_type"],
                              peer_name=f["peer_name"])
        else:
            st.record_outbound_echo(account_id, chat_key, ts=ts, kind=KIND_DM, username=f["peer_username"],
                                    is_mutual=f["is_mutual"], is_follower=f["is_follower"])
        source: Dict[str, Any] = {
            "source": SOURCE, "kind": KIND_DM, "mode": MODE, "reply_engine": "chengjie",
            "account_id": account_id, "device_id": device_id, "lang": f["lang"], "thread_type": f["thread_type"],
        }
        if f["is_mutual"] is not None:
            source["is_mutual"] = f["is_mutual"]
        if f["is_follower"] is not None:
            source["is_follower"] = f["is_follower"]
        if not incoming:
            source["echo"] = True
            source["origin"] = "huoke"
        text = f["text"] or (f"[{f['media_type']}]" if f["media_type"] else "")
        msg = make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key, text=text,
                           name=(f["peer_name"] if incoming else ""), ts=ts, msg_id=f["msg_id"],
                           direction=("in" if incoming else "out"), media_type=f["media_type"],
                           username=f["peer_username"], avatar_url=(f["avatar"] if incoming else ""), source=source)
        emit(msg)
        if incoming:
            stats["accepted"] += 1
            try:
                await auto_reply(msg)
                stats["drafted"] += 1
            except Exception:
                logger.debug("[tiktok-huoke] 起草钩子异常", exc_info=True)
        else:
            stats["echo"] += 1
    report_health(st, account_id, now=t_now)
    return 200, {"ok": True, **stats}


# ── 回传（hand-back）队列入口 ──────────────────────────────────────────────────────────────
def _quiet_hours(account_id: str, config: Optional[Dict[str, Any]], now: float) -> bool:
    """自动链是否处于夜间静默（``work_hours_gate``，fail-open）。"""
    try:
        from src.inbox.work_hours_gate import in_work_hours, work_schedule_cfg
        return not in_work_hours(work_schedule_cfg(config or {}), PLATFORM, account_id, now_ts=now)
    except Exception:
        return False


def enqueue_reply(account_id: str, chat_key: str, text: str, *, config: Optional[Dict[str, Any]],
                  state: Optional[TikTokHuokeStateStore] = None, now: Optional[float] = None,
                  origin: str = "auto") -> Dict[str, Any]:
    """坐席 / 自动链的回复 → 出站队列。评论与私信各自闸门；返回 ``{"ok", "item_id"}`` 或 ``{"ok": False, "reason", "status"}``。"""
    cfg = bridge_cfg(config)
    t_now = float(now if now is not None else time.time())
    st = state or get_state_store(cfg["state_db_path"] or None)
    kind = kind_of_chat(chat_key)
    acc = st.account(account_id)
    if not kind or not acc:
        return {"ok": False, "reason": REASON_NOT_BRIDGED, "status": 400}
    txt = str(text or "")
    ctx = st.ctx(account_id, chat_key)
    if kind == KIND_COMMENT:
        return _enqueue_comment(st, cfg, account_id, chat_key, txt, ctx=ctx, now=t_now, origin=origin)
    return _enqueue_dm(st, cfg, config, account_id, chat_key, txt, ctx=ctx, acc=acc, now=t_now, origin=origin)


def _enqueue_comment(st: TikTokHuokeStateStore, cfg: Dict[str, Any], account_id: str, chat_key: str, txt: str, *,
                     ctx: Dict[str, Any], now: float, origin: str) -> Dict[str, Any]:
    try:
        from src.inbox.channel_policy import text_block_reason
        blk = text_block_reason(PLATFORM, txt, first_message=False)
    except Exception:
        blk = ""
    if blk:
        return {"ok": False, "reason": blk, "status": 409}
    if len(txt) > cfg["max_reply_len"]:
        return {"ok": False, "reason": f"{REASON_TOO_LONG}:{len(txt)}>{cfg['max_reply_len']}", "status": 409}
    if not ctx or float(ctx.get("last_inbound_ts") or 0) <= 0:
        return {"ok": False, "reason": REASON_NOT_BRIDGED, "status": 400}
    if int(ctx.get("out_since_inbound") or 0) >= 1:
        return {"ok": False, "reason": REASON_ONE_REPLY, "status": 409}
    if st.sent_today(account_id, now=now, kind=KIND_COMMENT) >= cfg["daily_cap"]:
        return {"ok": False, "reason": f"{REASON_DAILY_CAP}:{cfg['daily_cap']}", "status": 429}
    item_id = st.enqueue(account_id, chat_key, txt, ctx=ctx, now=now, kind=KIND_COMMENT, origin=origin)
    return {"ok": True, "item_id": item_id, "queued": True, "handback": True, "kind": KIND_COMMENT}


def _enqueue_dm(st: TikTokHuokeStateStore, cfg: Dict[str, Any], config: Optional[Dict[str, Any]], account_id: str,
                chat_key: str, txt: str, *, ctx: Dict[str, Any], acc: Dict[str, Any], now: float, origin: str) -> Dict[str, Any]:
    manual = str(origin or "auto") == "manual"
    # D-P3：智聊只接管对方开口之后的对话——会话没有对方入站（首触后对方未回）→ 一条都不发
    last_in = float((ctx or {}).get("last_inbound_ts") or 0)
    if last_in <= 0:
        return {"ok": False, "reason": REASON_REQUEST_PENDING, "status": 409}
    try:
        from src.inbox.channel_policy import text_block_reason
        blk = text_block_reason(PLATFORM, txt, mode=MODE, first_message=False)
    except Exception:
        blk = ""
    if blk:
        return {"ok": False, "reason": blk, "status": 409}
    if len(txt) > cfg["dm_max_len"]:
        return {"ok": False, "reason": f"policy_text_too_long:{len(txt)}/{cfg['dm_max_len']}", "status": 409}
    # 对方 N 小时未回：自动链停；人工只可再补 1 条
    silent = (now - last_in) > cfg["peer_silent_hours"] * 3600.0
    out_since = int((ctx or {}).get("out_since_inbound") or 0)
    if silent and (not manual or out_since >= 1):
        return {"ok": False, "reason": REASON_PEER_SILENT, "status": 409}
    # 夜间静默只拦自动链（人工要发就发）
    if not manual and cfg["quiet_hours_block_auto"] and _quiet_hours(account_id, config, now):
        return {"ok": False, "reason": REASON_QUIET_HOURS, "status": 409}
    # 每号日上限（账号时区切日；账号自带上限与全局取小）
    cap = int(acc.get("dm_daily_cap") or 0) or cfg["dm_daily_cap"]
    cap = min(cap, cfg["dm_daily_cap"])
    if st.sent_today(account_id, now=now, kind=KIND_DM, tz_name=str(acc.get("timezone") or "")) >= cap:
        return {"ok": False, "reason": f"{REASON_DAILY_CAP}:{cap}", "status": 429}
    # 真机离线（>2h 无心跳）→ 不收单（收了也发不出去，坐席该去修设备）
    if heartbeat_state(float(acc.get("last_seen_ts") or 0), now) == "expired":
        return {"ok": False, "reason": REASON_DEVICE_OFFLINE, "status": 503}
    item_id = st.enqueue(account_id, chat_key, txt, ctx=ctx, now=now, kind=KIND_DM, origin=origin)
    return {"ok": True, "item_id": item_id, "queued": True, "handback": True, "kind": KIND_DM}


def _mirror_queued(item_id: int, account_id: str, chat_key: str, text: str, *, kind: str, now: float,
                   emit: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
    """入队即写 out 镜像（status 空＝无勾＝「待真机」）；回执成功再升 ``sent``。"""
    if emit is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit
        emit = _emit
    from src.integrations.protocol_bridge import make_message
    emit(make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key, text=text, ts=now,
                      msg_id=f"hb:{item_id}", direction="out",
                      source={"source": SOURCE, "kind": kind, "mode": MODE, "handback_item": int(item_id),
                              "handback_state": "queued"}))


def _echo_sent(item: Dict[str, Any], *, emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
               status_reporter: Optional[Callable[..., Any]] = None) -> None:
    """回执成功：入队镜像 ``hb:<id>`` 升 ``sent``（有镜像则不再另起一条）；无镜像（老队列项）→ 补一条 out 回显。"""
    chat_key = str(item["chat_key"])
    account_id = str(item["account_id"])
    if status_reporter is None:
        from src.integrations.protocol_bridge import report_message_status as _rep
        status_reporter = _rep
    try:
        if status_reporter(PLATFORM, account_id, chat_key, f"hb:{item['id']}", "sent"):
            return
    except Exception:
        logger.debug("[tiktok-huoke] 镜像状态升级失败，回落补回显", exc_info=True)
    if emit is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit
        emit = _emit
    from src.integrations.protocol_bridge import make_message
    echo_id = str(item.get("external_id") or f"hb:{item['id']}")
    emit(make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key,
                      text=str(item["text"]), ts=float(item.get("acked_at") or time.time()),
                      msg_id=echo_id, direction="out",
                      source={"source": SOURCE, "kind": str(item.get("kind") or KIND_COMMENT), "mode": MODE,
                              "handback_item": int(item["id"]), "comment_id": str(item.get("comment_id") or ""),
                              "video_id": str(item.get("video_id") or ""), "echo": True}))
    try:
        # TK-3 P2：补出来的回显也是「已发出」——带勾，别让老队列项 / 直入队的回复看起来像待真机
        status_reporter(PLATFORM, account_id, chat_key, echo_id, "sent")
    except Exception:
        logger.debug("[tiktok-huoke] 回显升 sent 失败", exc_info=True)


def _record_failed(item: Dict[str, Any], error: str, *, store: Any = None) -> str:
    """回执失败 → 收件箱失败留痕（前端「发送失败 + 一键重发」，原因随行）。"""
    st = store
    if st is None:
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            st = get_inbox_store()
        except Exception:
            st = None
    fn = getattr(st, "record_failed_outbound", None)
    if fn is None:
        return ""
    conv = f"{PLATFORM}:{item['account_id']}:{item['chat_key']}"
    try:
        return str(fn(conv, str(item["text"]), reason=f"huoke:{error or 'send_failed'}") or "")
    except Exception:
        logger.debug("[tiktok-huoke] 失败留痕失败", exc_info=True)
        return ""


def ack_handback(payload: Dict[str, Any], *, config: Optional[Dict[str, Any]], state: Optional[TikTokHuokeStateStore] = None,
                 now: Optional[float] = None, emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 status_reporter: Optional[Callable[..., Any]] = None, store: Any = None) -> Tuple[int, Dict[str, Any]]:
    """``{"item_id", "ok", "external_id"?, "error"?, "window"?, "device_id"?}`` → sent（镜像升 sent）/ failed（留痕）；
    ``window`` 归一到 DY 名；回执也是一次心跳。"""
    cfg = bridge_cfg(config)
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        return 400, {"error": "item_id 必填"}
    st = state or get_state_store(cfg["state_db_path"] or None)
    ok = bool(payload.get("ok"))
    before = st.item(item_id)
    if before is None:
        return 404, {"error": "unknown_item"}
    already = before["status"] in ("sent", "failed")
    item = st.ack(item_id, ok=ok, external_id=str(payload.get("external_id") or ""), error=str(payload.get("error") or ""), now=now)
    if item is None:
        return 404, {"error": "unknown_item"}
    device_id = str(payload.get("device_id") or item.get("claimed_by") or "")
    if device_id:
        st.heartbeat(device_id, now=now)
        report_health(st, str(item["account_id"]), now=now)
    window: Dict[str, Any] = {}
    if isinstance(payload.get("window"), dict):
        from src.inbox.channel_policy import normalize_window_fields
        window = normalize_window_fields(payload.get("window"))
        st.put_window(str(item["account_id"]), window)
    if not already:
        try:
            if ok:
                _echo_sent(item, emit=emit, status_reporter=status_reporter)
            else:
                _record_failed(item, str(payload.get("error") or ""), store=store)
        except Exception:
            logger.debug("[tiktok-huoke] 回执后处理失败", exc_info=True)
    return 200, {"ok": True, "status": item["status"], "dup": already, "window": window}


# ── 收件箱适配器（send 回落路径）─────────────────────────────────────────────────────────
class TikTokHuokeAdapter:
    """``send_via_adapters`` 回落到此：只接管经本桥进过线的评论 / 私信会话 → 出站队列 + 入队镜像；其余维持旧 400
    （「不支持的平台」）。``collect_chats`` 不出数（会话已由 emit 落 inbox_store）；``status`` 报队列摘要。"""

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
        if not bridge_enabled(cfg) or not kind_of_chat(chat_key):
            raise ChannelSendError(400, f"不支持的平台: {PLATFORM}")
        try:
            from src.inbox.send_context import is_manual_send
            origin = "manual" if is_manual_send() else "auto"
        except Exception:
            origin = "auto"
        res = enqueue_reply(account_id, chat_key, text, config=cfg, origin=origin)
        if not res.get("ok"):
            reason = str(res.get("reason") or REASON_NOT_BRIDGED)
            if reason == REASON_NOT_BRIDGED:
                raise ChannelSendError(400, f"不支持的平台: {PLATFORM}")
            raise ChannelSendError(int(res.get("status") or 409), f"tiktok huoke handback blocked: {reason}", reason_code=reason)
        try:
            _mirror_queued(int(res["item_id"]), account_id, chat_key, text, kind=str(res.get("kind") or ""), now=time.time())
        except Exception:
            logger.debug("[tiktok-huoke] 入队镜像失败", exc_info=True)
        return {"ok": True, "queued": True, "handback": True, "item_id": res["item_id"], "kind": res.get("kind"),
                "message_id": f"hb:{res['item_id']}"}


def install_adapter(adapters: List[Any], config_getter: Callable[[], Dict[str, Any]]) -> bool:
    """幂等追加适配器到收件箱注册表（``unified_inbox_aggregate._INBOX_ADAPTERS``）。"""
    if any(getattr(a, "_tiktok_huoke_bridge", False) for a in adapters):
        return False
    adapters.append(TikTokHuokeAdapter(config_getter))
    return True


# ── 路由 ───────────────────────────────────────────────────────────────────────────────────
def register_tiktok_huoke_routes(app: Any, config_manager: Any) -> bool:
    """``tiktok.huoke_bridge.enabled`` 才挂：leads / dm 进线、devices 绑定、handback 认领、ack 回执、status，全程 ``api_auth``；并追加适配器。"""
    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    if not bridge_enabled(_cfg()) or JSONResponse is None:
        return False
    if any(getattr(r, "path", "") == LEADS_ROUTE for r in getattr(app, "routes", [])):
        return True
    api_auth = getattr(getattr(app, "state", None), "api_auth", None)
    deps = [Depends(api_auth)] if (api_auth is not None and Depends is not None) else []

    async def _json(request: Request) -> Any:
        try:
            return await request.json()
        except Exception:
            return None

    @app.post(LEADS_ROUTE, dependencies=deps)
    async def tiktok_huoke_leads(request: Request):
        payload = await _json(request)
        if payload is None:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = await ingest_leads(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.post(DM_ROUTE, dependencies=deps)
    async def tiktok_huoke_dm(request: Request):
        payload = await _json(request)
        if payload is None:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = await ingest_dm(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.post(DEVICES_ROUTE, dependencies=deps)
    async def tiktok_huoke_devices(request: Request):
        payload = await _json(request)
        if payload is None:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = bind_device(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.get(DEVICES_ROUTE, dependencies=deps)
    async def tiktok_huoke_devices_list(request: Request):
        cfg = bridge_cfg(_cfg())
        st = get_state_store(cfg["state_db_path"] or None)
        now = time.time()
        out = []
        for a in st.accounts():
            out.append({"account_id": a["account_id"], "device_id": a.get("device_id") or "", "username": a.get("username") or "",
                        "timezone": a.get("timezone") or "", "dm_daily_cap": int(a.get("dm_daily_cap") or 0) or cfg["dm_daily_cap"],
                        "last_seen_ts": float(a.get("last_seen_ts") or 0), "health": report_health(st, a["account_id"], now=now),
                        "sent_today_dm": st.sent_today(a["account_id"], now=now, kind=KIND_DM, tz_name=str(a.get("timezone") or "")),
                        "sent_today_comment": st.sent_today(a["account_id"], now=now, kind=KIND_COMMENT)})
        return {"ok": True, "accounts": out}

    @app.get(HANDBACK_ROUTE, dependencies=deps)
    async def tiktok_huoke_handback(request: Request, device_id: str = "", account_id: str = "", limit: int = 10):
        if not device_id and not account_id:
            return JSONResponse({"error": "device_id 或 account_id 必填"}, status_code=400)
        cfg = bridge_cfg(_cfg())
        st = get_state_store(cfg["state_db_path"] or None)
        now = time.time()
        if device_id:
            for aid in st.heartbeat(device_id, now=now):
                report_health(st, aid, now=now)
        items = st.claim(device_id, account_id=account_id, limit=limit, claim_ttl_sec=cfg["claim_ttl_sec"], now=now,
                         min_gap_sec=cfg["min_gap_sec"])
        return {"ok": True, "items": [{k: v for k, v in it.items() if k != "error"} for it in items],
                "policy": {"max_reply_len": cfg["max_reply_len"], "daily_cap": cfg["daily_cap"],
                           "dm_max_len": cfg["dm_max_len"], "dm_daily_cap": cfg["dm_daily_cap"], "min_gap_sec": cfg["min_gap_sec"]}}

    @app.get(HANDBACK_PENDING_ROUTE, dependencies=deps)
    async def tiktok_huoke_handback_pending(request: Request, device_id: str = "", account_id: str = ""):
        """TK-3 只看不认领：huoke 轮询器每 N 秒问一次，队列空就不建真机任务；带 device_id 顺手记心跳。"""
        if not device_id and not account_id:
            return JSONResponse({"error": "device_id 或 account_id 必填"}, status_code=400)
        cfg = bridge_cfg(_cfg())
        st = get_state_store(cfg["state_db_path"] or None)
        now = time.time()
        if device_id:
            for aid in st.heartbeat(device_id, now=now):
                report_health(st, aid, now=now)
        return {"ok": True, **st.pending(device_id, account_id=account_id, now=now, claim_ttl_sec=cfg["claim_ttl_sec"])}

    @app.post(HANDBACK_ACK_ROUTE, dependencies=deps)
    async def tiktok_huoke_handback_ack(request: Request):
        payload = await _json(request)
        if payload is None:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        status, resp = ack_handback(payload, config=_cfg())
        return JSONResponse(resp, status_code=status)

    @app.get(STATUS_ROUTE, dependencies=deps)
    async def tiktok_huoke_status(request: Request):
        cfg = bridge_cfg(_cfg())
        st = get_state_store(cfg["state_db_path"] or None)
        health = sweep_health(config=_cfg(), state=st)
        return {"ok": True, "enabled": True, **st.summary(), "health": health,
                "policy": {"min_intent": cfg["min_intent"], "max_reply_len": cfg["max_reply_len"], "daily_cap": cfg["daily_cap"],
                           "dm_max_len": cfg["dm_max_len"], "dm_daily_cap": cfg["dm_daily_cap"],
                           "peer_silent_hours": cfg["peer_silent_hours"]}}

    try:
        from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
        install_adapter(_INBOX_ADAPTERS, _cfg)
    except Exception:
        logger.debug("[tiktok-huoke] 适配器追加跳过", exc_info=True)
    logger.info("[tiktok-huoke] 桥已挂载 %s / %s / %s / %s / %s / %s", LEADS_ROUTE, DM_ROUTE, DEVICES_ROUTE, HANDBACK_ROUTE,
                HANDBACK_ACK_ROUTE, STATUS_ROUTE)
    return True


__all__ = [
    "PLATFORM", "MODE", "SOURCE", "KIND_COMMENT", "KIND_DM", "CHAT_PREFIX", "DM_CHAT_PREFIX", "COMMENT_MAX_LEN", "DM_MAX_LEN",
    "LEADS_ROUTE", "DM_ROUTE", "DEVICES_ROUTE", "HANDBACK_ROUTE", "HANDBACK_ACK_ROUTE", "HANDBACK_PENDING_ROUTE", "STATUS_ROUTE",
    "REASON_ONE_REPLY", "REASON_DAILY_CAP", "REASON_TOO_LONG", "REASON_NOT_BRIDGED", "REASON_REQUEST_PENDING",
    "REASON_PEER_SILENT", "REASON_QUIET_HOURS", "REASON_DEVICE_OFFLINE",
    "bridge_cfg", "bridge_enabled", "chat_key_for", "dm_chat_key_for", "is_bridge_chat", "is_dm_chat", "kind_of_chat",
    "heartbeat_state", "TikTokHuokeStateStore", "get_state_store", "report_health", "sweep_health", "bind_device",
    "ingest_leads", "ingest_dm", "enqueue_reply", "ack_handback",
    "TikTokHuokeAdapter", "install_adapter", "register_tiktok_huoke_routes",
]
