# -*- coding: utf-8 -*-
"""主动触达验收周报 CLI（P4，2026-07-29，只读）。

「P0-P3 修没修好」从翻日志/开看板变成一条命令：

    python -m scripts.proactive_review [--data-root PATH] [--days 14]
                                       [--json] [--out-jsonl FILE]

读数（全部只读——inbox.db 走 mode=ro URI，对活体生产库零写风险）：
- 发送量按 mode / 按形态（text·voice·photo）+ 各自回复率（触达后 72h 有入站）；
- **事故原话指标**：出站里「好久没联系/还好吗/最近怎么样」模板句计数，
  本窗 vs 上一窗对比——P0 的整改效果直接可见；
- 未回退避账本直方图（streak 1/2/3+）与 opt-out 静默数；
- ``--out-jsonl`` 追加趋势行（与 translation_eval_weekly 同哲学，周审看走势）。

多实例机自动逐根出报告（``scripts/_data_root.py`` 契约）。
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 事故原话模板句（2026-07-29 实录：42 条「好久没联系，你最近还好吗」近逐字复读）
SPAM_PATTERNS = ("好久没", "还好吗", "最近怎么样", "最近好吗")

_KINDS = ("text", "voice", "photo")


def _ro_conn(db_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _mode_response(conn: sqlite3.Connection, since: float,
                   window_s: float) -> Dict[str, Dict[str, Any]]:
    """按 note(mode) 分组的 发送/回复率（与 store.outreach_note_response_stats
    同口径；CLI 走只读连接故独立实现，口径由 P4 门禁双向钉住）。"""
    rows = conn.execute(
        "SELECT o.note AS note, o.ts AS sent_ts, "
        "(SELECT MIN(m.ts) FROM messages m "
        " WHERE m.conversation_id = o.conversation_id "
        "   AND m.direction='in' AND m.ts > o.ts) AS reply_ts "
        "FROM outreach_log o "
        "WHERE o.batch_id LIKE 'proactive_topic:%' AND o.status='sent' "
        "  AND o.ts >= ?", (since,)).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = out.setdefault(str(r["note"] or "unknown"),
                           {"sent": 0, "responded": 0})
        b["sent"] += 1
        if r["reply_ts"] is None:
            continue
        delta = float(r["reply_ts"]) - float(r["sent_ts"])
        if 0 < delta and (window_s <= 0 or delta <= window_s):
            b["responded"] += 1
    for b in out.values():
        b["response_rate"] = (
            round(b["responded"] / b["sent"], 3) if b["sent"] else 0.0)
    return out


def _kind_response(conn: sqlite3.Connection, since: float,
                   window_s: float) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for kind in _KINDS:
        rows = conn.execute(
            "SELECT o.ts AS sent_ts, "
            "(SELECT MIN(m.ts) FROM messages m "
            " WHERE m.conversation_id = o.conversation_id "
            "   AND m.direction='in' AND m.ts > o.ts) AS reply_ts "
            "FROM outreach_log o WHERE o.batch_id = ? AND o.status='sent' "
            "  AND o.ts >= ?", (f"proactive_topic:{kind}", since)).fetchall()
        sent, responded = len(rows), 0
        for r in rows:
            if r["reply_ts"] is None:
                continue
            delta = float(r["reply_ts"]) - float(r["sent_ts"])
            if 0 < delta and (window_s <= 0 or delta <= window_s):
                responded += 1
        out[kind] = {
            "sent": sent, "responded": responded,
            "response_rate": round(responded / sent, 3) if sent else 0.0,
        }
    return out


def _spam_count(conn: sqlite3.Connection, since: float, until: float) -> int:
    """时间窗内出站消息命中事故模板句的条数。"""
    like = " OR ".join("text LIKE ?" for _ in SPAM_PATTERNS)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM messages "
        f"WHERE direction='out' AND ts >= ? AND ts < ? AND ({like})",
        (since, until, *[f"%{p}%" for p in SPAM_PATTERNS])).fetchone()
    return int(row["n"] or 0)


def _ledger_hist(config_dir: Path) -> Dict[str, Any]:
    out = {"entries": 0, "streak1": 0, "streak2": 0, "streak3plus": 0}
    p = config_dir / "companion_proactive_cooldown.json"
    try:
        if p.exists():
            data = json.loads(p.read_text("utf-8")) or {}
            for v in data.values():
                out["entries"] += 1
                s = int((v or {}).get("streak") or 0) if isinstance(v, dict) else 1
                if s == 1:
                    out["streak1"] += 1
                elif s == 2:
                    out["streak2"] += 1
                elif s >= 3:
                    out["streak3plus"] += 1
    except Exception:
        pass
    return out


def _resp_rate_dist(config_dir: Path, *, min_obs: int = 4) -> Dict[str, Any]:
    """账本各会话长期回复率分布（P6：阈值自适应的判据可见化）。

    只取 obs_n≥min_obs 的会话（与 response_rate_factor 同一适用人群）；
    n<4 时分位数意义不大，只报 n。
    """
    p = config_dir / "companion_proactive_cooldown.json"
    rates = []
    try:
        if p.exists():
            data = json.loads(p.read_text("utf-8")) or {}
            for v in data.values():
                if not isinstance(v, dict):
                    continue
                n = int(v.get("obs_n") or 0)
                if n >= min_obs:
                    rates.append(
                        min(1.0, max(0.0, int(v.get("obs_replied") or 0) / n)))
    except Exception:
        pass
    out: Dict[str, Any] = {"n": len(rates)}
    if len(rates) >= 4:
        from src.utils.proactive_pacing import _percentile
        rates.sort()
        out.update({
            "p25": round(_percentile(rates, 25), 3),
            "p50": round(_percentile(rates, 50), 3),
            "p75": round(_percentile(rates, 75), 3),
        })
    return out


def _optout_muted(config_dir: Path, now: float) -> int:
    p = config_dir / "companion_optout_mute.json"
    try:
        if p.exists():
            data = json.loads(p.read_text("utf-8")) or {}
            return sum(
                1 for v in data.values()
                if isinstance(v, dict) and float(v.get("until") or 0) > now)
    except Exception:
        pass
    return 0


def collect_review(data_root: Path, *, days: float = 14.0,
                   response_window_days: float = 3.0,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """单实例数据根 → 验收报告 dict（只读；库缺失/坏根软失败出空段）。"""
    t = float(now if now is not None else time.time())
    since = t - float(days) * 86400.0
    prev_since = since - float(days) * 86400.0
    window_s = float(response_window_days) * 86400.0
    root = Path(data_root)
    rep: Dict[str, Any] = {
        "data_root": str(root), "ts": t, "days": float(days),
        "mode_ab": {}, "kind_ab": {},
        "spam_now": -1, "spam_prev": -1,
        "backoff": {}, "optout_muted": 0,
    }
    conn = _ro_conn(root / "config" / "inbox.db")
    if conn is not None:
        try:
            rep["mode_ab"] = _mode_response(conn, since, window_s)
            rep["kind_ab"] = _kind_response(conn, since, window_s)
            rep["spam_now"] = _spam_count(conn, since, t)
            rep["spam_prev"] = _spam_count(conn, prev_since, since)
        finally:
            conn.close()
    rep["backoff"] = _ledger_hist(root / "config")
    rep["optout_muted"] = _optout_muted(root / "config", t)
    rep["resp_rates"] = _resp_rate_dist(root / "config")
    return rep


def render_review(rep: Dict[str, Any]) -> str:
    lines = [
        f"=== 主动触达验收（{rep['data_root']}，近 {rep['days']:.0f} 天）===",
        "",
        "-- 事故原话指标（「好久没/还好吗/最近怎么样」出站模板句）--",
        f"  本窗 {rep['spam_now']} 条  vs  上一窗 {rep['spam_prev']} 条"
        + ("  ↓ 收敛" if 0 <= rep["spam_now"] < rep["spam_prev"] else ""),
        "",
        "-- 开场 mode：发送 / 回复率 --",
    ]
    mab = rep.get("mode_ab") or {}
    for k in sorted(mab, key=lambda x: -mab[x]["sent"]):
        b = mab[k]
        lines.append(
            f"  {k:<18} {b['sent']:>4} 发   {b['response_rate']*100:5.1f}% 回")
    if not mab:
        lines.append("  （窗口内无主动发送）")
    lines.append("")
    lines.append("-- 形态：发送 / 回复率 --")
    for k in _KINDS:
        b = (rep.get("kind_ab") or {}).get(k) or {}
        if not b:
            continue
        lines.append(
            f"  {k:<6} {b.get('sent', 0):>4} 发   "
            f"{(b.get('response_rate') or 0)*100:5.1f}% 回")
    bo = rep.get("backoff") or {}
    lines += [
        "",
        "-- 未回退避账本 --",
        f"  条目 {bo.get('entries', 0)}  streak1={bo.get('streak1', 0)}"
        f"  streak2={bo.get('streak2', 0)}  streak3+={bo.get('streak3plus', 0)}",
        f"  opt-out 静默中 {rep.get('optout_muted', 0)} 人",
    ]
    rr = rep.get("resp_rates") or {}
    if rr.get("n"):
        line = f"  长期回复率分布 n={rr['n']}"
        if "p50" in rr:
            line += (f"  P25={rr['p25']:.2f} P50={rr['p50']:.2f}"
                     f" P75={rr['p75']:.2f}（阈值自适应判据）")
        lines.append(line)
    return "\n".join(lines)


def render_trend(rows: list) -> str:
    """周批 JSONL 行 → 周环比趋势表 + 判词（P5）。

    每行=一次 collect_review 快照。看四个验收问题：模板句在收敛吗、checkin
    还垄断吗、退避 streak3+ 在涨吗、各 mode 回复率怎么走。
    """
    if not rows:
        return "（趋势文件为空——周批任务 ProactiveReviewWeekly 每周六 07:10 追加）"
    lines = ["=== 主动触达趋势（每行一次周批快照）===", ""]
    lines.append(f"{'日期':<12} {'模板句':>6} {'总发':>5} {'checkin%':>9} "
                 f"{'富开场':>6} {'streak3+':>9} {'text%':>6} {'voice%':>7}")
    prev_spam = None
    for r in rows:
        mab = r.get("mode_ab") or {}
        total = sum(int((b or {}).get("sent") or 0) for b in mab.values())
        ck = int((mab.get("gentle_checkin") or {}).get("sent") or 0)
        rich = total - ck
        kab = r.get("kind_ab") or {}
        day = time.strftime("%m-%d", time.localtime(float(r.get("ts") or 0)))
        lines.append(
            f"{day:<12} {int(r.get('spam_now', -1)):>6} {total:>5} "
            f"{(ck / total * 100 if total else 0):>8.0f}% {rich:>6} "
            f"{int((r.get('backoff') or {}).get('streak3plus') or 0):>9} "
            f"{((kab.get('text') or {}).get('response_rate') or 0)*100:>5.0f}% "
            f"{((kab.get('voice') or {}).get('response_rate') or 0)*100:>6.0f}%")
    # 判词：末两行对比
    if len(rows) >= 2:
        cur, prv = rows[-1], rows[-2]
        s_c, s_p = int(cur.get("spam_now", -1)), int(prv.get("spam_now", -1))
        verdicts = []
        if 0 <= s_c < s_p:
            verdicts.append(f"模板句收敛（{s_p}→{s_c}）")
        elif s_c > s_p >= 0:
            verdicts.append(f"⚠ 模板句回潮（{s_p}→{s_c}），查变体守卫/退避是否被改")
        b_c = int((cur.get("backoff") or {}).get("streak3plus") or 0)
        b_p = int((prv.get("backoff") or {}).get("streak3plus") or 0)
        if b_c > b_p:
            verdicts.append(f"⚠ 未回 streak3+ 增多（{b_p}→{b_c}），死联系人仍在被触达")
        if verdicts:
            lines += ["", "-- 判词 --"] + [f"  {v}" for v in verdicts]
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="主动触达验收周报（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="", help="趋势行追加到 JSONL")
    ap.add_argument("--trend", nargs="?", const="logs/eval/proactive_trend.jsonl",
                    default="", help="渲染趋势 JSONL（缺省路径可省参数值）")
    args = ap.parse_args()

    if args.trend:
        rows = []
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
        # 多实例：按 data_root 分组各渲一张
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
                f.write(json.dumps(rep, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
