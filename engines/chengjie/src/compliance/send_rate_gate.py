# -*- coding: utf-8 -*-
"""智安 · 发送限速闸（预热期硬上限 + 脚本流量日上限）。

背景（ZHILIAO_PLAN_20261008 P0-4）：10-07 预热期号日发 212 条（建议 ≤15）；6834…252 上的
一问一答脚本测试流量 ``sent_by`` 为空、计入风控把号打成红灯。既有 ``companion_send_gate``
缺省关闭，且 D-Q2「额度永不限制人工」——预热期号被坐席 / 脚本照样打穿。

本闸挂在编排器统一出站护栏 ``send_guard.send_blocked`` 的**最后一道**（急停 / 金丝雀 /
既有反封号闸都放行之后），因此 AI 自动回复、L2 autosend、主动触达、坐席手发（经编排器）
一条链全覆盖：

1. **预热期硬上限**（``warmup_block``，缺省开）：账号天龄 < ``warmup_ramp_days`` 时，
   滚动 24h 内本系统发出的条数（ai + agent + script）≥ 建议上限
   ``account_health.warmup_cap(age)`` → 拦截（AI 与坐席一视同仁），只留痕不发。
   天龄未知（registry 无 created_at）→ 不判（不臆造预热期）。预热期满 → 本道不管。
2. **脚本流量日上限**（``script_daily_cap``，缺省 20/号/24h）：被标成 ``script`` 的发送
   单独计数，超过即拦——防测试把号打封。标 script 的三种方式：
   - 请求头 ``X-Send-Origin: script``（或 JSON ``origin: "script"``）→ 收件箱发送路由
     ``mark_script_from_request`` 置位上下文；
   - ``compliance.send_rate_gate.script_peers``：对端 chat_key 命中（子串，号码有无国家码均可）；
   - ``compliance.send_rate_gate.script_accounts``：``platform:account_id`` 或 ``account_id``。
3. 手机端 / 外部发送（``phone``）不经本系统出站，不拦，只经 :func:`record_external` 单独计数。

来源口径与 ``messages.sent_by`` 对齐：``ai`` / ``agent`` / ``script`` / ``phone``
（:func:`origin_to_sent_by`）。计数与审计落 ``config/send_rate_gate.db``（不记消息原文）。

紧急关闭：``compliance.send_rate_gate.enabled: false``。任何异常一律放行（fail-open，
与 send_guard 同口径：坏掉的护栏不得把全部发送卡死），但会打 warning。
"""
from __future__ import annotations

import contextvars
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

ORIGIN_AI = "ai"
ORIGIN_AGENT = "agent"
ORIGIN_SCRIPT = "script"
ORIGIN_PHONE = "phone"
ORIGINS = (ORIGIN_AI, ORIGIN_AGENT, ORIGIN_SCRIPT, ORIGIN_PHONE)
# 本系统发出的（计入预热上限）；phone 是手机端 / 外部发的，单独计数
SYSTEM_ORIGINS = (ORIGIN_AI, ORIGIN_AGENT, ORIGIN_SCRIPT)

REASON_WARMUP = "warmup_cap"
REASON_SCRIPT = "script_daily_cap"
_DAY = 86400.0

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "warmup_block": True,
    "script_daily_cap": 20,
    "script_peers": [],
    "script_accounts": [],
    "db_path": "config/send_rate_gate.db",
}

# ═══════════════════════════════════════════════════════════════════════
# ① 配置 / 来源
# ═══════════════════════════════════════════════════════════════════════


def gate_cfg(config: Any = None) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        c = config if isinstance(config, dict) else (getattr(config, "config", None) or {})
        node = ((c or {}).get("compliance") or {}).get("send_rate_gate")
        if isinstance(node, dict):
            out.update({k: v for k, v in node.items() if v is not None})
    except Exception:
        pass
    return out


#: 脚本测试号名单的环境变量覆盖（逗号分隔；设为空串＝清空名单）。限速闸与 sent_by 归因共用。
SCRIPT_ACCOUNTS_ENV = "CHENGJIE_SCRIPT_SENDER_ACCOUNTS"


def script_accounts(config: Any = None) -> list:
    """脚本测试号名单的**唯一**读取口（2026-10-08，限速闸 + ``messages.sent_by`` 归因共用）。

    优先级：环境变量 ``CHENGJIE_SCRIPT_SENDER_ACCOUNTS``（设了就整体覆盖，空串＝清空）
    > 配置 ``compliance.send_rate_gate.script_accounts`` > :data:`DEFAULTS`（空）。
    ``config`` 缺省时读 ``compliance.runtime.runtime_config()``（装配层注册的实时配置，
    跟随 overlay 热重载）；条目写 ``platform:account_id`` 或 ``account_id``。绝不抛。"""
    raw = os.environ.get(SCRIPT_ACCOUNTS_ENV)
    if raw is not None:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if config is None:
        try:
            from src.compliance.runtime import runtime_config
            config = runtime_config()
        except Exception:
            config = None
    try:
        v = gate_cfg(config).get("script_accounts")
        if isinstance(v, str):
            v = v.split(",")
        return [str(x).strip() for x in (v or []) if str(x or "").strip()]
    except Exception:
        return []


def is_script_account(platform: Any, account_id: Any, config: Any = None) -> bool:
    """``platform:account_id`` 是否脚本测试号（匹配口径与 :func:`classify_origin` 同一个）。"""
    acct = str(account_id or "").strip()
    if not acct:
        return False
    try:
        return _match_list(script_accounts(config), f"{platform or ''}:{acct}", acct)
    except Exception:
        return False


def _truthy(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


def enabled(config: Any = None) -> bool:
    """缺省 True；显式 ``compliance.send_rate_gate.enabled: false`` 才关。

    环境变量 ``ZHILIAO_SEND_RATE_GATE=off`` 也关（测试套件 conftest 缺省置位，避免
    既有用例里「新登记账号连发几条」被预热上限误拦；本闸自己的用例显式打开）。"""
    env = os.environ.get("ZHILIAO_SEND_RATE_GATE")
    if env is not None and env.strip().lower() in ("0", "off", "false", "no"):
        return False
    return _truthy(gate_cfg(config).get("enabled"), True)


def _warmup_params(config: Any = None) -> Dict[str, int]:
    """预热曲线与既有 ``companion_send_gate`` / 发送额度看板同源（建议上限口径一致）。"""
    try:
        c = config if isinstance(config, dict) else {}
        g = (c.get("companion_send_gate") or {}) if isinstance(c, dict) else {}
    except Exception:
        g = {}

    def _i(key: str, dflt: int) -> int:
        try:
            v = int(g.get(key, dflt) or dflt)
            return v if v > 0 else dflt
        except Exception:
            return dflt

    return {"target_cap": _i("target_cap", 15), "start_cap": _i("warmup_start_cap", 2),
            "ramp_days": _i("warmup_ramp_days", 14)}


_SCRIPT_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "zhiliao_script_send", default=False)


def in_script_scope() -> bool:
    try:
        return bool(_SCRIPT_CTX.get())
    except Exception:
        return False


def set_script_scope(flag: bool = True):
    """把当前 await 链标成脚本发送；返回 token（可 ``_SCRIPT_CTX.reset``）。每个 HTTP 请求
    在自己的任务上下文里跑，置位不会泄漏到别的请求。"""
    return _SCRIPT_CTX.set(bool(flag))


def reset_script_scope(token: Any) -> None:
    try:
        _SCRIPT_CTX.reset(token)
    except Exception:
        pass


def is_script_marker(value: Any) -> bool:
    return str(value or "").strip().lower() in ("script", "test", "scripted")


def mark_script_from_request(request: Any, body: Optional[Dict[str, Any]] = None) -> bool:
    """收件箱发送路由入口调用：请求头 ``X-Send-Origin`` 或 body ``origin`` 是 script → 置位。"""
    flag = False
    try:
        hdrs = getattr(request, "headers", None)
        if hdrs is not None and is_script_marker(hdrs.get("x-send-origin")):
            flag = True
        if not flag and isinstance(body, dict) and is_script_marker(body.get("origin")):
            flag = True
    except Exception:
        flag = False
    if flag:
        set_script_scope(True)
    return flag


def _match_list(items: Iterable[Any], *cands: str) -> bool:
    for it in items or []:
        s = str(it or "").strip()
        if not s:
            continue
        for c in cands:
            c = str(c or "").strip()
            if c and (s == c or (len(s) >= 5 and s in c) or (len(c) >= 5 and c in s)):
                return True
    return False


def classify_origin(origin: Any, *, platform: str = "", account_id: str = "",
                    chat_key: str = "", config: Any = None) -> str:
    """把护栏口径（``auto`` / ``manual`` / ``script`` / ``phone``）归一到 sent_by 口径。"""
    o = str(origin or "").strip().lower()
    if o == ORIGIN_PHONE:
        return ORIGIN_PHONE
    if o == ORIGIN_SCRIPT or in_script_scope():
        return ORIGIN_SCRIPT
    g = gate_cfg(config)
    try:
        if chat_key and _match_list(g.get("script_peers") or [], str(chat_key)):
            return ORIGIN_SCRIPT
        acct = str(account_id or "")
        if acct and _match_list(script_accounts(config), f"{platform}:{acct}", acct):
            return ORIGIN_SCRIPT
    except Exception:
        pass
    if o in ("manual", ORIGIN_AGENT, "human"):
        return ORIGIN_AGENT
    return ORIGIN_AI


def origin_to_sent_by(origin: Any) -> str:
    """护栏 origin → ``messages.sent_by`` 取值（ai / agent / script / phone）。"""
    o = str(origin or "").strip().lower()
    if o in ORIGINS:
        return o
    if o in ("manual", "human"):
        return ORIGIN_AGENT
    return ORIGIN_AI


# ═══════════════════════════════════════════════════════════════════════
# ② 计数 / 审计存储
# ═══════════════════════════════════════════════════════════════════════

_DDL = """
CREATE TABLE IF NOT EXISTS send_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    chat_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sre_acct_ts ON send_rate_events(platform, account_id, ts);
CREATE TABLE IF NOT EXISTS send_rate_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    chat_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    used INTEGER NOT NULL DEFAULT 0,
    cap INTEGER NOT NULL DEFAULT 0,
    age_days REAL
);
CREATE INDEX IF NOT EXISTS idx_sra_ts ON send_rate_audit(ts);
"""


class SendRateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._path = str(path or "")
        if self._path and self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10)
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record(self, platform: str, account_id: str, origin: str, chat_key: str = "",
               *, ts: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO send_rate_events (ts, platform, account_id, origin, chat_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (float(ts if ts is not None else time.time()), str(platform or ""),
                 str(account_id or ""), str(origin or ""), str(chat_key or "")))
            # 只留 8 天（滚动 24h 判定 + 一周回看）
            self._conn.execute("DELETE FROM send_rate_events WHERE ts < ?",
                               (time.time() - 8 * _DAY,))
            self._conn.commit()

    def count(self, platform: str, account_id: str, *, since: float,
              origins: Iterable[str] = SYSTEM_ORIGINS) -> int:
        orig = [str(o) for o in origins]
        if not orig:
            return 0
        q = ("SELECT COUNT(*) FROM send_rate_events WHERE platform=? AND account_id=? AND ts>=? "
             f"AND origin IN ({','.join('?' * len(orig))})")
        with self._lock:
            row = self._conn.execute(q, (str(platform or ""), str(account_id or ""),
                                         float(since), *orig)).fetchone()
        return int(row[0] or 0) if row else 0

    def times(self, platform: str, account_id: str, *, since: float,
              origins: Iterable[str] = SYSTEM_ORIGINS) -> list:
        """窗口内计数事件时间戳（升序）——算「几点恢复」用。"""
        orig = [str(o) for o in origins]
        if not orig:
            return []
        q = ("SELECT ts FROM send_rate_events WHERE platform=? AND account_id=? AND ts>=? "
             f"AND origin IN ({','.join('?' * len(orig))}) ORDER BY ts ASC")
        with self._lock:
            rows = self._conn.execute(q, (str(platform or ""), str(account_id or ""),
                                          float(since), *orig)).fetchall()
        return [float(r[0]) for r in rows]

    def counts_by_origin(self, platform: str, account_id: str, *, since: float) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT origin, COUNT(*) FROM send_rate_events WHERE platform=? AND account_id=? "
                "AND ts>=? GROUP BY origin", (str(platform or ""), str(account_id or ""),
                                              float(since))).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def audit(self, **row: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO send_rate_audit (ts, platform, account_id, origin, chat_key, action, "
                "reason, used, cap, age_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (float(row.get("ts") or time.time()), str(row.get("platform") or ""),
                 str(row.get("account_id") or ""), str(row.get("origin") or ""),
                 str(row.get("chat_key") or ""), str(row.get("action") or ""),
                 str(row.get("reason") or ""), int(row.get("used") or 0),
                 int(row.get("cap") or 0), row.get("age_days")))
            self._conn.commit()

    def audit_rows(self, limit: int = 100, *, action: str = "") -> list:
        q = "SELECT * FROM send_rate_audit"
        args: list = []
        if action:
            q += " WHERE action=?"
            args.append(action)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]


_INSTANCES: Dict[str, SendRateStore] = {}
_INST_LOCK = threading.Lock()


def get_store(config: Any = None) -> SendRateStore:
    path = str(gate_cfg(config).get("db_path") or DEFAULTS["db_path"])
    env = os.environ.get("ZHILIAO_SEND_RATE_DB")
    if env:
        path = env
    elif path != ":memory:" and not os.path.isabs(path):
        # 部署约定：相对路径落在实例数据根（AITR_DATA_DIR）下；未设置则相对 CWD
        root = os.environ.get("AITR_DATA_DIR")
        if root:
            path = str(Path(root) / path)
    with _INST_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            try:
                inst = SendRateStore(path)
            except Exception:
                logger.warning("[send-rate-gate] 打开 %s 失败，回落内存计数", path, exc_info=True)
                inst = SendRateStore(None)
            _INSTANCES[path] = inst
    return inst


def reset_for_tests() -> None:
    with _INST_LOCK:
        for inst in _INSTANCES.values():
            inst.close()
        _INSTANCES.clear()


# ═══════════════════════════════════════════════════════════════════════
# ③ 判定
# ═══════════════════════════════════════════════════════════════════════


def _age_days(platform: str, account_id: str, registry: Any, now: float) -> Optional[float]:
    if registry is None:
        try:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        except Exception:
            registry = None
    if registry is None:
        return None
    try:
        acc = registry.get(platform, account_id) or {}
        created = float((acc.get("created_at") if isinstance(acc, dict)
                         else getattr(acc, "created_at", 0)) or 0)
    except Exception:
        return None
    if created <= 0:
        return None
    return max(0.0, (now - created) / _DAY)


def check(
    platform: str,
    account_id: str,
    *,
    origin: str = "auto",
    chat_key: str = "",
    config: Any = None,
    registry: Any = None,
    age_days: Optional[float] = None,
    now: Optional[float] = None,
    record: bool = True,
    store: Optional[SendRateStore] = None,
) -> Dict[str, Any]:
    """发送前判定。返回 ``{allowed, reason, origin, used, cap, age_days, in_warmup}``。

    ``record=True``（真发送路径）：放行即记一条计数；拦截记审计 ``blocked``。
    ``record=False``（预判 / 看板）：只算不写。绝不抛（异常 → 放行 + warning）。
    """
    p, a = str(platform or ""), str(account_id or "default")
    res: Dict[str, Any] = {"allowed": True, "reason": "", "origin": "", "used": 0, "cap": 0,
                           "age_days": None, "in_warmup": False}
    if not enabled(config):
        res["reason"] = "disabled"
        return res
    try:
        ts = float(now if now is not None else time.time())
        g = gate_cfg(config)
        org = classify_origin(origin, platform=p, account_id=a, chat_key=chat_key, config=config)
        res["origin"] = org
        st = store or get_store(config)
        if org == ORIGIN_PHONE:
            if record:
                st.record(p, a, org, chat_key, ts=ts)
            return res
        since = ts - _DAY
        # ② 脚本流量日上限（任何天龄）
        if org == ORIGIN_SCRIPT:
            try:
                scap = int(g.get("script_daily_cap", DEFAULTS["script_daily_cap"]))
            except Exception:
                scap = int(DEFAULTS["script_daily_cap"])
            if scap >= 0:
                used_s = st.count(p, a, since=since, origins=(ORIGIN_SCRIPT,))
                if used_s >= scap:
                    res.update(allowed=False, reason=REASON_SCRIPT, used=used_s, cap=scap)
        # ① 预热期硬上限（ai / agent / script 同一个桶）
        if res["allowed"] and _truthy(g.get("warmup_block"), True):
            age = age_days if age_days is not None else _age_days(p, a, registry, ts)
            res["age_days"] = age
            wp = _warmup_params(config)
            if age is not None and age < float(wp["ramp_days"]):
                from src.skills.account_health import warmup_cap
                cap = warmup_cap(age, wp["target_cap"], start_cap=wp["start_cap"],
                                 ramp_days=wp["ramp_days"])
                used = st.count(p, a, since=since, origins=SYSTEM_ORIGINS)
                res.update(in_warmup=True, used=used, cap=cap)
                if used >= cap:
                    res.update(allowed=False, reason=REASON_WARMUP)
        if record:
            if res["allowed"]:
                st.record(p, a, org, chat_key, ts=ts)
            else:
                st.audit(ts=ts, platform=p, account_id=a, origin=org, chat_key=chat_key,
                         action="blocked", reason=res["reason"], used=res["used"],
                         cap=res["cap"], age_days=res.get("age_days"))
                logger.warning(
                    "[send-rate-gate] 拦截 %s:%s → peer=%s origin=%s reason=%s used=%s cap=%s age=%s",
                    p, a, chat_key, org, res["reason"], res["used"], res["cap"],
                    None if res.get("age_days") is None else round(float(res["age_days"]), 1))
        return res
    except Exception:
        logger.warning("[send-rate-gate] 判定异常（放行）%s:%s", p, a, exc_info=True)
        return {"allowed": True, "reason": "error", "origin": res.get("origin") or "",
                "used": 0, "cap": 0, "age_days": None, "in_warmup": False}


def frees_at(platform: str, account_id: str, *, reason: str, cap: int, config: Any = None,
             now: Optional[float] = None, store: Optional[SendRateStore] = None) -> Optional[float]:
    """被本闸拦下后**预计恢复时刻**（epoch 秒）：滚动 24h 窗里最早的那几条过期、计数回落到
    上限以下的时刻。预热期上限还会随号龄增长提前放宽，所以这是「最晚」口径。算不出 → None。"""
    try:
        c = int(cap)
        if c <= 0:
            return None
        ts = float(now if now is not None else time.time())
        origins = (ORIGIN_SCRIPT,) if str(reason) == REASON_SCRIPT else SYSTEM_ORIGINS
        st = store or get_store(config)
        times = st.times(str(platform or ""), str(account_id or "default"), since=ts - _DAY,
                         origins=origins)
        idx = len(times) - c
        if idx < 0:
            return None
        return float(times[idx]) + _DAY
    except Exception:
        logger.debug("[send-rate-gate] frees_at 计算失败", exc_info=True)
        return None


def block_info(platform: str, account_id: str, *, origin: str = "manual", chat_key: str = "",
               config: Any = None, registry: Any = None, now: Optional[float] = None,
               store: Optional[SendRateStore] = None) -> Optional[Dict[str, Any]]:
    """前端 / 409 用：当前是否被本闸拦 + ``{reason, used, cap, frees_at, origin}``。只读、绝不抛。"""
    try:
        r = check(platform, account_id, origin=origin, chat_key=chat_key, config=config,
                  registry=registry, now=now, record=False, store=store)
        if r.get("allowed", True):
            return None
        return {
            "reason": str(r.get("reason") or ""),
            "origin": str(r.get("origin") or ""),
            "used": int(r.get("used") or 0),
            "cap": int(r.get("cap") or 0),
            "frees_at": frees_at(platform, account_id, reason=str(r.get("reason") or ""),
                                 cap=int(r.get("cap") or 0), config=config, now=now, store=store),
        }
    except Exception:
        return None


def record_external(platform: str, account_id: str, *, origin: str = ORIGIN_PHONE,
                    chat_key: str = "", config: Any = None, ts: Optional[float] = None) -> None:
    """手机端 / 外部发送（入库时识别出不是本系统发的）单独计数；不拦。绝不抛。"""
    try:
        get_store(config).record(str(platform or ""), str(account_id or "default"),
                                 origin_to_sent_by(origin), chat_key, ts=ts)
    except Exception:
        logger.debug("[send-rate-gate] record_external 失败", exc_info=True)


def snapshot(platform: str, account_id: str, *, config: Any = None, registry: Any = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    """看板用：滚动 24h 各来源条数 + 预热建议上限。只读。"""
    ts = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"by_origin": {}, "cap": None, "age_days": None, "in_warmup": False}
    try:
        st = get_store(config)
        out["by_origin"] = st.counts_by_origin(str(platform or ""), str(account_id or "default"),
                                               since=ts - _DAY)
        age = _age_days(str(platform or ""), str(account_id or "default"), registry, ts)
        out["age_days"] = age
        wp = _warmup_params(config)
        if age is not None and age < float(wp["ramp_days"]):
            from src.skills.account_health import warmup_cap
            out["cap"] = warmup_cap(age, wp["target_cap"], start_cap=wp["start_cap"],
                                    ramp_days=wp["ramp_days"])
            out["in_warmup"] = True
        out["script_daily_cap"] = int(gate_cfg(config).get("script_daily_cap") or 0)
    except Exception:
        logger.debug("[send-rate-gate] snapshot 失败", exc_info=True)
    return out


__all__ = [
    "ORIGIN_AI", "ORIGIN_AGENT", "ORIGIN_SCRIPT", "ORIGIN_PHONE", "REASON_WARMUP", "REASON_SCRIPT",
    "gate_cfg", "enabled", "classify_origin", "origin_to_sent_by", "in_script_scope",
    "set_script_scope", "reset_script_scope", "mark_script_from_request", "SendRateStore",
    "get_store", "reset_for_tests", "check", "record_external", "snapshot",
]
