"""成本账本（2026-09-08 成本对账 P0/P1）：LLM 用量落盘 + 账单真值 + 对账结果。

为什么要单独一张库：``llm_cost`` 是进程内计数器，重启即清零（0908 当天三次重启，
看板归零三次）；``token_ledger.db`` 记的是**授权配额单位**（ai_reply=10 虚拟 token/条），
不是真实 token 与金额。对账需要的是「按天、按厂商、按模型、按用途」的真实用量，
以及一处能放**外部真值**（硅基账单 CSV / 人工填的当日金额与余额）的地方——硅基
``/v1/user/info`` 已于 2026-08-14 下线，没有 API 可拉，真值只能这样进来。

表：
  llm_usage(day, provider, model, purpose, account_id) → calls / tokens / cost / suspected
  bill_truth(day, provider) → 账单金额 / 余额 / 来源（csv|manual）
  recharges(id) → 充值流水（余额推算用）
  recon_result(day, provider) → 对账结论 + 原因（JSON）

写入模式：写穿（每笔 UPSERT，一天百来笔，SQLite 毫无压力），不做内存缓冲——缓冲
意味着崩溃丢账，而这张账本存在的意义就是不丢。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.cost_ledger")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS llm_usage (
        day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        purpose TEXT NOT NULL, account_id TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        suspected_calls INTEGER NOT NULL DEFAULT 0,
        suspected_cost REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (day, provider, model, purpose, account_id))""",
    """CREATE TABLE IF NOT EXISTS bill_truth (
        day TEXT NOT NULL, provider TEXT NOT NULL,
        amount REAL NOT NULL, balance REAL,
        source TEXT NOT NULL DEFAULT 'manual', note TEXT NOT NULL DEFAULT '',
        imported_at REAL NOT NULL,
        PRIMARY KEY (day, provider))""",
    """CREATE TABLE IF NOT EXISTS recharges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, provider TEXT NOT NULL, amount REAL NOT NULL,
        note TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS recon_result (
        day TEXT NOT NULL, provider TEXT NOT NULL,
        internal REAL NOT NULL, truth REAL, diff_pct REAL,
        verdict TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        PRIMARY KEY (day, provider))""",
)


def day_of(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


class CostLedger:
    """SQLite 成本账本。所有方法线程安全、绝不抛（读失败回空、写失败只记 debug）。"""

    def __init__(self, path: str) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        with self._conn:
            for ddl in _SCHEMA:
                self._conn.execute(ddl)

    @property
    def path(self) -> str:
        return self._path

    # ── 用量 ──────────────────────────────────────────────────────────────
    def record_usage(self, rec: Dict[str, Any]) -> None:
        """``llm_cost`` sink：一笔调用 → 按 (day, provider, model, purpose, account) 累加。"""
        try:
            ts = float(rec.get("ts") or time.time())
            day = day_of(ts)
            provider = str(rec.get("provider") or "unknown")
            model = str(rec.get("model") or "unknown")
            purpose = str(rec.get("purpose") or "unknown")
            account = str(rec.get("account_id") or "default")
            pt = int(rec.get("prompt_tokens") or 0)
            ct = int(rec.get("completion_tokens") or 0)
            cost = float(rec.get("cost") or 0.0)
            sus = 1 if rec.get("suspected") else 0
            with self._lock, self._conn:
                self._conn.execute(
                    """INSERT INTO llm_usage
                       (day, provider, model, purpose, account_id, calls, prompt_tokens,
                        completion_tokens, cost, suspected_calls, suspected_cost, updated_at)
                       VALUES (?,?,?,?,?,1,?,?,?,?,?,?)
                       ON CONFLICT(day, provider, model, purpose, account_id) DO UPDATE SET
                         calls = calls + 1,
                         prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                         completion_tokens = completion_tokens + excluded.completion_tokens,
                         cost = cost + excluded.cost,
                         suspected_calls = suspected_calls + excluded.suspected_calls,
                         suspected_cost = suspected_cost + excluded.suspected_cost,
                         updated_at = excluded.updated_at""",
                    (day, provider, model, purpose, account, pt, ct, cost, sus,
                     cost if sus else 0.0, ts),
                )
        except Exception:
            logger.debug("[cost_ledger] record_usage 失败（已忽略）", exc_info=True)

    def usage_rows(self, day: str, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                if provider:
                    cur = self._conn.execute(
                        "SELECT * FROM llm_usage WHERE day=? AND provider=? ORDER BY cost DESC",
                        (day, provider))
                else:
                    cur = self._conn.execute(
                        "SELECT * FROM llm_usage WHERE day=? ORDER BY cost DESC", (day,))
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def day_summary(self, day: str, provider: Optional[str] = None) -> Dict[str, Any]:
        """一天的汇总：总额 + 按用途 / 按模型分桶。"""
        rows = self.usage_rows(day, provider)
        out: Dict[str, Any] = {
            "day": day, "provider": provider or "", "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cost": 0.0, "suspected_calls": 0,
            "suspected_cost": 0.0, "by_purpose": {}, "by_model": {}, "by_provider": {},
        }
        for r in rows:
            for k in ("calls", "prompt_tokens", "completion_tokens", "suspected_calls"):
                out[k] += int(r.get(k) or 0)
            out["cost"] += float(r.get("cost") or 0.0)
            out["suspected_cost"] += float(r.get("suspected_cost") or 0.0)
            for bucket, key in (("by_purpose", r["purpose"]), ("by_model", r["model"]),
                                ("by_provider", r["provider"])):
                b = out[bucket].setdefault(key, {"calls": 0, "cost": 0.0, "tokens": 0})
                b["calls"] += int(r.get("calls") or 0)
                b["cost"] += float(r.get("cost") or 0.0)
                b["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
        out["cost"] = round(out["cost"], 4)
        out["suspected_cost"] = round(out["suspected_cost"], 4)
        return out

    def daily_costs(self, days: int = 14, provider: Optional[str] = None,
                    end_day: Optional[str] = None) -> List[Dict[str, Any]]:
        """最近 N 天每日成本（含 0 元日，便于画柱状图 / 算基线）。"""
        end = end_day or day_of()
        try:
            end_ts = time.mktime(time.strptime(end, "%Y-%m-%d"))
        except Exception:
            end_ts = time.time()
        wanted = [day_of(end_ts - i * 86400) for i in range(max(1, int(days)))]
        wanted.reverse()
        got: Dict[str, Dict[str, Any]] = {}
        try:
            with self._lock:
                q = ("SELECT day, SUM(cost) c, SUM(calls) n, SUM(suspected_cost) s "
                     "FROM llm_usage WHERE day BETWEEN ? AND ?")
                args: List[Any] = [wanted[0], wanted[-1]]
                if provider:
                    q += " AND provider=?"
                    args.append(provider)
                q += " GROUP BY day"
                for r in self._conn.execute(q, args).fetchall():
                    got[r["day"]] = {"cost": float(r["c"] or 0), "calls": int(r["n"] or 0),
                                     "suspected_cost": float(r["s"] or 0)}
        except Exception:
            pass
        return [{"day": d, **got.get(d, {"cost": 0.0, "calls": 0, "suspected_cost": 0.0})}
                for d in wanted]

    def providers(self) -> List[str]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT DISTINCT provider FROM llm_usage UNION "
                    "SELECT DISTINCT provider FROM bill_truth").fetchall()
            return sorted({str(r[0]) for r in rows if r[0]})
        except Exception:
            return []

    # ── 真值 ──────────────────────────────────────────────────────────────
    def set_truth(self, day: str, provider: str, amount: float, *,
                  balance: Optional[float] = None, source: str = "manual",
                  note: str = "", now: Optional[float] = None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """INSERT INTO bill_truth (day, provider, amount, balance, source, note, imported_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(day, provider) DO UPDATE SET
                         amount=excluded.amount,
                         balance=COALESCE(excluded.balance, bill_truth.balance),
                         source=excluded.source, note=excluded.note,
                         imported_at=excluded.imported_at""",
                    (str(day), str(provider), float(amount),
                     None if balance is None else float(balance),
                     str(source or "manual"), str(note or "")[:200],
                     float(now if now is not None else time.time())),
                )
        except Exception:
            logger.debug("[cost_ledger] set_truth 失败（已忽略）", exc_info=True)

    def get_truth(self, day: str, provider: str) -> Optional[Dict[str, Any]]:
        try:
            with self._lock:
                r = self._conn.execute(
                    "SELECT * FROM bill_truth WHERE day=? AND provider=?",
                    (day, provider)).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    def latest_truth(self, provider: str) -> Optional[Dict[str, Any]]:
        try:
            with self._lock:
                r = self._conn.execute(
                    "SELECT * FROM bill_truth WHERE provider=? ORDER BY day DESC LIMIT 1",
                    (provider,)).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    def latest_balance(self, provider: str) -> Optional[Dict[str, Any]]:
        """最近一次**人填/导入的余额**（不是推算值）。"""
        try:
            with self._lock:
                r = self._conn.execute(
                    "SELECT day, balance, imported_at FROM bill_truth "
                    "WHERE provider=? AND balance IS NOT NULL ORDER BY day DESC LIMIT 1",
                    (provider,)).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    def truth_rows(self, provider: str, days: int = 30) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM bill_truth WHERE provider=? ORDER BY day DESC LIMIT ?",
                    (provider, int(days))).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def add_recharge(self, provider: str, amount: float, *, note: str = "",
                     ts: Optional[float] = None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO recharges (ts, provider, amount, note) VALUES (?,?,?,?)",
                    (float(ts if ts is not None else time.time()), str(provider),
                     float(amount), str(note or "")[:200]))
        except Exception:
            logger.debug("[cost_ledger] add_recharge 失败（已忽略）", exc_info=True)

    def recharges(self, provider: str) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM recharges WHERE provider=? ORDER BY ts DESC", (provider,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── 对账结果 ─────────────────────────────────────────────────────────
    def save_recon(self, day: str, provider: str, *, internal: float,
                   truth: Optional[float], diff_pct: Optional[float], verdict: str,
                   reasons: Iterable[Dict[str, Any]], summary: str = "",
                   now: Optional[float] = None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """INSERT INTO recon_result
                       (day, provider, internal, truth, diff_pct, verdict, reasons, summary, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(day, provider) DO UPDATE SET
                         internal=excluded.internal, truth=excluded.truth,
                         diff_pct=excluded.diff_pct, verdict=excluded.verdict,
                         reasons=excluded.reasons, summary=excluded.summary,
                         created_at=excluded.created_at""",
                    (str(day), str(provider), float(internal),
                     None if truth is None else float(truth),
                     None if diff_pct is None else float(diff_pct),
                     str(verdict), json.dumps(list(reasons), ensure_ascii=False),
                     str(summary or ""), float(now if now is not None else time.time())),
                )
        except Exception:
            logger.debug("[cost_ledger] save_recon 失败（已忽略）", exc_info=True)

    def recon_rows(self, provider: Optional[str] = None, days: int = 30) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                if provider:
                    rows = self._conn.execute(
                        "SELECT * FROM recon_result WHERE provider=? ORDER BY day DESC LIMIT ?",
                        (provider, int(days))).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM recon_result ORDER BY day DESC LIMIT ?",
                        (int(days),)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["reasons"] = json.loads(d.get("reasons") or "[]")
                except Exception:
                    d["reasons"] = []
                out.append(d)
            return out
        except Exception:
            return []

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass


# ── 硅基账单 CSV 解析 ─────────────────────────────────────────────────────────
# 硅基「费用明细」导出列（2026-09 实测）：计费周期 / 费用流水ID / 费用发生时间 / 计费项 /
# 计费项原始用量 / 资源包抵扣用量 / 资源包抵扣后余量 / 用量单位 / 计费项单价 / 计费金额。
# 列名用模糊匹配（含关键字即认），厂商改个字不至于整张表读不进。
_COL_TIME = ("费用发生时间", "发生时间", "计费周期", "时间", "time", "date")
_COL_AMOUNT = ("计费金额", "账单金额", "金额", "amount", "cost")
_COL_ITEM = ("计费项", "产品", "模型", "item", "model", "product")
_COL_USAGE = ("原始用量", "用量", "usage", "quantity")

_ITEM_SUFFIX_RE = re.compile(
    r"\.(online|offline)?\.?(input|output|prompt|completion)[-_]?tokens?$", re.IGNORECASE)


def _pick_col(header: List[str], cands: Tuple[str, ...]) -> Optional[int]:
    low = [h.strip().lower() for h in header]
    for c in cands:
        for i, h in enumerate(low):
            if c.lower() in h:
                return i
    return None


def _num(s: Any) -> float:
    t = str(s or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    try:
        return float(t)
    except ValueError:
        return 0.0


def model_from_bill_item(item: str) -> str:
    """``deepseek-ai/deepseek-v3.2.online.input-tokens`` → ``deepseek-ai/deepseek-v3.2``。"""
    s = str(item or "").strip()
    s = _ITEM_SUFFIX_RE.sub("", s)
    return s


def parse_bill_csv(text: str, *, provider: str = "siliconflow") -> Dict[str, Any]:
    """账单 CSV/TSV 文本 → 按天汇总 + 按天×模型明细。

    返回 ``{ok, provider, days: {day: amount}, by_model: {day: {model: amount}},
    rows, skipped, error}``。任何列缺失都在 ``error`` 里说人话，绝不抛。
    """
    out: Dict[str, Any] = {"ok": False, "provider": provider, "days": {}, "by_model": {},
                           "rows": 0, "skipped": 0, "error": ""}
    raw = str(text or "").lstrip("\ufeff")
    if not raw.strip():
        out["error"] = "文件为空"
        return out
    sample = raw[:4096]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(raw), delimiter=delim)
    header: Optional[List[str]] = None
    ti = ai = ii = None
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        if header is None:
            # 允许前几行是标题/说明：找到同时含时间列与金额列的那一行当表头
            ti = _pick_col(row, _COL_TIME)
            ai = _pick_col(row, _COL_AMOUNT)
            if ti is not None and ai is not None:
                header = row
                ii = _pick_col(row, _COL_ITEM)
            continue
        if ti is None or ai is None or max(ti, ai) >= len(row):
            out["skipped"] += 1
            continue
        tcell = str(row[ti]).strip()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", tcell)
        if not m:
            out["skipped"] += 1
            continue
        day = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        amount = _num(row[ai])
        out["days"][day] = round(out["days"].get(day, 0.0) + amount, 6)
        if ii is not None and ii < len(row):
            model = model_from_bill_item(row[ii]) or "unknown"
            bm = out["by_model"].setdefault(day, {})
            bm[model] = round(bm.get(model, 0.0) + amount, 6)
        out["rows"] += 1
    if header is None:
        out["error"] = "没找到表头：需要同时包含「费用发生时间/计费周期」和「计费金额」两列"
        return out
    if not out["rows"]:
        out["error"] = "表头认出来了，但没有一行有效数据"
        return out
    out["ok"] = True
    return out


# ── 单例 ─────────────────────────────────────────────────────────────────────
_LEDGER: Optional[CostLedger] = None
_LOCK = threading.Lock()
_PATH_OVERRIDE: Optional[str] = None


def _default_path() -> str:
    from src.licensing.data_paths import data_file
    return data_file("cost_ledger.db")


def configure_cost_ledger(path: Any = None, *, ledger: Optional[CostLedger] = None) -> Optional[CostLedger]:
    """启动期/测试期装配：指定库路径或直接注入实例；幂等。"""
    global _LEDGER, _PATH_OVERRIDE
    with _LOCK:
        if ledger is not None:
            _LEDGER = ledger
        if path is not None:
            _PATH_OVERRIDE = str(path)
            if ledger is None:
                _LEDGER = None
        return _LEDGER


def get_cost_ledger() -> Optional[CostLedger]:
    """取账本单例（建库失败 → None，调用方按无账本处理）。"""
    global _LEDGER
    with _LOCK:
        if _LEDGER is None:
            try:
                _LEDGER = CostLedger(_PATH_OVERRIDE or _default_path())
            except Exception:
                logger.warning("[cost_ledger] 建库失败，成本落盘禁用", exc_info=True)
                return None
        return _LEDGER


def reset_cost_ledger() -> None:
    """测试钩子。"""
    global _LEDGER, _PATH_OVERRIDE
    with _LOCK:
        if _LEDGER is not None:
            _LEDGER.close()
        _LEDGER = None
        _PATH_OVERRIDE = None


def attach_ledger_sink() -> bool:
    """把账本挂成 ``llm_cost`` 的 sink（启动期调用一次）。返回是否成功。"""
    ledger = get_cost_ledger()
    if ledger is None:
        return False
    from src.ai.llm_cost import get_llm_cost
    get_llm_cost().set_sink(ledger.record_usage)
    return True


__all__ = [
    "CostLedger", "get_cost_ledger", "configure_cost_ledger", "reset_cost_ledger",
    "attach_ledger_sink", "parse_bill_csv", "model_from_bill_item", "day_of",
]
