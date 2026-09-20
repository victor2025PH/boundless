# -*- coding: utf-8 -*-
"""回怼防线效果周读 CLI（只读）——「骂完还聊吗」用数字回答。

背景：2026-08-22 实施54 上线骂战贯彻（连轮加码/情感让位/语音怒气）与消气曲线
（记仇期/翻篇带边界/出站否决）。进程内计数器（temper_stats）随重启清零，
效果复盘需要持久口径：inbox.db 消息表按 detect_insult/is_de_escalation 等
**同一套生产纯函数**离线重放，保证读数与线上判定零口径分叉。

回答三个业务问题：
  1. 骂战规模与形态：几场/几轮/多少会话在骂；
  2. 求和形态：真道歉 vs 敷衍开脱（「开玩笑的」）各多少——消气曲线的输入分布；
  3. **留存**：骂完 24h 内还回来说话吗（骂战不该赶走客户——feisty 档过火的
     直接读数）；--cutoff 切修复前后两段对比。

数据源＝inbox.db（sqlite ro URI 只读，绝不建库/写库）；多实例按
scripts/_data_root 契约逐根审读。仅私聊（群聊骂战动力学不同，刻意不混）。

用法::

    python tools/temper_review.py                  # 全部活跃实例，近 14 天
    python tools/temper_review.py --days 30
    python tools/temper_review.py --cutoff "2026-08-22 08:11"   # 修复装载时刻
    python tools/temper_review.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.companion.temper import (  # noqa: E402
    detect_insult,
    is_de_escalation,
    is_flippant_retraction,
    is_sincere_apology,
)

# 同一场骂战的归组间隙：两次辱骂相隔超过这个窗＝两场（与粘性窗同数量级，
# 取宽一点容纳「骂-被怼-隔几分钟再骂」的真实节奏）
EPISODE_GAP_SEC = 1800.0
# 留存判定窗：骂战最后一条消息后 24h 内对方还有入站＝留住了
RETENTION_SEC = 86400.0


def analyze_fights(
    rows: Sequence[Tuple[str, str, str, float]],
    *,
    now: float,
    cutoff_ts: float = 0.0,
    episode_gap_sec: float = EPISODE_GAP_SEC,
    retention_sec: float = RETENTION_SEC,
) -> Dict[str, Any]:
    """离线重放消息流，聚合骂战场次/求和形态/留存。纯函数（门禁钉口径）。

    ``rows``＝(conversation_id, direction, text, ts)，需按 (cid, ts) 升序；
    ``cutoff_ts``>0 时按骂战首轮时刻切 pre/post 两段（修复装载前后对比）。
    留存只对「已过完整观察窗」（now - 末条 ≥ retention_sec）的场次计率，
    窗未走完的场次单列 pending 不掺水。
    """
    by_conv: Dict[str, List[Tuple[str, str, float]]] = {}
    for cid, direction, text, ts in rows:
        by_conv.setdefault(str(cid), []).append(
            (str(direction), str(text or ""), float(ts)))

    episodes: List[Dict[str, Any]] = []
    deesc_sincere = 0
    deesc_flippant = 0
    deesc_plain = 0
    for cid, msgs in by_conv.items():
        msgs.sort(key=lambda m: m[2])
        ep: Optional[Dict[str, Any]] = None

        def _close(e: Dict[str, Any]) -> None:
            episodes.append(e)

        for direction, text, ts in msgs:
            inbound = direction == "in"
            if ep is not None and ts - ep["last_ts"] > episode_gap_sec:
                _close(ep)
                ep = None
            if inbound and detect_insult(text):
                if ep is None:
                    ep = {"cid": cid, "start_ts": ts, "last_ts": ts,
                          "rounds": 1, "replies": 0, "deesc": ""}
                else:
                    ep["rounds"] += 1
                    ep["last_ts"] = ts
                continue
            if ep is None:
                continue
            ep["last_ts"] = ts
            if inbound:
                if is_de_escalation(text):
                    if is_sincere_apology(text):
                        ep["deesc"] = "sincere"
                        deesc_sincere += 1
                    elif is_flippant_retraction(text):
                        ep["deesc"] = ep["deesc"] or "flippant"
                        deesc_flippant += 1
                    else:
                        ep["deesc"] = ep["deesc"] or "plain"
                        deesc_plain += 1
            else:
                ep["replies"] += 1
        if ep is not None:
            _close(ep)

    # 留存：骂战末条后 retention 窗内同会话有任何入站
    for e in episodes:
        e["window_done"] = (now - e["last_ts"]) >= retention_sec
        e["retained"] = False
        for direction, _t, ts in by_conv.get(e["cid"], []):
            if direction == "in" and e["last_ts"] < ts <= (
                    e["last_ts"] + retention_sec):
                e["retained"] = True
                break

    def _agg(eps: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not eps:
            return {"episodes": 0, "conversations": 0, "rounds_total": 0,
                    "rounds_max": 0, "replied": 0, "retention_eligible": 0,
                    "retained": 0, "retention_rate": None}
        eligible = [e for e in eps if e["window_done"]]
        retained = sum(1 for e in eligible if e["retained"])
        return {
            "episodes": len(eps),
            "conversations": len({e["cid"] for e in eps}),
            "rounds_total": sum(e["rounds"] for e in eps),
            "rounds_max": max(e["rounds"] for e in eps),
            "replied": sum(1 for e in eps if e["replies"] > 0),
            "retention_eligible": len(eligible),
            "retained": retained,
            "retention_rate": (
                round(retained / len(eligible), 3) if eligible else None),
        }

    out: Dict[str, Any] = {
        "total": _agg(episodes),
        "deesc": {"sincere": deesc_sincere, "flippant": deesc_flippant,
                  "plain": deesc_plain},
    }
    if cutoff_ts > 0:
        out["pre"] = _agg([e for e in episodes if e["start_ts"] < cutoff_ts])
        out["post"] = _agg([e for e in episodes if e["start_ts"] >= cutoff_ts])
    return out


def _load_rows(db: Path, since_ts: float) -> List[Tuple[str, str, str, float]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.execute(
            """
            select m.conversation_id, m.direction, m.text, m.ts
            from messages m join conversations c
              on m.conversation_id = c.conversation_id
            where m.ts > ? and c.chat_type = 'private'
            order by m.conversation_id, m.ts
            """,
            (since_ts,),
        )
        return [(str(r[0]), str(r[1]), str(r[2] or ""), float(r[3] or 0))
                for r in cur.fetchall()]
    finally:
        con.close()


def _render(root: str, rep: Dict[str, Any]) -> str:
    lines = [f"== {root} =="]
    t = rep["total"]
    lines.append(
        f"  骂战 {t['episodes']} 场 / {t['conversations']} 会话，"
        f"共 {t['rounds_total']} 轮（单场最多 {t['rounds_max']}），"
        f"AI 有回应 {t['replied']} 场")
    d = rep["deesc"]
    lines.append(
        f"  求和形态：真道歉 {d['sincere']} · 敷衍开脱 {d['flippant']} · "
        f"求和不认错 {d['plain']}")
    rr = t["retention_rate"]
    lines.append(
        f"  24h 留存：{t['retained']}/{t['retention_eligible']}"
        + (f" = {rr:.0%}" if rr is not None else "（观察窗未走完）"))
    for seg, label in (("pre", "修复前"), ("post", "修复后")):
        if seg in rep:
            s = rep[seg]
            srr = s["retention_rate"]
            lines.append(
                f"  [{label}] {s['episodes']} 场 {s['rounds_total']} 轮，"
                f"留存 {s['retained']}/{s['retention_eligible']}"
                + (f" = {srr:.0%}" if srr is not None else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="回怼防线效果周读（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--cutoff", default="",
                    help="修复装载时刻（YYYY-MM-DD [HH:MM]），切前后两段对比")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = time.time()
    since = now - args.days * 86400
    cutoff_ts = 0.0
    if args.cutoff:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                cutoff_ts = time.mktime(time.strptime(args.cutoff, fmt))
                break
            except ValueError:
                continue
        if cutoff_ts <= 0:
            print(f"无法解析 --cutoff：{args.cutoff}", file=sys.stderr)
            return 2

    roots = ([Path(args.data_root)] if args.data_root
             else resolve_data_roots())
    results: Dict[str, Any] = {}
    for root in roots:
        db = Path(root) / "config" / "inbox.db"
        if not db.exists():
            continue
        try:
            rows = _load_rows(db, since)
        except Exception as exc:
            results[str(root)] = {"error": str(exc)}
            continue
        results[str(root)] = analyze_fights(
            rows, now=now, cutoff_ts=cutoff_ts)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("未发现任何实例 inbox.db")
        return 1
    for root, rep in results.items():
        if "error" in rep:
            print(f"== {root} ==\n  读取失败：{rep['error']}")
            continue
        print(_render(root, rep))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
