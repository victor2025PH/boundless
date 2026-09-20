# -*- coding: utf-8 -*-
"""营销目标验收周报 CLI（P27，2026-08-05，只读）。

「目标子系统跑得怎么样、桥该不该开」从翻看板变成一条命令：

    python -m scripts.goal_review [--data-root PATH] [--days 14]
                                  [--json] [--out-jsonl FILE]

读数（marketing_goals.db 经 ``GoalStore.open_readonly``，对活体生产库零写事务）：
- 窗口终态聚合（复用 ``store.outcome_report`` 单一口径）：模板×结果 / 成功率 /
  平均达成天数 / 真成交、拍漏斗 planned→consumed→sent（skipped=坐席驳回）、
  坐席 adopt/reject、「采纳并拟稿」使用、画像槽位填充分源（auto/llm/agent）；
- **摸底目标（profile_discovery）专项**：活跃数/窗口达成数、勾选槽位当前平均
  填充率、槽位从建目标到聊出来的中位天数（采集速度）、按槽位的填充分布；
- **活跃目标快照（模板×自治档）**：「suggest 档注入质量 → 要不要开
  ``bridge.enabled``」的决策读数面（overlay 注释里等的就是这组数）；
- ``--out-jsonl`` 追加趋势行（与 proactive_review 同哲学，周审看走势）。

多实例机自动逐根出报告（``scripts/_data_root.py`` 契约）。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DAY = 86400.0
_DEFAULT_DB_REL = Path("config") / "marketing_goals.db"


def _median(vals: List[float]) -> Optional[float]:
    xs = sorted(float(v) for v in vals)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _discovery_section(store: Any, since: float) -> Dict[str, Any]:
    """摸底目标专项（活跃 + 窗口内达成）：填充率/采集速度/按槽分布。

    CLI 属第一方脚本、连接已是只读——直读 ``store._conn`` 与
    ``proactive_review`` 自开只读连接同纪律。
    """
    out: Dict[str, Any] = {
        "active": 0, "done_window": 0, "avg_fill_active": None,
        "median_days_to_fill": None, "by_slot": {}, "n_slot_fills": 0,
    }
    try:
        from src.companion.goals.profile_slots import parse_selected_slots
        rows = store._conn.execute(
            "SELECT goal_id, platform, chat_key, params, status,"
            "       start_ts, done_at"
            " FROM goals WHERE template='profile_discovery'"
            "   AND (status='active' OR done_at >= ?)", (since,)).fetchall()
        fills: List[float] = []       # 槽位从建目标到聊出来的天数
        active_rates: List[float] = []
        for r in rows:
            status = str(r["status"])
            if status == "active":
                out["active"] += 1
            elif status == "done":
                out["done_window"] += 1
            try:
                params = json.loads(r["params"] or "{}")
            except Exception:
                params = {}
            sel = parse_selected_slots((params or {}).get("slots"))
            if not sel:
                continue
            prof = store.get_customer_profile(
                str(r["platform"] or ""), str(r["chat_key"] or ""))
            fields = dict((prof or {}).get("fields") or {})
            got = 0
            start_ts = float(r["start_ts"] or 0.0)
            for k in sel:
                cell = fields.get(k)
                v = str((cell or {}).get("v") or "").strip() if isinstance(
                    cell, dict) else str(cell or "").strip()
                if not v:
                    continue
                got += 1
                out["by_slot"][k] = int(out["by_slot"].get(k, 0)) + 1
                out["n_slot_fills"] += 1
                ts = float((cell or {}).get("ts") or 0.0) if isinstance(
                    cell, dict) else 0.0
                if ts > 0 and start_ts > 0 and ts >= start_ts:
                    fills.append((ts - start_ts) / _DAY)
            if status == "active":
                active_rates.append(got / len(sel))
        if active_rates:
            out["avg_fill_active"] = round(
                sum(active_rates) / len(active_rates), 3)
        md = _median(fills)
        out["median_days_to_fill"] = round(md, 2) if md is not None else None
    except Exception:
        pass
    return out


def _active_snapshot(store: Any) -> Dict[str, Any]:
    """活跃目标 模板×自治档 快照（桥开闸决策读数）。"""
    out: Dict[str, Any] = {"by_template": {}, "by_autonomy": {}}
    try:
        rows = store._conn.execute(
            "SELECT template, autonomy, COUNT(*) AS n FROM goals"
            " WHERE status='active' GROUP BY template, autonomy").fetchall()
        for r in rows:
            t, a, n = str(r["template"]), str(r["autonomy"]), int(r["n"])
            out["by_template"][t] = int(out["by_template"].get(t, 0)) + n
            out["by_autonomy"][a] = int(out["by_autonomy"].get(a, 0)) + n
    except Exception:
        pass
    return out


def collect_review(data_root: Path, *, days: int = 14,
                   db_path: str = "", now: Optional[float] = None
                   ) -> Dict[str, Any]:
    """单数据根的完整读数（可 import 进门禁测试）。"""
    n = float(now if now is not None else time.time())
    since = n - max(1, int(days)) * _DAY
    db = Path(db_path) if db_path else (Path(data_root) / _DEFAULT_DB_REL)
    rep: Dict[str, Any] = {
        "ts": n, "kind": "goal_review", "data_root": str(data_root),
        "days": int(days), "db": str(db), "ok": False,
    }
    if not db.exists():
        rep["error"] = "db_not_found"
        return rep
    try:
        from src.companion.goals.store import GoalStore
        store = GoalStore.open_readonly(db)
    except Exception as exc:  # noqa: BLE001
        rep["error"] = f"open_failed:{exc}"
        return rep
    try:
        outcome = store.outcome_report(since, now=n)
        rep.update({
            "ok": True,
            "totals": outcome.get("totals") or {},
            "by_template": outcome.get("by_template") or {},
            "beats": outcome.get("beats") or {},
            "feedback": outcome.get("feedback") or {},
            "drive_draft": outcome.get("drive_draft") or {},
            "profile_fills": outcome.get("profile_fills") or {},
            "active_now": int(outcome.get("active_now") or 0),
            "discovery": _discovery_section(store, since),
            "active_snapshot": _active_snapshot(store),
        })
    except Exception as exc:  # noqa: BLE001
        rep["error"] = f"collect_failed:{exc}"
    finally:
        try:
            store.close()
        except Exception:
            pass
    return rep


def render_review(rep: Dict[str, Any]) -> str:
    ls: List[str] = []
    ls.append(f"=== 营销目标周报 · {rep.get('data_root')} · "
              f"近 {rep.get('days')} 天 ===")
    if not rep.get("ok"):
        ls.append(f"  (跳过：{rep.get('error')})")
        return "\n".join(ls)
    t = rep.get("totals") or {}
    ls.append(f"终态 {t.get('n', 0)} 个：done {t.get('done', 0)} / "
              f"failed {t.get('failed', 0)} / expired {t.get('expired', 0)} / "
              f"cancelled {t.get('cancelled', 0)}"
              f"（成功率 {round(float(t.get('done_rate') or 0) * 100)}%"
              + (f"，平均 {round(float(t['avg_days_to_done']), 1)} 天达成"
                 if t.get("avg_days_to_done") is not None else "") + "）")
    if t.get("won"):
        ls.append(f"真成交 won：{t.get('won')}（{round(float(t.get('won_rate') or 0) * 100)}%）")
    b = rep.get("beats") or {}
    ls.append(f"当日拍：planned {b.get('planned', 0)} → consumed "
              f"{b.get('consumed', 0)} → sent {b.get('sent', 0)}"
              f"（坐席驳回 skipped {b.get('skipped', 0)}）")
    fb = rep.get("feedback") or {}
    ls.append(f"坐席反馈：adopt {fb.get('adopt', 0)} / reject {fb.get('reject', 0)}"
              f"；采纳并拟稿 {((rep.get('drive_draft') or {}).get('total', 0))} 次")
    pf = rep.get("profile_fills") or {}
    if pf.get("total"):
        src = pf.get("by_src") or {}
        ls.append(f"画像填充 {pf.get('total')} 槽（正则 {src.get('auto', 0)} / "
                  f"LLM {src.get('llm', 0)} / 坐席 {src.get('agent', 0)}）")
    d = rep.get("discovery") or {}
    if d.get("active") or d.get("done_window") or d.get("n_slot_fills"):
        fill = d.get("avg_fill_active")
        md = d.get("median_days_to_fill")
        ls.append(f"摸底目标：活跃 {d.get('active', 0)} / 窗口达成 "
                  f"{d.get('done_window', 0)}"
                  + (f"，活跃平均填充率 {round(float(fill) * 100)}%"
                     if fill is not None else "")
                  + (f"，槽位采集中位 {md} 天" if md is not None else ""))
        if d.get("by_slot"):
            top = sorted((d.get("by_slot") or {}).items(),
                         key=lambda kv: -kv[1])[:6]
            ls.append("  按槽位：" + "、".join(f"{k}×{v}" for k, v in top))
    snap = rep.get("active_snapshot") or {}
    by_a = snap.get("by_autonomy") or {}
    ls.append(f"活跃目标 {rep.get('active_now', 0)} 个"
              f"（observe {by_a.get('observe', 0)} / suggest "
              f"{by_a.get('suggest', 0)} / auto {by_a.get('auto', 0)}）")
    if by_a.get("auto"):
        ls.append("  ↳ 有 auto 档目标：开 companion.goals.bridge.enabled 后"
                  "主动触达即带目标拍（当前 overlay 为关）")
    return "\n".join(ls)


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="营销目标验收周报（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--db", default="", help="显式 marketing_goals.db 路径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="")
    args = ap.parse_args()

    from scripts._data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    reports = [collect_review(Path(r), days=args.days, db_path=args.db)
               for r in roots]
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
