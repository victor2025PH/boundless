# -*- coding: utf-8 -*-
"""账号接入漏斗验收周报 CLI（2026-08-02，只读）。

「接入成功率在收敛还是回潮 / checkpoint 是不是这周突然增多」从翻看板变成一条命令：

    python -m scripts.login_funnel_review [--data-root PATH] [--days 7]
                                         [--json] [--out-jsonl FILE]
                                         [--trend [FILE]]

读数（全部只读——``login_funnel_trend.db`` mode=ro，对活体零写风险）：
- 近 N 天 started / authorized / failed + 成功率；
- 失败原因 Top（checkpoint / two_factor / session_timeout…）；
- ``--trend``：周批 JSONL 环比 + 自动判词。

多实例机自动逐根出报告（``scripts/_data_root.py`` 契约）。
未开 ``ops.login_funnel_trend`` 或库不存在 → 报告 ``enabled=false``，不装作有数据。
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ro_conn(db_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _day_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def load_daily(db_path: Path, *, days: int = 7,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
    """读趋势库近 N 天（升序，缺日补零）。库不存在 → []。"""
    n = max(1, min(int(days or 7), 90))
    base = now if now is not None else time.time()
    day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
    if not db_path.is_file():
        return [{"day": d, "started": 0, "authorized": 0, "failed": 0,
                 "by_reason": {}} for d in day_keys]
    conn = _ro_conn(db_path)
    if conn is None:
        return [{"day": d, "started": 0, "authorized": 0, "failed": 0,
                 "by_reason": {}} for d in day_keys]
    totals: Dict[str, Dict[str, int]] = {}
    reasons: Dict[str, Dict[str, int]] = {}
    try:
        for r in conn.execute(
            "SELECT day, started, authorized, failed "
            "FROM login_funnel_trend_daily WHERE day >= ? ORDER BY day",
            (day_keys[0],),
        ).fetchall():
            totals[str(r["day"])] = {
                "started": int(r["started"] or 0),
                "authorized": int(r["authorized"] or 0),
                "failed": int(r["failed"] or 0),
            }
        for r in conn.execute(
            "SELECT day, reason, n FROM login_funnel_reason_daily "
            "WHERE day >= ? ORDER BY day",
            (day_keys[0],),
        ).fetchall():
            reasons.setdefault(str(r["day"]), {})[str(r["reason"])] = int(r["n"] or 0)
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for day in day_keys:
        t = totals.get(day) or {"started": 0, "authorized": 0, "failed": 0}
        out.append({
            "day": day,
            "started": t["started"],
            "authorized": t["authorized"],
            "failed": t["failed"],
            "by_reason": dict(sorted(reasons.get(day, {}).items())),
        })
    return out


def summarize(days_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """纯函数：日序列 → 窗内汇总 + 成功率 + 原因 Top。"""
    started = sum(int(r.get("started") or 0) for r in days_rows)
    authorized = sum(int(r.get("authorized") or 0) for r in days_rows)
    failed = sum(int(r.get("failed") or 0) for r in days_rows)
    reason_tot: Dict[str, int] = {}
    for r in days_rows:
        for k, v in (r.get("by_reason") or {}).items():
            reason_tot[str(k)] = reason_tot.get(str(k), 0) + int(v or 0)
    top = sorted(reason_tot.items(), key=lambda kv: (-kv[1], kv[0]))
    rate = round(authorized / started, 3) if started else None
    return {
        "started": started,
        "authorized": authorized,
        "failed": failed,
        "success_rate": rate,
        "reasons": dict(top),
        "top_reasons": [{"reason": k, "n": v} for k, v in top[:5]],
    }


def verdicts(cur: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> List[str]:
    """纯函数：本窗 vs 上一窗 → 可读判词（只减噪音、不造旋钮）。"""
    out: List[str] = []
    s = int(cur.get("started") or 0)
    if s == 0:
        out.append("本窗无接入尝试（可能无人接号，或趋势落库未开）")
        return out
    rate = cur.get("success_rate")
    if rate is None:
        return out
    if rate < 0.3 and s >= 3:
        out.append(f"⚠ 成功率偏低（{rate*100:.0f}%，n={s}）——查 sidecar / 凭据 / 风控")
    reasons = cur.get("reasons") or {}
    ck = int(reasons.get("checkpoint") or 0)
    failed_n = int(cur.get("failed") or 0)
    if ck >= 2 and failed_n and ck >= max(2, int(0.4 * failed_n)):
        out.append(f"⚠ 失败以 checkpoint 为主（{ck}）——账号风控/申诉，不是代码链路")
    tf = int(reasons.get("two_factor") or 0)
    if tf and tf >= 2:
        out.append(f"两步验证未完成 {tf} 次——运维要到服务器窗口输码")
    st = int(reasons.get("session_timeout") or 0)
    if st and st >= 2:
        out.append(f"会话超时 {st} 次——核对托管 TTL / 人是否在窗口期内完成")
    if prev and int(prev.get("started") or 0) >= 3 and prev.get("success_rate") is not None:
        pr = float(prev["success_rate"])
        if rate + 0.15 < pr:
            out.append(f"⚠ 成功率回落（{pr*100:.0f}%→{rate*100:.0f}%）")
        elif rate > pr + 0.15:
            out.append(f"成功率回升（{pr*100:.0f}%→{rate*100:.0f}%）")
        pck = int((prev.get("reasons") or {}).get("checkpoint") or 0)
        if ck > pck + 1:
            out.append(f"⚠ checkpoint 增多（{pck}→{ck}）")
    if not out:
        if s < 3:
            out.append(
                f"样本尚少（n={s}，成功率 {rate*100:.0f}%）——攒够几天再读趋势更稳")
        else:
            out.append(
                f"窗内成功率 {rate*100:.0f}%（{int(cur.get('authorized') or 0)}/{s}），"
                f"未见集中归因")
    return out


def collect_review(data_root: Path, *, days: float = 7.0,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """单数据根验收快照。"""
    root = Path(data_root)
    db = root / "config" / "login_funnel_trend.db"
    enabled = db.is_file()
    span = max(1, int(days or 7))
    rows = load_daily(db, days=span, now=now) if enabled else []
    # 上一窗：再往前同等天数，供环比
    base = now if now is not None else time.time()
    prev_rows = load_daily(db, days=span, now=base - span * 86400) if enabled else []
    cur = summarize(rows) if rows else summarize([])
    prev = summarize(prev_rows) if prev_rows else None
    return {
        "ts": base,
        "data_root": str(root),
        "days": float(span),
        "enabled": enabled,
        "db_path": str(db) if enabled else "",
        "daily": rows,
        "summary": cur,
        "prev_summary": prev,
        "verdicts": verdicts(cur, prev),
    }


def render_review(rep: Dict[str, Any]) -> str:
    lines = [
        f"=== 账号接入漏斗验收（{rep['data_root']}，近 {rep['days']:.0f} 天）===",
        "",
    ]
    if not rep.get("enabled"):
        lines.append("  趋势落库未启用或库不存在（ops.login_funnel_trend.enabled=true 后重启）。")
        return "\n".join(lines)
    s = rep.get("summary") or {}
    rate = s.get("success_rate")
    rate_s = f"{rate*100:.1f}%" if rate is not None else "—"
    lines += [
        f"  发起 {s.get('started', 0)}  成功 {s.get('authorized', 0)}  "
        f"失败 {s.get('failed', 0)}  成功率 {rate_s}",
        "",
        "-- 失败原因 Top --",
    ]
    tops = s.get("top_reasons") or []
    if tops:
        for item in tops:
            lines.append(f"  {item['reason']:<18} {item['n']}")
    else:
        lines.append("  （窗口内无失败归因）")
    lines += ["", "-- 判词 --"]
    for v in rep.get("verdicts") or []:
        lines.append(f"  {v}")
    return "\n".join(lines)


def render_trend(rows: List[Dict[str, Any]]) -> str:
    """周批 JSONL → 环比表 + 判词。"""
    if not rows:
        return ("（趋势文件为空——可先 "
                "`python -m scripts.login_funnel_review --out-jsonl "
                "logs/eval/login_funnel_trend.jsonl` 攒基线）")
    lines = [
        "=== 账号接入漏斗趋势（每行一次周批快照）===",
        "",
        f"{'日期':<12} {'发起':>5} {'成功':>5} {'失败':>5} {'成功率':>8} {'Top原因':<24}",
    ]
    for r in rows:
        s = r.get("summary") or {}
        day = time.strftime("%m-%d", time.gmtime(float(r.get("ts") or 0)))
        rate = s.get("success_rate")
        rate_s = f"{rate*100:.0f}%" if rate is not None else "—"
        tops = s.get("top_reasons") or []
        top = ",".join(f"{x['reason']}:{x['n']}" for x in tops[:2]) or "—"
        lines.append(
            f"{day:<12} {int(s.get('started') or 0):>5} "
            f"{int(s.get('authorized') or 0):>5} {int(s.get('failed') or 0):>5} "
            f"{rate_s:>8} {top:<24}")
    if len(rows) >= 2:
        cur_s = rows[-1].get("summary") or {}
        prv_s = rows[-2].get("summary") or {}
        vs = verdicts(cur_s, prv_s)
        if vs:
            lines += ["", "-- 判词 --"] + [f"  {v}" for v in vs]
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="账号接入漏斗验收周报（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="", help="趋势行追加到 JSONL")
    ap.add_argument("--trend", nargs="?", const="logs/eval/login_funnel_trend.jsonl",
                    default="", help="渲染趋势 JSONL（缺省路径可省参数值）")
    args = ap.parse_args()

    if args.trend:
        rows: List[Dict[str, Any]] = []
        p = Path(args.trend)
        if p.exists():
            for ln in p.read_text("utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
        by_root: Dict[str, list] = {}
        for r in rows:
            by_root.setdefault(str(r.get("data_root") or "?"), []).append(r)
        for root_key, rs in by_root.items():
            print(f"### {root_key}")
            print(render_trend(rs))
            print()
        if not by_root:
            print(render_trend([]))
        return 0

    from scripts._data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    reports = [collect_review(r, days=args.days) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(render_review(rep))
            print()
    if args.out_jsonl:
        out = Path(args.out_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            for rep in reports:
                # JSONL 不落每日明细（体积）；摘要 + 判词够周审
                slim = {k: rep[k] for k in (
                    "ts", "data_root", "days", "enabled", "summary",
                    "prev_summary", "verdicts")}
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
