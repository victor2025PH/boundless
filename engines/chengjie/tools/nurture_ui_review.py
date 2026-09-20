# -*- coding: utf-8 -*-
"""智能养号卡 ntr_ 埋点裁决读数 CLI（只读；对齐 inbox_filter_usage_report 家法）。

背景
----
2026-08-22 智能养号卡可懂化三批（实施55）：三步引导条 + go_live 内联确认 + 试点 chip +
上线前单步试跑收进「高级」折叠。交互重构的价值主张全部可埋点验证——ntr_* 9 桶经
``/api/telemetry/ui-event`` → ``ops.ui_event_trend`` 按日落库
（``<数据根>/config/ui_event_trend.db``）。**裁决口径在数据攒够之前先钉死在这里**，
防止两周后拿着数字凑结论（iflt_ 裁决同款纪律）。

**只读、不改任何行为**：真正调整交互（软化确认框/降级探针入口/加批量试点）是看完
本报告后的显式施工决定。DB 不存在（``ops.ui_event_trend`` 未开/尚无数据）→ 明确提示，
不创建空库；读取走 ``mode=ro`` URI，对活体生产库零写事务。

判词设计（纯函数，喂 totals 即可单测）
----
- **纪元日** ``NTR_EPOCH_DAY``：全部 ntr_* 埋点 2026-08-22 同批上线。观测天数按
  「今天 - 纪元日」算而不是数据首现日——零点击本身是证据。
- **链路自证**：观测满窗且 ntr_* 全量为 0 → 一律「先查链路」（上线当天浏览器门禁
  N10 已证 9 桶全通，两周后全 0 更可能是链断/trend 开关被关，而不是没人点）。
- 四个裁决问题：
  Q1 确认框拦截率（golive_cancel / (cancel+confirm)）——确认框是否在拦真实误操作；
  Q2 单步试跑采用度（probe_pre/probe_run）——上线前验证纪律有没有被执行；
  Q3 试点 UI 入口采用度（pilot_on/off）——canary 管理是否真的离开了 YAML
     （0 用量 → 指路 audit 台账 nurture.set_canary 查是否仍有人手改配置）；
  Q4 高级区打开频度（adv_open vs sim_start）——折叠边界是否切对了。

用法
----
    python tools/nurture_ui_review.py [--days 30] [--min-days 14] [--data-root PATH] [--json]

数据根按 ``scripts/_data_root`` 契约解析；多实例逐根输出。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

# 全部 ntr_* 埋点的上线日（可懂化三批同日：sim_start/golive_ask|confirm|cancel/save/
# pilot_on|off/probe_pre|run/adv_open）。晚于纪元日的新分桶按问题挂独立纪元。
NTR_EPOCH_DAY = "2026-08-22"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
MIN_CONFIRM_ASKS = 5      # Q1 比值最小样本（1 取消 vs 2 确认这种别当真理）
ADV_HEAVY_RATIO = 2.0     # Q4 高级区打开 > 模拟启动 x2 ＝折叠边界可能切错


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 ntr_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'ntr\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                for r in rows]
    finally:
        conn.close()


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {totals, by_day, grand_total}（纯函数）。"""
    totals: Dict[str, int] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        a = str(r.get("action") or "")
        d = str(r.get("day") or "")
        n = int(r.get("n") or 0)
        if not a or not d:
            continue
        totals[a] = totals.get(a, 0) + n
        by_day.setdefault(d, {})[a] = by_day.setdefault(d, {}).get(a, 0) + n
    return {"totals": totals, "by_day": by_day, "grand_total": sum(totals.values())}


def observed_days(now_day: str, epoch_day: str = NTR_EPOCH_DAY) -> int:
    """自埋点纪元日起的观测天数（含首尾；纪元日当天=1）。解析失败→0（保守）。"""
    try:
        d = (date.fromisoformat(now_day) - date.fromisoformat(epoch_day)).days + 1
        return max(0, d)
    except Exception:
        return 0


def verdicts(totals: Dict[str, int], observed: int,
             *, min_days: int = DEFAULT_MIN_DAYS) -> List[Dict[str, Any]]:
    """四问裁决（纯函数）。status ∈ insufficient | check_link | verdict。"""
    g = lambda k: int(totals.get(k, 0) or 0)  # noqa: E731
    grand = sum(int(v or 0) for v in totals.values())
    out: List[Dict[str, Any]] = []

    if observed >= min_days and grand == 0:
        return [{
            "topic": "chain", "status": "check_link",
            "verdict": ("观测已满窗但 ntr_* 全量为 0——上线当天浏览器门禁已证 9 桶全通，"
                        "先查链路（ops.ui_event_trend 是否被关 / beacon 是否被挡），"
                        "而不是下「没人用」结论"),
            "numbers": {"observed_days": observed, "grand_total": 0},
        }]
    if observed < min_days:
        out.append({
            "topic": "window", "status": "insufficient",
            "verdict": f"观测 {observed} 天 < {min_days} 天样本闸门——先攒数据，勿早裁",
            "numbers": {"observed_days": observed, "grand_total": grand},
        })

    asks, cancels, confirms = g("ntr_golive_ask"), g("ntr_golive_cancel"), g("ntr_golive_confirm")
    if asks < MIN_CONFIRM_ASKS:
        out.append({
            "topic": "golive_confirm", "status": "insufficient",
            "verdict": f"确认框样本不足（ask={asks} < {MIN_CONFIRM_ASKS}）",
            "numbers": {"ask": asks, "cancel": cancels, "confirm": confirms},
        })
    else:
        denom = max(1, cancels + confirms)
        ratio = cancels / denom
        if ratio >= 0.15:
            v = (f"确认框在拦真实误操作（取消率 {ratio:.0%}）——保留，"
                 "且说明 go_live 按钮位置/文案仍有误触诱因，可再看按钮布局")
        elif cancels == 0:
            v = ("零取消——确认框没拦到误操作；威慑价值测不出，保留但降优先级，"
                 "不再加重确认阻力")
        else:
            v = f"取消率 {ratio:.0%}（低位）——确认框保留现状"
        out.append({"topic": "golive_confirm", "status": "verdict", "verdict": v,
                    "numbers": {"ask": asks, "cancel": cancels, "confirm": confirms,
                                "cancel_ratio": round(ratio, 3)}})

    pre, run = g("ntr_probe_pre"), g("ntr_probe_run")
    if observed >= min_days and pre + run == 0:
        out.append({"topic": "probe", "status": "verdict",
                    "verdict": ("满窗零使用——「上线前单步试跑」无人用；候选处置：保持折叠不动"
                                "（低频≠无用，go_live 前才需要）或并入文档；勿加曝光"),
                    "numbers": {"pre": 0, "run": 0}})
    elif pre + run > 0:
        out.append({"topic": "probe", "status": "verdict",
                    "verdict": (f"在用（先检查 {pre} / 真跑 {run}）——"
                                + ("真跑>0：上线前验证纪律在执行" if run > 0
                                   else "只查不跑：可能在当健康探针用")),
                    "numbers": {"pre": pre, "run": run}})

    p_on, p_off = g("ntr_pilot_on"), g("ntr_pilot_off")
    if observed >= min_days and p_on + p_off == 0:
        out.append({"topic": "pilot_ui", "status": "verdict",
                    "verdict": ("满窗零使用——试点仍无人从 UI 设；查 audit 台账 "
                                "nurture.set_canary 是否为 0：若 YAML 也没人改＝功能未被采用；"
                                "若 YAML 仍在改＝入口没被发现，考虑首次引导"),
                    "numbers": {"on": 0, "off": 0}})
    elif p_on + p_off > 0:
        out.append({"topic": "pilot_ui", "status": "verdict",
                    "verdict": f"UI 已接管试点管理（设 {p_on} / 撤 {p_off}）——YAML-only 环节收口成功",
                    "numbers": {"on": p_on, "off": p_off}})

    adv, sim = g("ntr_adv_open"), g("ntr_sim_start")
    if adv > 0 and adv > sim * ADV_HEAVY_RATIO:
        out.append({"topic": "advanced_fold", "status": "verdict",
                    "verdict": (f"高级区打开 {adv} 次 >> 模拟启动 {sim} 次——运维高频进折叠区，"
                                "考虑把（警告 chips）提升出折叠常显"),
                    "numbers": {"adv_open": adv, "sim_start": sim}})
    elif observed >= min_days:
        out.append({"topic": "advanced_fold", "status": "verdict",
                    "verdict": f"折叠边界工作正常（adv_open={adv} vs sim_start={sim}）",
                    "numbers": {"adv_open": adv, "sim_start": sim}})

    return out


def _report_root(root: Path, *, days: int, min_days: int,
                 now: Optional[float] = None) -> Dict[str, Any]:
    db = root / "config" / "ui_event_trend.db"
    now_day = _utc_day(now)
    try:
        rows = read_rows(db, days=days, now=now)
    except FileNotFoundError:
        return {"root": str(root), "db": str(db), "ok": False,
                "note": "trend 库不存在（ops.ui_event_trend 未开或尚无任何上报）"}
    agg = aggregate(rows)
    obs = observed_days(now_day)
    return {"root": str(root), "db": str(db), "ok": True,
            "now_day": now_day, "observed_days": obs,
            "totals": dict(sorted(agg["totals"].items())),
            "grand_total": agg["grand_total"],
            "verdicts": verdicts(agg["totals"], obs, min_days=min_days)}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="智能养号卡 ntr_ 埋点裁决读数（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    reports = [_report_root(Path(r), days=args.days, min_days=args.min_days)
               for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"\n=== {rep['root']} ===")
        if not rep.get("ok"):
            print(f"  [SKIP] {rep.get('note')}")
            continue
        print(f"  观测窗：纪元 {NTR_EPOCH_DAY} 起 {rep['observed_days']} 天"
              f"（读近 {args.days} 天，闸门 {args.min_days} 天）  合计 {rep['grand_total']} 次")
        for a, n in (rep.get("totals") or {}).items():
            print(f"    {a:<24} {n}")
        for v in rep.get("verdicts") or []:
            print(f"  [{v['status']:<12}] {v['topic']:<14} {v['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
