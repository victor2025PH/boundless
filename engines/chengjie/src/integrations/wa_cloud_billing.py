# -*- coding: utf-8 -*-
"""WhatsApp Cloud API 按条计费台账（Meta 费用单列，2026-10-08 智聊 DM 接入）。

Meta 2025-07-01 起对模板消息按条计费（PMP，per-message pricing）：每条消息的 status
webhook（sent/delivered/read）带 ``pricing{billable, pricing_model, category, type}``；
客服窗口内的 service / utility 免费（``type=free_customer_service``），入口免费
（``free_entry_point``）。旧的会话计费（CBP）在 ``conversation{id, origin.type}`` 里。

本台账：
- 每条消息（PMP 按 message id；CBP 按 conversation id）**只记一行**，后续 status 只刷新
  billable / 状态，不重复计费；``failed`` 状态的消息不计费。
- 单价来自配置 ``whatsapp_cloud.pricing``（Meta 价格按国家 × 类别变动，**仓内不写死价格**）：

      pricing:
        currency: USD
        rates:
          default: {marketing: 0.0, utility: 0.0, authentication: 0.0, service: 0.0}
          "86":    {marketing: 0.0, utility: 0.0, authentication: 0.0}   # 按收件人国家码前缀覆盖

  未配置单价的计费消息记为 ``unpriced``（数量照记，金额不猜）。
- 不存收件人号码明文：只存国家码前缀 + 号码 sha256 前 16 位（可按客户聚合、不可还原）。
- 库文件 data 区 ``wa_cloud_billing.db``（与其它台账同手法）；全部函数绝不抛。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_NAME = "wa_cloud_billing.db"
CATEGORIES = ("marketing", "utility", "authentication", "authentication_international",
              "service", "marketing_lite")

_DDL = """
CREATE TABLE IF NOT EXISTS wa_billing (
    bill_key TEXT PRIMARY KEY,          -- PMP: msg:<wamid>；CBP: conv:<conversation id>
    msg_id TEXT,
    phone_number_id TEXT,
    recipient_hash TEXT,
    country_code TEXT,
    category TEXT,
    pricing_model TEXT,
    pricing_type TEXT,
    billable INTEGER,
    status TEXT,
    unit_price REAL,                    -- NULL = 未配置单价
    currency TEXT,
    first_ts REAL,
    last_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_wab_ts ON wa_billing(first_ts);
"""

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: str = ""
_path_override: Optional[str] = None


def set_db_path_for_tests(path: Optional[str]) -> None:
    """测试用：指定库路径（None = :memory:）。会关闭当前连接。"""
    global _path_override, _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                logger.debug("[wa-billing] close 失败", exc_info=True)
        _conn, _conn_path = None, ""
        _path_override = path if path is not None else ":memory:"


def _db_path() -> str:
    if _path_override is not None:
        return _path_override
    try:
        from src.licensing.data_paths import data_file
        return str(data_file(DB_NAME))
    except Exception:
        return str(Path("config") / DB_NAME)


def _get_conn() -> sqlite3.Connection:
    global _conn, _conn_path
    path = _db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    c.commit()
    _conn, _conn_path = c, path
    return c


def _recipient_hash(recipient: str) -> str:
    r = "".join(ch for ch in str(recipient or "") if ch.isdigit())
    return hashlib.sha256(r.encode("utf-8")).hexdigest()[:16] if r else ""


def _country_prefix(recipient: str, rate_keys: List[str]) -> str:
    """收件人号码 → 配置里最长匹配的国家码前缀；无配置命中 → 号码前 2 位（仅作聚合维度）。"""
    digits = "".join(ch for ch in str(recipient or "") if ch.isdigit())
    best = ""
    for k in rate_keys:
        ks = str(k)
        if ks.isdigit() and digits.startswith(ks) and len(ks) > len(best):
            best = ks
    return best or digits[:2]


def resolve_unit_price(pricing_cfg: Dict[str, Any], country: str, category: str) -> Optional[float]:
    rates = (pricing_cfg or {}).get("rates") or {}
    if not isinstance(rates, dict):
        return None
    for key in (country, "default"):
        row = rates.get(key) if key else None
        if isinstance(row, dict) and category in row:
            try:
                return float(row[category])
            except (TypeError, ValueError):
                return None
    return None


def record_status(status: Dict[str, Any], *, phone_number_id: str = "",
                  pricing_cfg: Optional[Dict[str, Any]] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """吃一条 webhook ``statuses[]`` 元素；有 pricing/conversation 才落账。返回 {recorded, new, bill_key}。"""
    out: Dict[str, Any] = {"recorded": False, "new": False, "bill_key": ""}
    try:
        st = status or {}
        pricing = st.get("pricing") if isinstance(st.get("pricing"), dict) else {}
        conv = st.get("conversation") if isinstance(st.get("conversation"), dict) else {}
        state = str(st.get("status") or "").lower()
        if not pricing and not conv:
            return out
        msg_id = str(st.get("id") or "")
        model = str(pricing.get("pricing_model") or ("CBP" if conv else "")).upper()
        category = str(pricing.get("category")
                       or ((conv.get("origin") or {}).get("type") if conv else "") or "unknown").lower()
        ptype = str(pricing.get("type") or "")
        billable = bool(pricing.get("billable", bool(conv)))
        if state == "failed":
            billable = False
        if model == "CBP" and conv.get("id"):
            bill_key = f"conv:{conv.get('id')}"
        elif msg_id:
            bill_key = f"msg:{msg_id}"
        else:
            return out
        cfg = pricing_cfg or {}
        rate_keys = list(((cfg.get("rates") or {}) if isinstance(cfg.get("rates"), dict) else {}).keys())
        recipient = str(st.get("recipient_id") or "")
        country = _country_prefix(recipient, rate_keys)
        unit = resolve_unit_price(cfg, country, category)
        currency = str(cfg.get("currency") or "USD")
        ts = float(now if now is not None else time.time())
        with _lock:
            c = _get_conn()
            row = c.execute("SELECT billable FROM wa_billing WHERE bill_key=?", (bill_key,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO wa_billing(bill_key,msg_id,phone_number_id,recipient_hash,country_code,"
                    "category,pricing_model,pricing_type,billable,status,unit_price,currency,first_ts,last_ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bill_key, msg_id, str(phone_number_id or ""), _recipient_hash(recipient), country,
                     category, model, ptype, 1 if billable else 0, state, unit, currency, ts, ts))
                out["new"] = True
            else:
                # failed 一律撤销计费；否则 billable 只升不降（Meta 先 sent 后 delivered 都带同一 pricing）
                new_b = 0 if state == "failed" else max(int(row["billable"] or 0), 1 if billable else 0)
                c.execute("UPDATE wa_billing SET billable=?, status=?, last_ts=? WHERE bill_key=?",
                          (new_b, state, ts, bill_key))
            c.commit()
        out.update(recorded=True, bill_key=bill_key)
    except Exception:
        logger.debug("[wa-billing] record_status 失败（忽略）", exc_info=True)
    return out


def summary(*, days: int = 30, phone_number_id: str = "",
            now: Optional[float] = None) -> Dict[str, Any]:
    """费用汇总（只读）：总条数 / 计费条数 / 估算金额 / 未定价条数 / 按类别 / 按日（近 7 天）。"""
    base: Dict[str, Any] = {
        "days": int(days), "currency": "", "messages": 0, "billable": 0, "free": 0,
        "unpriced_billable": 0, "est_cost": 0.0, "by_category": {}, "by_day": [],
    }
    try:
        t_now = float(now if now is not None else time.time())
        since = t_now - max(1, int(days)) * 86400
        q = ("SELECT category, billable, unit_price, currency, first_ts FROM wa_billing"
             " WHERE first_ts>=?")
        args: List[Any] = [since]
        if phone_number_id:
            q += " AND phone_number_id=?"
            args.append(str(phone_number_id))
        with _lock:
            rows = _get_conn().execute(q, args).fetchall()
        by_cat: Dict[str, Dict[str, Any]] = {}
        by_day: Dict[str, Dict[str, Any]] = {}
        currencies = set()
        for r in rows:
            cat = str(r["category"] or "unknown")
            b = int(r["billable"] or 0)
            unit = r["unit_price"]
            cost = float(unit) if (b and unit is not None) else 0.0
            if r["currency"]:
                currencies.add(str(r["currency"]))
            base["messages"] += 1
            base["billable"] += b
            base["free"] += 0 if b else 1
            if b and unit is None:
                base["unpriced_billable"] += 1
            base["est_cost"] += cost
            cr = by_cat.setdefault(cat, {"messages": 0, "billable": 0, "est_cost": 0.0})
            cr["messages"] += 1
            cr["billable"] += b
            cr["est_cost"] = round(cr["est_cost"] + cost, 6)
            day = time.strftime("%Y-%m-%d", time.localtime(float(r["first_ts"] or 0)))
            if float(r["first_ts"] or 0) >= t_now - 7 * 86400:
                dr = by_day.setdefault(day, {"day": day, "billable": 0, "est_cost": 0.0})
                dr["billable"] += b
                dr["est_cost"] = round(dr["est_cost"] + cost, 6)
        base["est_cost"] = round(base["est_cost"], 6)
        base["by_category"] = by_cat
        base["by_day"] = [by_day[k] for k in sorted(by_day)]
        base["currency"] = ",".join(sorted(currencies)) if currencies else ""
    except Exception:
        logger.debug("[wa-billing] summary 失败", exc_info=True)
        base["error"] = "unavailable"
    return base


__all__ = ["DB_NAME", "CATEGORIES", "record_status", "summary", "resolve_unit_price",
           "set_db_path_for_tests"]
