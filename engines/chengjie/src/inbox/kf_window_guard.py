# -*- coding: utf-8 -*-
"""微信客服「48h / 5 条」窗口配额守卫 + 拉取游标持久化（实施97 线 A，2026-09-07）。

微信客服的硬规则：**客户每发一条消息，企业在其后 48 小时内最多可发 5 条**（客户再发则重置）；
超窗/超条不报错，``send_msg`` 照样返回成功，失败以 ``msg_send_fail`` 事件（fail_type 4/6）事后
回来——不在本地记账就会「坐席看着发了、客户什么都没收到」。本模块把这条规则做成**出站收口
点的确定性判定**：

- 记账：worker 收到客户消息 → :meth:`KfStateStore.record_inbound`（重置本轮）；每成功发一条 →
  :meth:`record_sent`；收到 ``msg_send_fail`` → :meth:`record_fail`（4/5/6/10 直接关窗，等客户下一条）。
- 判定：:func:`check` → :class:`Verdict`。接在 ``send_guard.send_blocked``（编排器发送**唯一**收口，
  自动链/人工链/媒体链全经此）——命中即 ``{delivered: False, blocked: kf_*}``，autosend 走既有
  投递失败链（审计 + 坐席铃铛），不会假装送达。
- 平台封顶（同一事实源 :data:`QUOTA_WINDOW_PLATFORMS`）：``reply_split`` 在这些平台**不拆条**
  （拆 5 条＝一轮配额全没）、``holding_reply`` 不发「稍等」缓冲话术（白占 1 条）。
- 人工预留：自动链在剩余 ≤ ``reserve_for_manual``（默认 1）时让路，给坐席留最后一句。
- 敏感词（:func:`sensitive_hit`）：微信对加密货币/转账/外链/引流类词汇有安全过滤（fail_type 13），
  自动链命中即 HOLD 转人审；人工原文不拦（坐席自负）。词表 ``wechat_kf.sensitive_terms`` 可覆写。

持久化：``<config_dir>/wechat_kf_state.db``（SQLite，WAL）。表 ``kf_cursor``（每客服账号 sync_msg
游标——重启不重拉不漏拉）与 ``kf_turn``（每 (账号, 客户) 本轮计数）。全程绝不抛：守卫自身
故障按「放行」处理（broken guard 不得反过来把全部发送卡死，与 send_guard 同哲学）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

#: 受「窗口 + 条数配额」约束的平台（reply_split / holding_reply / send_guard 共用这一处定义）
QUOTA_WINDOW_PLATFORMS = frozenset({"wechat_kf"})

DEFAULT_WINDOW_SEC = 48 * 3600.0
DEFAULT_QUOTA_PER_TURN = 5
DEFAULT_RESERVE_FOR_MANUAL = 1

REASON_NO_INBOUND = "kf_no_inbound"                   # 从未收到该客户消息（发了必 fail_type 4）
REASON_WINDOW_EXPIRED = "kf_window_expired"           # 距客户最后一条 > 48h
REASON_WINDOW_CLOSED = "kf_window_closed"             # 平台已回 msg_send_fail（会话关闭/拒收/超条…）
REASON_QUOTA_EXHAUSTED = "kf_quota_exhausted"         # 本轮 5 条用完
REASON_RESERVED_FOR_MANUAL = "kf_quota_reserved_for_manual"  # 自动链让路给坐席
REASON_SENSITIVE = "kf_sensitive_term"

#: 默认敏感词（小写比较；覆写 wechat_kf.sensitive_terms 整表替换）。刻意不收单字/泛词，
#: 只收「微信安全过滤高命中 + 与本产品客群（跨境/加密边缘）高重叠」的词。
DEFAULT_SENSITIVE_TERMS: Sequence[str] = (
    "usdt", "泰达币", "比特币", "btc", "eth", "加密货币", "虚拟货币", "虚拟币", "数字货币",
    "转账", "打款", "汇款", "银行卡号", "收款码",
    "加微信", "加我微信", "微信号", "vx号", "私聊我", "二维码",
    "http://", "https://", "vpn", "翻墙", "博彩", "彩票", "赌博", "贷款", "网贷", "催收",
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    remaining: int = 0
    window_remaining_sec: float = 0.0
    matched: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "remaining": self.remaining,
                "window_remaining_sec": round(self.window_remaining_sec, 1),
                "matched": self.matched}


def is_quota_platform(platform: Any) -> bool:
    return str(platform or "").strip().lower() in QUOTA_WINDOW_PLATFORMS


def resolve_guard_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``wechat_kf.window_guard`` 块 → 归一配置（缺省全用官方硬规则数值）。"""
    blk: Dict[str, Any] = {}
    try:
        blk = dict((((config or {}).get("wechat_kf") or {}).get("window_guard")) or {})
    except Exception:
        blk = {}

    def _f(key: str, default: float, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(blk.get(key, default))))
        except (TypeError, ValueError):
            return float(default)

    quota = int(_f("quota_per_turn", DEFAULT_QUOTA_PER_TURN, 1, 5))
    reserve = int(_f("reserve_for_manual", DEFAULT_RESERVE_FOR_MANUAL, 0, quota - 1))
    return {
        "enabled": bool(blk.get("enabled", True)),
        "quota_per_turn": quota,
        "reserve_for_manual": reserve,
        "window_sec": _f("window_sec", DEFAULT_WINDOW_SEC, 60.0, DEFAULT_WINDOW_SEC),
    }


def resolve_sensitive_terms(config: Optional[Dict[str, Any]]) -> Sequence[str]:
    try:
        raw = ((config or {}).get("wechat_kf") or {}).get("sensitive_terms")
    except Exception:
        raw = None
    if isinstance(raw, (list, tuple)):
        terms = [str(x).strip().lower() for x in raw if str(x).strip()]
        return tuple(terms)
    return DEFAULT_SENSITIVE_TERMS


def sensitive_hit(text: Any, terms: Optional[Sequence[str]] = None) -> str:
    """文本命中的第一个敏感词（小写子串匹配）；无命中返回空串。纯函数。"""
    low = str(text or "").lower()
    if not low:
        return ""
    for t in (terms if terms is not None else DEFAULT_SENSITIVE_TERMS):
        tt = str(t or "").lower()
        if tt and tt in low:
            return tt
    return ""


# ── 持久化 ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS kf_cursor (
    account_id TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kf_turn (
    account_id         TEXT NOT NULL,
    external_userid    TEXT NOT NULL,
    last_inbound_ts    REAL NOT NULL DEFAULT 0,
    sent_since_inbound INTEGER NOT NULL DEFAULT 0,
    closed_reason      TEXT NOT NULL DEFAULT '',
    closed_at          REAL NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, external_userid)
);
"""


class KfStateStore:
    """SQLite 状态库（线程安全，单连接 + 锁）。``path=":memory:"`` 供单测。"""

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
            return str(config_dir() / "wechat_kf_state.db")
        except Exception:
            return os.path.join("config", "wechat_kf_state.db")

    # ── 游标 ──
    def get_cursor(self, account_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM kf_cursor WHERE account_id=?", (str(account_id),)).fetchone()
        return str(row["cursor"]) if row else ""

    def set_cursor(self, account_id: str, cursor: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kf_cursor(account_id, cursor, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET cursor=excluded.cursor, "
                "updated_at=excluded.updated_at",
                (str(account_id), str(cursor or ""), time.time()))
            self._conn.commit()

    # ── 本轮计数 ──
    def get_turn(self, account_id: str, external_userid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kf_turn WHERE account_id=? AND external_userid=?",
                (str(account_id), str(external_userid))).fetchone()
        return dict(row) if row else None

    def record_inbound(self, account_id: str, external_userid: str, ts: Optional[float] = None) -> None:
        """客户发来一条 → 新一轮：计数归零、关窗标记清除、窗口起点=消息时间。

        游标重放/乱序保护：**严格更早**的消息（ts 小于已记起点）不把窗口往回拨；同一秒内的
        第二条是真实的新消息（worker 已按 msgid 去重，进到这里的都不是重放），必须重开本轮——
        微信 send_time 只有秒级精度，客户连发两句常落在同一秒。
        """
        t = float(ts or time.time())
        with self._lock:
            cur = self._conn.execute(
                "SELECT last_inbound_ts FROM kf_turn WHERE account_id=? AND external_userid=?",
                (str(account_id), str(external_userid))).fetchone()
            if cur is not None and float(cur["last_inbound_ts"] or 0) > t:
                return
            self._conn.execute(
                "INSERT INTO kf_turn(account_id, external_userid, last_inbound_ts, "
                "sent_since_inbound, closed_reason, closed_at, updated_at) VALUES(?,?,?,0,'',0,?) "
                "ON CONFLICT(account_id, external_userid) DO UPDATE SET "
                "last_inbound_ts=excluded.last_inbound_ts, sent_since_inbound=0, "
                "closed_reason='', closed_at=0, updated_at=excluded.updated_at",
                (str(account_id), str(external_userid), t, time.time()))
            self._conn.commit()

    def record_sent(self, account_id: str, external_userid: str, n: int = 1) -> int:
        """成功发出 n 条 → 计数累加。返回累加后的本轮已发数。没有本轮记录时也建行（防漏记）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO kf_turn(account_id, external_userid, last_inbound_ts, "
                "sent_since_inbound, updated_at) VALUES(?,?,0,?,?) "
                "ON CONFLICT(account_id, external_userid) DO UPDATE SET "
                "sent_since_inbound=sent_since_inbound+excluded.sent_since_inbound, "
                "updated_at=excluded.updated_at",
                (str(account_id), str(external_userid), max(1, int(n)), time.time()))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT sent_since_inbound FROM kf_turn WHERE account_id=? AND external_userid=?",
                (str(account_id), str(external_userid))).fetchone()
        return int(row["sent_since_inbound"]) if row else 0

    def record_fail(self, account_id: str, external_userid: str, fail_type: Any) -> bool:
        """``msg_send_fail`` → 4/5/6/10 关窗（等客户下一条自动重开）。返回是否关了窗。"""
        try:
            ft = int(fail_type)
        except (TypeError, ValueError):
            return False
        from src.integrations.wechat_kf import WINDOW_CLOSING_FAIL_TYPES
        if ft not in WINDOW_CLOSING_FAIL_TYPES:
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO kf_turn(account_id, external_userid, closed_reason, closed_at, "
                "updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(account_id, external_userid) DO UPDATE SET "
                "closed_reason=excluded.closed_reason, closed_at=excluded.closed_at, "
                "updated_at=excluded.updated_at",
                (str(account_id), str(external_userid), f"fail_type_{ft}", time.time(), time.time()))
            self._conn.commit()
        return True

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_STORE: Optional[KfStateStore] = None
_STORE_LOCK = threading.Lock()


def get_kf_state_store(path: Optional[str] = None) -> KfStateStore:
    """进程级单例（首次调用可指定路径；``_reset_for_tests`` 后重建）。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = KfStateStore(path)
        return _STORE


def _reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


# ── 判定 ─────────────────────────────────────────────────────────────────────

def check(
    account_id: str,
    external_userid: str,
    *,
    origin: str = "auto",
    now: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
    store: Optional[KfStateStore] = None,
) -> Verdict:
    """本次发送能否放行（纯判定，不改状态；成功发送后由调用方 ``record_sent``）。

    判序：从未入站 → 平台关窗 → 超 48h → 条数用完 → 自动链让路人工预留。
    ``origin="manual"``（坐席亲手发/人审通过）可用满 5 条；其余按预留让路。
    """
    cfg = resolve_guard_cfg(config)
    quota = int(cfg["quota_per_turn"])
    if not cfg.get("enabled", True):
        return Verdict(True, "", quota, cfg["window_sec"])
    try:
        st = store or get_kf_state_store()
        turn = st.get_turn(account_id, external_userid)
    except Exception:
        logger.debug("[kf_window_guard] 读状态失败（放行）", exc_info=True)
        return Verdict(True, "", quota, cfg["window_sec"])
    t = float(now if now is not None else time.time())
    if not turn or float(turn.get("last_inbound_ts") or 0) <= 0:
        return Verdict(False, REASON_NO_INBOUND, 0, 0.0)
    if str(turn.get("closed_reason") or ""):
        return Verdict(False, REASON_WINDOW_CLOSED, 0, 0.0,
                       matched=str(turn.get("closed_reason") or ""))
    elapsed = t - float(turn.get("last_inbound_ts") or 0)
    remaining_win = float(cfg["window_sec"]) - elapsed
    if remaining_win <= 0:
        return Verdict(False, REASON_WINDOW_EXPIRED, 0, 0.0)
    sent = int(turn.get("sent_since_inbound") or 0)
    remaining = max(0, quota - sent)
    if remaining <= 0:
        return Verdict(False, REASON_QUOTA_EXHAUSTED, 0, remaining_win)
    if str(origin or "auto") != "manual" and remaining <= int(cfg["reserve_for_manual"]):
        return Verdict(False, REASON_RESERVED_FOR_MANUAL, remaining, remaining_win)
    return Verdict(True, "", remaining, remaining_win)


def send_block_reason(platform: str, account_id: str, chat_key: str, *, origin: str = "auto",
                      config: Optional[Dict[str, Any]] = None) -> str:
    """供 ``send_guard.send_blocked`` 调用的薄封装：非配额平台/无 chat_key → 空串放行。"""
    if not is_quota_platform(platform) or not str(chat_key or "").strip():
        return ""
    try:
        from src.integrations.wechat_kf import external_userid_from_chat_key
        uid = external_userid_from_chat_key(chat_key)
        v = check(str(account_id or ""), uid, origin=origin, config=config)
        return "" if v.allowed else v.reason
    except Exception:
        logger.debug("[kf_window_guard] 判定异常（放行）", exc_info=True)
        return ""


def auto_text_block_reason(platform: str, text: str, *, origin: str = "auto",
                           config: Optional[Dict[str, Any]] = None) -> str:
    """自动链文本敏感词守卫：命中返回 ``kf_sensitive_term:<词>``，否则空串。人工不拦。"""
    if not is_quota_platform(platform) or str(origin or "auto") == "manual":
        return ""
    hit = sensitive_hit(text, resolve_sensitive_terms(config))
    return f"{REASON_SENSITIVE}:{hit}" if hit else ""


def snapshot(platform: str, account_id: str, chat_key: str, *, now: Optional[float] = None,
             config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """会话配额快照（UI 指示「本轮还可发 N 条 · 窗口剩 Xh」）。非配额平台返回 ``{}``。"""
    if not is_quota_platform(platform):
        return {}
    try:
        from src.integrations.wechat_kf import external_userid_from_chat_key
        uid = external_userid_from_chat_key(chat_key)
        st = get_kf_state_store()
        turn = st.get_turn(str(account_id or ""), uid) or {}
        v_manual = check(str(account_id or ""), uid, origin="manual", now=now, config=config,
                         store=st)
        v_auto = check(str(account_id or ""), uid, origin="auto", now=now, config=config, store=st)
        return {
            "platform": platform, "quota": resolve_guard_cfg(config)["quota_per_turn"],
            "sent": int(turn.get("sent_since_inbound") or 0),
            "remaining": v_manual.remaining,
            "window_remaining_sec": round(v_manual.window_remaining_sec, 1),
            "last_inbound_ts": float(turn.get("last_inbound_ts") or 0),
            "closed_reason": str(turn.get("closed_reason") or ""),
            "manual_allowed": v_manual.allowed, "auto_allowed": v_auto.allowed,
            "reason": v_manual.reason or v_auto.reason,
        }
    except Exception:
        logger.debug("[kf_window_guard] snapshot 异常", exc_info=True)
        return {}


def ui_snapshot(platform: str, account_id: str, chat_key: str, *, now: Optional[float] = None,
                config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """与 ``window_guard.snapshot`` **同一 UI 契约**的快照（``send-caps.reply_window``），让工作台现成的
    倒计时/剩余条数信息带直接画微信客服的 48h·5 条：``cap/sent/remaining/reserve_for_manual/no_inbound/
    remaining_sec/window_sec/deadline_ts/expired`` + ``manual_allowed/auto_allowed/reason``。
    平台关窗（msg_send_fail 4/5/6/10）表现为 ``remaining_sec=0, expired=True``（UI 文案「等客户再发言」正合语义），
    并额外带 ``closed_reason``。非配额平台 → ``{}``。
    """
    k = snapshot(platform, account_id, chat_key, now=now, config=config)
    if not k:
        return {}
    try:
        cfg = resolve_guard_cfg(config)
        window_sec = float(cfg["window_sec"])
        last_in = float(k.get("last_inbound_ts") or 0.0)
        closed = bool(k.get("closed_reason"))
        remaining_sec = 0.0 if closed else float(k.get("window_remaining_sec") or 0.0)
        return {
            "platform": str(platform), "window_sec": window_sec, "cap": int(k.get("quota") or 0),
            "reserve_for_manual": int(cfg["reserve_for_manual"]),
            "last_inbound_ts": last_in, "sent": int(k.get("sent") or 0),
            "remaining": int(k.get("remaining") or 0),
            "remaining_sec": round(remaining_sec, 1),
            "deadline_ts": (last_in + window_sec) if last_in > 0 else 0.0,
            "no_inbound": last_in <= 0,
            "expired": last_in > 0 and remaining_sec <= 0,
            "closed_reason": str(k.get("closed_reason") or ""),
            "manual_allowed": bool(k.get("manual_allowed")), "auto_allowed": bool(k.get("auto_allowed")),
            "reason": str(k.get("reason") or ""),
        }
    except Exception:
        logger.debug("[kf_window_guard] ui_snapshot 异常", exc_info=True)
        return {}


__all__ = [
    "QUOTA_WINDOW_PLATFORMS", "DEFAULT_WINDOW_SEC", "DEFAULT_QUOTA_PER_TURN",
    "DEFAULT_RESERVE_FOR_MANUAL", "DEFAULT_SENSITIVE_TERMS",
    "REASON_NO_INBOUND", "REASON_WINDOW_EXPIRED", "REASON_WINDOW_CLOSED",
    "REASON_QUOTA_EXHAUSTED", "REASON_RESERVED_FOR_MANUAL", "REASON_SENSITIVE",
    "Verdict", "KfStateStore", "get_kf_state_store", "is_quota_platform",
    "resolve_guard_cfg", "resolve_sensitive_terms", "sensitive_hit", "check",
    "send_block_reason", "auto_text_block_reason", "snapshot", "ui_snapshot",
]
