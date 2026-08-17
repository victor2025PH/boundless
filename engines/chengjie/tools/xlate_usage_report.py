# -*- coding: utf-8 -*-
"""对话翻译弹层用量裁决读数 CLI（只读；xlate P0-P2 收敛后「数据裁决」的决策工具）。

背景
----
2026-08-16 翻译弹层三批收敛（P0 一键开关/坐席语言默认/管理弹窗修活、P1 信息架构
重排/智能建议/坐席级我的语言、P2 vi 词包）后，遗留五个**读数决策**：智能建议条
转化如何、一键开关是否高频（暗示 smart default 不够）、「先预览再发」值不值得
做新手默认、管理弹窗要不要升级设置中心 tab、服务端「我的语言」有没有人用。
埋点全走 ``_uiBeacon`` 的 ``xlt_*`` 前缀 → ``ops.ui_event_trend`` 按日落库
（``<数据根>/config/ui_event_trend.db``）。

**只读、不改任何行为**（对齐 ``tools/inbox_filter_usage_report.py`` 家法：只读、
逐数据根、判词带样本闸门与 ETA；``mode=ro`` URI，库不存在不创建空库）。

判词设计（纯函数，喂 rows 即可单测）
----
- **埋点纪元日** ``XLT_EPOCH_DAY``：全部 xlt_* 埋点同日上线（2026-08-16 P1）。
- **零点击是合法证据**：满 ``min_days`` 后 total==0 照样出裁决；尤其「建议条
  零曝光」是**好消息**（曝光条件=双向全关，smart default 把外语会话都点亮了）。
- **埋点链自证**：窗口内 xlt_* 全量为 0 → 一律「样本不足」并点名先查链路。

用法
----
    python tools/xlate_usage_report.py [--days 30] [--min-days 14]
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

# 埋点纪元日。代码 2026-08-16 上线，但**当天含真浏览器门禁（verify_xlate_ui）的
# 真实点击噪声**（首跑实测 22 条基本全是门禁产物；此后门禁已 context 级吞 beacon
# 不再污染）→ 纪元定次日，且 read 侧把早于纪元的行整体隔离（quarantine_pre_epoch），
# 保证裁决样本从第一天起就是干净的坐席行为。
XLT_EPOCH_DAY = "2026-08-17"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 xlt_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'xlt\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                for r in rows]
    finally:
        conn.close()


def quarantine_pre_epoch(rows: List[Dict[str, Any]],
                         epoch_day: str = XLT_EPOCH_DAY) -> List[Dict[str, Any]]:
    """剔除早于纪元日的行（版本上线当天的门禁点击噪声不进裁决样本）。"""
    return [r for r in rows or [] if str(r.get("day") or "") >= epoch_day]


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


def observed_days(now_day: str, epoch_day: str = XLT_EPOCH_DAY) -> int:
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
                   epoch_day: str = XLT_EPOCH_DAY) -> List[Dict[str, Any]]:
    """五个裁决问题 → 判词列表（纯函数）。

    status: ready（可裁决）/ insufficient（样本不足，带 eta_days）。
    confidence: high / low（比值型结论样本 < min_total 时降级；零点击不降）。
    """
    totals: Dict[str, int] = agg.get("totals") or {}

    def g(k: str) -> int:
        return int(totals.get(k) or 0)

    obs = observed_days(now_day, epoch_day)
    broken_chain = int(agg.get("grand_total") or 0) == 0
    out: List[Dict[str, Any]] = []

    def push(question: str, actions: List[str],
             decide: Callable[[int], Tuple[str, str]]) -> None:
        total = sum(g(a) for a in actions)
        ev = {a: g(a) for a in actions}
        if broken_chain:
            out.append({"question": question, "status": "insufficient",
                        "evidence": ev,
                        "eta_days": max(1, min_days - obs) if obs < min_days else None,
                        "note": "窗口内 xlt_* 全量为 0——先查埋点链"
                                "（ops.ui_event_trend 开关 / beacon 路由），别急着下裁决"})
            return
        if obs < min_days:
            out.append({"question": question, "status": "insufficient",
                        "evidence": ev,
                        "eta_days": _eta_days(obs, total, min_days, min_total),
                        "note": f"观测 {obs}/{min_days} 天（埋点纪元 {epoch_day}）"})
            return
        rec, conf = decide(total)
        out.append({"question": question, "status": "ready",
                    "confidence": conf, "evidence": ev, "recommendation": rec})

    # Q1 智能建议条：曝光→接受转化（曝光条件=会话语言≠坐席语言且双向全关）
    def _q1(total: int) -> Tuple[str, str]:
        shown, accept = g("xlt_suggest_shown"), g("xlt_suggest_accept")
        conf = "high" if (shown >= min_total or shown == 0) else "low"
        if shown == 0:
            return ("建议条零曝光=好消息：smart default 已把外语会话点亮"
                    "（曝光前提是双向全关）；机制保留零成本，不动", conf)
        rate = accept / shown
        if rate >= 0.3:
            return (f"建议条高转化（{accept}/{shown}≈{rate:.0%}）：保留；"
                    "可考虑把曝光面提前到换会话时（当前在弹层内）", conf)
        if rate < 0.1:
            return (f"建议条低转化（{accept}/{shown}≈{rate:.0%}）：文案/位置再调"
                    "或收窄曝光条件——坐席在刻意保持关闭时别反复劝", conf)
        return (f"建议条转化中位（{accept}/{shown}≈{rate:.0%}）：维持现状继续观察", conf)

    push("智能建议条转化", ["xlt_suggest_shown", "xlt_suggest_accept"], _q1)

    # Q2 一键开关频次（背景读数：高频 quick_on 暗示默认值仍不够聪明）
    def _q2(total: int) -> Tuple[str, str]:
        on, off = g("xlt_quick_on"), g("xlt_quick_off")
        per_wk = round((on + off) / max(1, obs) * 7, 1)
        if on >= min_total and on >= off * 3:
            return (f"quick_on 高频（{on} 开 vs {off} 关，约 {per_wk} 次/周）："
                    "说明默认没把该开的会话点亮——查 smart default 抑制条件是否过严", "high")
        return (f"开 {on} / 关 {off}（约 {per_wk} 次/周）：频次正常，默认值健康", "high")

    push("一键开关频次（默认值健康度）", ["xlt_quick_on", "xlt_quick_off"], _q2)

    # Q3 「先预览再发」采用度：要不要做成新手默认
    def _q3(total: int) -> Tuple[str, str]:
        on, off = g("xlt_preview_on"), g("xlt_preview_off")
        conf = "high" if (total >= min_total or total == 0) else "low"
        if on == 0:
            return ("无人开预览：维持「一击直发」默认；分段控件保留（低成本安全阀）", conf)
        if on >= max(off * 2, min_total // 2):
            return (f"预览需求真实（开 {on} vs 关 {off}）：评估把「先预览再发」"
                    "设为新坐席默认（信任建立期），老坐席跟随记忆", conf)
        return (f"预览少量使用（开 {on} vs 关 {off}）：维持现状", conf)

    push("「先预览再发」采用度", ["xlt_preview_on", "xlt_preview_off"], _q3)

    # Q4 管理弹窗使用：要不要升级「翻译设置中心」tab
    def _q4(total: int) -> Tuple[str, str]:
        opens = g("xlt_mgr_open")
        per_wk = round(opens / max(1, obs) * 7, 1)
        if per_wk >= 3:
            return (f"管理弹窗高频（{opens} 次，约 {per_wk} 次/周）：值得升级"
                    "「翻译设置中心」页级 tab（含引擎偏好列表）", "high")
        return (f"管理弹窗低频（{opens} 次，约 {per_wk} 次/周）：维持弹窗形态，"
                "设置中心不投入", "high")

    push("管理弹窗 → 设置中心？", ["xlt_mgr_open"], _q4)

    # Q5 服务端「我的语言」采用度
    def _q5(total: int) -> Tuple[str, str]:
        n = g("xlt_agent_lang_set")
        if n > 0:
            return (f"「我的语言」被真实使用（{n} 次设置）：保留；下一步可在"
                    "个人设置页加同款入口（弹层高级折叠较深）", "high")
        return ("零设置：浏览器推导已覆盖需求（或入口太深）——维持现状，"
                "待建议条/quick 数据佐证后再决定是否提级入口", "high")

    push("服务端「我的语言」采用度", ["xlt_agent_lang_set"], _q5)
    return out


def _report_for_root(root: Path, *, days: int, min_days: int, min_total: int,
                     now: Optional[float] = None) -> Dict[str, Any]:
    db = Path(root) / "config" / "ui_event_trend.db"
    rep: Dict[str, Any] = {"root": str(root), "db": str(db)}
    try:
        rows = read_rows(db, days=days, now=now)
    except FileNotFoundError:
        rep["missing"] = True
        rep["note"] = "趋势库不存在（ops.ui_event_trend 未开或尚无数据）——未创建空库"
        return rep
    agg = aggregate(quarantine_pre_epoch(rows))
    rep["window_days"] = days
    rep["totals"] = agg["totals"]
    rep["grand_total"] = agg["grand_total"]
    rep["first_seen"] = agg["first_seen"]
    rep["verdicts"] = build_verdicts(
        agg, now_day=_utc_day(now), min_days=min_days, min_total=min_total)
    return rep


def render_text(rep: Dict[str, Any]) -> List[str]:
    lines = [f"== 数据根 {rep['root']}", f"   库 {rep['db']}"]
    if rep.get("missing"):
        lines.append(f"   [MISS] {rep['note']}")
        return lines
    lines.append(f"   窗口 {rep['window_days']} 天 · xlt_* 总量 {rep['grand_total']}")
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
            lines.append(f"   [样本不足] {v['question']}：{v.get('note','')}"
                         + (f"（约还需 {eta} 天{when}）" if eta else ""))
        lines.append(f"           证据 {v['evidence']}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="对话翻译弹层用量裁决读数（只读）")
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
