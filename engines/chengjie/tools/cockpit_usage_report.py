# -*- coding: utf-8 -*-
"""驾驶舱页用量裁决读数 CLI（只读；P0/P1 改版后「数据裁决周」的决策工具）。

背景
----
2026-08-14 驾驶舱 P0/P1 改版（KPI 单口径 / 「已处理」出口 / 分类筛选 / 历史积压
折叠 / 空态 ROI）后，遗留五个**读数决策**：页面本身有没有人来、resolve 出口是否
被使用、筛选行值不值得留、积压折叠区有没有人翻、「接完不还」行为是否坐实。
埋点全走本页 ``beacon()`` 的 ``ck_*`` 前缀 → ``ops.ui_event_trend`` 按日落库
（``<数据根>/config/ui_event_trend.db``）。机制先上线，样本门槛就是等待期
（与 mode_gate / inbox_filter_usage_report 同哲学）——本 CLI 把「样本到没到、
证据指向哪」读出来，别在看板里另算一套。

**只读、不改任何行为**：真正砍筛选行 / 撤积压折叠 / 提级页面入口，是看完本报告
后的显式施工决定。DB 不存在 → 明确提示，**不创建空库**；读取走 ``mode=ro`` URI。

判词设计（纯函数，喂 rows 即可单测）
----
- **埋点纪元日** ``CK_EPOCH_DAY``：全部 ck_* 埋点同日上线（2026-08-14 P0/P1）。
  观测天数按「今天 - 纪元日」算而**不是**按数据首现日——零点击本身是证据，
  不能拿「数据首见日」当起点（inbox_filter_usage_report 同款铁律）。
- **零点击是合法证据**：满 ``min_days`` 后 total==0 照样出裁决；``min_total``
  只用来给「比值型结论」降置信。
- **埋点链自证**：窗口内 ck_* 全量为 0（含 ck_open——页面一打开就发）→ 一律
  「样本不足」并点名先查链路，两周后全 0 更可能是链断了而不是没人用。

用法
----
    python tools/cockpit_usage_report.py [--days 30] [--min-days 14]
        [--min-total 20] [--data-root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

# 全部 ck_* 埋点的上线日（P0/P1 改版随批：open/take/handback/resolve/
# filter_*/stale_*/hint_dismiss 同日）。晚于纪元日新增的分桶在判词里单独标注。
CK_EPOCH_DAY = "2026-08-14"

# 顶栏入口隐藏日（ui_visibility.cockpit 默认 False，2026-08-16 老板决定：人工
# 操作台那套藏了，驾驶舱跟着藏）。**此日之后的零点击不是「没人用」的证据**——
# 入口都没了，坐席进不来。本 CLI 的观测窗因此中止：判词照常算，但每份报告顶
# 部打横幅提醒读数人别据此裁决；重新开启该键后 ck_* 自然续上，届时把
# CK_EPOCH_DAY 挪到重开那天再重新计窗（纪元日必须是「入口对坐席可见」的起点，
# 与 inbox_filter_usage_report 那条「不能拿数据首见日当起点」同源）。
# 置空字符串 = 入口已重新开启且窗口已重算。
CK_ENTRY_HIDDEN_DAY = "2026-08-16"

DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 ck_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'ck\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                for r in rows]
    finally:
        conn.close()


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {totals, by_day, first_seen, grand_total}（纯函数）。"""
    totals: Dict[str, int] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    first_seen: Dict[str, str] = {}
    for r in rows or []:
        a = str(r.get("action") or "")
        d = str(r.get("day") or "")
        n = int(r.get("n") or 0)
        if not a or not d:
            continue
        totals[a] = totals.get(a, 0) + n
        by_day.setdefault(d, {})
        by_day[d][a] = by_day[d].get(a, 0) + n
        if n > 0 and (a not in first_seen or d < first_seen[a]):
            first_seen[a] = d
    return {"totals": totals, "by_day": by_day, "first_seen": first_seen,
            "grand_total": sum(totals.values())}


def observed_days(now_day: str, epoch_day: str = CK_EPOCH_DAY) -> int:
    """自埋点纪元日起的观测天数（含首尾；纪元日当天=1）。解析失败→0（保守）。"""
    try:
        d = (date.fromisoformat(now_day) - date.fromisoformat(epoch_day)).days + 1
        return max(0, d)
    except Exception:
        return 0


def _eta_days(observed: int, total: int, min_days: int, min_total: int) -> Optional[int]:
    """距「可裁决」还差几天：天数缺口与（有流量时的）样本缺口取更晚者。"""
    gap_d = max(0, min_days - observed)
    if total > 0 and observed > 0 and total < min_total:
        rate = total / observed
        gap_n = math.ceil((min_total - total) / rate) if rate > 0 else 0
        return max(gap_d, gap_n) or None
    return gap_d or None


def build_verdicts(agg: Dict[str, Any], *, now_day: str,
                   min_days: int = DEFAULT_MIN_DAYS,
                   min_total: int = DEFAULT_MIN_TOTAL,
                   epoch_day: str = CK_EPOCH_DAY) -> List[Dict[str, Any]]:
    """五个裁决问题 → 判词列表（纯函数）。

    status: ready（可裁决）/ insufficient（样本不足，带 eta_days）。
    confidence: high / low（比值型结论样本 < min_total 时降级；零点击不降）。
    """
    totals: Dict[str, int] = agg.get("totals") or {}

    def g(k: str) -> int:
        return int(totals.get(k) or 0)

    def g_prefix(pfx: str) -> int:
        return sum(int(v or 0) for k, v in totals.items() if k.startswith(pfx))

    obs = observed_days(now_day, epoch_day)
    broken_chain = int(agg.get("grand_total") or 0) == 0
    out: List[Dict[str, Any]] = []

    def push(question: str, evidence: Dict[str, int],
             decide: Callable[[int], Tuple[str, str]]) -> None:
        total = sum(evidence.values())
        if broken_chain:
            out.append({"question": question, "status": "insufficient",
                        "evidence": evidence,
                        "eta_days": max(1, min_days - obs) if obs < min_days else None,
                        "note": "窗口内 ck_* 全量为 0（含页面打开）——先查埋点链"
                                "（ops.ui_event_trend 开关 / beacon 路由），别急着下裁决"})
            return
        if obs < min_days:
            out.append({"question": question, "status": "insufficient",
                        "evidence": evidence,
                        "eta_days": _eta_days(obs, total, min_days, min_total),
                        "note": f"观测 {obs}/{min_days} 天（埋点纪元 {epoch_day}）"})
            return
        rec, conf = decide(total)
        out.append({"question": question, "status": "ready",
                    "confidence": conf, "evidence": evidence, "recommendation": rec})

    # Q1 页面采用度：ck_open 一打开就发——驾驶舱作为独立入口有没有人来
    def _q1(total: int) -> Tuple[str, str]:
        opens = g("ck_open")
        per_wk = round(opens / max(1, obs) * 7, 1)
        if opens == 0:
            return ("零访问——驾驶舱入口无人问津：提级入口（顶栏/首页卡）或并回收件箱，"
                    "别留一个没人开的页面", "high")
        return (f"页面打开 {opens} 次（约 {per_wk} 次/周）——有真实使用，入口维持", "high")

    push("页面采用度", {"ck_open": g("ck_open")}, _q1)

    # Q2 「已处理」出口使用度：resolve 是 needs_human 卡唯一清除出口
    def _q2(total: int) -> Tuple[str, str]:
        res, opens = g("ck_resolve"), g("ck_open")
        conf = "high" if (total >= min_total or res == 0) else "low"
        if res == 0 and opens > 0:
            return ("「已处理」零使用而页面有访问——needs_human 积压可能仍在陈尸，"
                    "查按钮可见性/文案，或坐席在别处摘标签", conf)
        if res == 0:
            return ("页面与按钮都零使用——随 Q1 一并裁决", conf)
        return (f"resolve 被真实使用（{res} 次）——积压清除出口成立", conf)

    push("「已处理」出口使用度", {"ck_resolve": g("ck_resolve"),
                                  "ck_open": g("ck_open")}, _q2)

    # Q3 分类筛选行去留：ck_filter_*（含回「全部」）
    def _q3(total: int) -> Tuple[str, str]:
        flt = g_prefix("ck_filter_")
        conf = "high" if (total >= min_total or flt == 0) else "low"
        if flt == 0:
            return ("筛选 chips 零点击——队列平时短到不用筛：可收掉筛选行省一行高度"
                    "（卡多时再回访本判词）", conf)
        return (f"筛选被真实使用（{flt} 次）——保留", conf)

    push("分类筛选行去留", {"ck_filter_*": g_prefix("ck_filter_")}, _q3)

    # Q4 历史积压折叠区：有没有人翻（0 = 折叠语义成立，积压确实不值得占屏）
    def _q4(total: int) -> Tuple[str, str]:
        so = g("ck_stale_open")
        if so == 0:
            return ("积压区从未被翻看——折叠语义成立；配合 Q2 若 resolve 也活跃，"
                    "可考虑给积压卡加批量「全部已处理」", "high")
        return (f"积压区被翻看 {so} 次——有人在管旧账，维持现状", "high")

    push("历史积压折叠区", {"ck_stale_open": g("ck_stale_open"),
                            "ck_stale_close": g("ck_stale_close")}, _q4)

    # Q5 接管/交还闭环：take 远多于 handback = 「接完不还」坐实
    def _q5(total: int) -> Tuple[str, str]:
        tk, hb = g("ck_take"), g("ck_handback")
        conf = "high" if (total >= min_total or total == 0) else "low"
        if tk == 0 and hb == 0:
            return ("本页无人接管/交还——坐席在收件箱操作或没有接管需求；"
                    "本页按钮维持（成本为零）", conf)
        if tk > 0 and hb * 2 < tk:
            return (f"「接完不还」坐实：接管 {tk} vs 交还 {hb}——考虑接管超时提醒"
                    "外推（webhook）或交还入口前置", conf)
        return (f"接管/交还大体闭环（{tk} vs {hb}）", conf)

    push("接管/交还闭环", {"ck_take": g("ck_take"),
                           "ck_handback": g("ck_handback")}, _q5)
    return out


def _report_for_root(root: Path, *, days: int, min_days: int, min_total: int,
                     now: Optional[float] = None) -> Dict[str, Any]:
    db = Path(root) / "config" / "ui_event_trend.db"
    rep: Dict[str, Any] = {"root": str(root), "db": str(db)}
    if CK_ENTRY_HIDDEN_DAY:
        rep["entry_hidden_since"] = CK_ENTRY_HIDDEN_DAY
    try:
        rows = read_rows(db, days=days, now=now)
    except FileNotFoundError:
        rep["missing"] = True
        rep["note"] = "趋势库不存在（ops.ui_event_trend 未开或尚无数据）——未创建空库"
        return rep
    agg = aggregate(rows)
    rep["window_days"] = days
    rep["totals"] = agg["totals"]
    rep["grand_total"] = agg["grand_total"]
    rep["first_seen"] = agg["first_seen"]
    rep["verdicts"] = build_verdicts(
        agg, now_day=_utc_day(now), min_days=min_days, min_total=min_total)
    return rep


def render_text(rep: Dict[str, Any]) -> List[str]:
    lines = [f"== 数据根 {rep['root']}", f"   库 {rep['db']}"]
    hidden = rep.get("entry_hidden_since")
    if hidden:
        lines.append(
            f"   [入口已隐藏] {hidden} 起顶栏「驾驶舱」入口随 ui_visibility.cockpit "
            "关闭——此后的零点击是「进不来」不是「没人用」，别据此裁决；"
            "重开该键后把 CK_EPOCH_DAY 挪到重开日重新计窗")
    if rep.get("missing"):
        lines.append(f"   [MISS] {rep['note']}")
        return lines
    lines.append(f"   窗口 {rep['window_days']} 天 · ck_* 总量 {rep['grand_total']}")
    totals = rep.get("totals") or {}
    for a, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"     {a:<24} {n}")
    for v in rep.get("verdicts") or []:
        if v["status"] == "ready":
            tag = "裁决可下" + ("·低样本" if v.get("confidence") == "low" else "")
            lines.append(f"   [{tag}] {v['question']}：{v['recommendation']}")
        else:
            eta = v.get("eta_days")
            when = ""
            if eta:
                try:
                    when = f" → 预计 {(date.today() + timedelta(days=int(eta))).isoformat()}"
                except Exception:
                    when = ""
            lines.append(f"   [样本不足] {v['question']}：{v.get('note', '')}"
                         + (f"（约还需 {eta} 天{when}）" if eta else ""))
        lines.append(f"           证据 {v['evidence']}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="驾驶舱页用量裁决读数（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="读取窗口天数")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS, help="可裁决的最少观测天数")
    ap.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL, help="比值型结论的高置信样本量")
    ap.add_argument("--data-root", default="", help="显式数据根（默认按 _data_root 契约解析）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    roots = resolve_data_roots(args.data_root)
    reports = [_report_for_root(Path(r), days=args.days, min_days=args.min_days,
                                min_total=args.min_total) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            for ln in render_text(rep):
                print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
