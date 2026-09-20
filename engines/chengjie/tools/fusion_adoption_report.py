# -*- coding: utf-8 -*-
"""案例×学习融合采纳度裁决读数 CLI（只读；融合 P3，2026-08-16）。

背景
----
融合 P1/P2 在 /cases 页上线了三个「等数据裁决」的入口：页顶跨系统待办条
（pill 点击 ``ctdo_sla/drafts/crisis/learner``）、结案「顺手教 AI」喂料桥
（``ctdo_feed``）、告警链路引导条（``ctdo_alertlink_shown/dismiss``）。当时刻意
**没有**做更重的「统一待办中心页 / 简洁导航切换 / 统一卡片组件」——本 CLI 就是
那次延期决策的兑现工具：满观测窗后跑一次，判词直接回答「值不值得加码 / 该不该撤」。

家法对齐 ``tools/inbox_filter_usage_report.py``：只读、逐数据根、判词带样本闸门
与 ETA、零点击是合法证据、埋点链自证（全零先怀疑链路不下结论）。
喂料桥采纳率的分母＝同窗口案例结案数（``cases_trend.db``，case_trend_store 落的
按日聚合）；趋势库/结案库不存在一律如实标注，**绝不创建空库**（mode=ro URI）。

用法
----
    python tools/fusion_adoption_report.py [--days 30] [--min-days 14]
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

# 全部 ctdo_* 埋点同日上线（融合 P2/P3，2026-08-16）：pill 四分桶 + feed +
# alertlink 两分桶。观测天数按纪元日算而不是数据首现日——「两周零点击」必须
# 分得清「没人用」和「埋点还没装」。
CTDO_EPOCH_DAY = "2026-08-16"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20

_PILL_ACTIONS = ("ctdo_sla", "ctdo_drafts", "ctdo_crisis", "ctdo_learner")


def _utc_day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 ctdo_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'ctdo\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                for r in rows]
    finally:
        conn.close()


def read_closes(db_path: Any, *, days: int = DEFAULT_DAYS,
                now: Optional[float] = None) -> Optional[int]:
    """同窗口案例结案数（喂料桥采纳率分母）；库不存在/不可读 → None（如实降级）。"""
    p = Path(db_path)
    if not p.exists():
        return None
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(closed), 0) FROM case_trend_daily WHERE day >= ?",
                (cut,)).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        return None


def read_effect(db_path: Any, *, min_age_days: int = 3,
                now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """学习效果快照（knowledge_base.db 只读，kb_drafts 与 kb_query_log 同库同文件）。

    存在意义＝**固化易蒸发的数据**：kb_query_log 只滚动保留 7 天，不做周快照，
    8/30 之后想看「学习条目命中趋势」时历史已经没了。口径与 daily_learner 的
    效果回访/淘汰建议同源：``zero_hit_aged`` 带零流量防冤枉闸（日志零流量时
    返回 None＝「不可证」，绝不伪装成 0 或 N）。库缺席/旧库无表 → None。
    """
    p = Path(db_path)
    if not p.exists():
        return None
    t = now if now is not None else time.time()
    week_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t - 7 * 86400))
    age_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(t - max(1, int(min_age_days)) * 86400))
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT entry_id, reviewed_at FROM kb_drafts "
                "WHERE status='approved' AND entry_id != ''").fetchall()
            approved_7d = sum(
                1 for r in rows if str(r["reviewed_at"] or "") >= week_iso)
            traffic = int(conn.execute(
                "SELECT COUNT(*) FROM kb_query_log").fetchone()[0] or 0)
            hits_by: Dict[str, int] = {}
            for r in conn.execute(
                    "SELECT matched_entry_id, COUNT(*) AS c FROM kb_query_log "
                    "WHERE hit=1 AND matched_entry_id != '' "
                    "GROUP BY matched_entry_id"):
                hits_by[str(r["matched_entry_id"])] = int(r["c"])
            learner_ids = {str(r["entry_id"]) for r in rows}
            hits_on_learner = sum(hits_by.get(e, 0) for e in learner_ids)
            zero_hit_aged = (
                sum(1 for r in rows
                    if str(r["reviewed_at"] or "") <= age_iso
                    and hits_by.get(str(r["entry_id"]), 0) == 0)
                if traffic > 0 else None)
            return {
                "learner_entries": len(learner_ids),
                "approved_7d": approved_7d,
                "hits_on_learner_7d": hits_on_learner,
                "zero_hit_aged": zero_hit_aged,
                "query_log_rows": traffic,
            }
        finally:
            conn.close()
    except Exception:
        return None


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {totals, first_seen, grand_total}（纯函数）。"""
    totals: Dict[str, int] = {}
    first_seen: Dict[str, str] = {}
    for r in rows or []:
        a = str(r.get("action") or "")
        d = str(r.get("day") or "")
        n = int(r.get("n") or 0)
        if not a or not d:
            continue
        totals[a] = totals.get(a, 0) + n
        if n > 0 and (a not in first_seen or d < first_seen[a]):
            first_seen[a] = d
    return {"totals": totals, "first_seen": first_seen,
            "grand_total": sum(totals.values())}


def observed_days(now_day: str, epoch_day: str = CTDO_EPOCH_DAY) -> int:
    """自埋点纪元日的观测天数（含首尾；纪元日当天=1）。解析失败→0（保守）。"""
    try:
        d = (date.fromisoformat(now_day) - date.fromisoformat(epoch_day)).days + 1
        return max(0, d)
    except Exception:
        return 0


def _eta_days(observed: int, total: int, min_days: int, min_total: int) -> Optional[int]:
    gap_d = max(0, min_days - observed)
    if total > 0 and observed > 0 and total < min_total:
        rate = total / observed
        gap_n = math.ceil((min_total - total) / rate) if rate > 0 else 0
        return max(gap_d, gap_n) or None
    return gap_d or None


def build_verdicts(agg: Dict[str, Any], *, now_day: str,
                   closes: Optional[int] = None,
                   min_days: int = DEFAULT_MIN_DAYS,
                   min_total: int = DEFAULT_MIN_TOTAL,
                   epoch_day: str = CTDO_EPOCH_DAY) -> List[Dict[str, Any]]:
    """四个裁决问题 → 判词列表（纯函数，喂 agg 即可单测）。"""
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
            out.append({
                "question": question, "status": "insufficient", "evidence": ev,
                "eta_days": max(1, min_days - obs) if obs < min_days else None,
                "note": "窗口内 ctdo_* 全量为 0——先查埋点链"
                        "（ops.ui_event_trend 开关 / cases 页 _csBeacon），别急着下裁决"})
            return
        if obs < min_days:
            out.append({
                "question": question, "status": "insufficient", "evidence": ev,
                "eta_days": _eta_days(obs, total, min_days, min_total),
                "note": f"观测 {obs}/{min_days} 天（埋点纪元 {epoch_day}）"})
            return
        rec, conf = decide(total)
        out.append({"question": question, "status": "ready",
                    "confidence": conf, "evidence": ev, "recommendation": rec})

    # Q1 待办条整体值不值得加码（升级独立聚合页 / 简洁导航调整的判据）
    def _q1(total: int) -> Tuple[str, str]:
        conf = "high" if (total >= min_total or total == 0) else "low"
        per_wk = round(total / max(1, obs) * 7, 1)
        if total == 0:
            return ("撤待办条或换位置：满窗零点击，跨系统聚合的需求不成立，"
                    "更别提独立待办中心页", conf)
        if per_wk >= 10:
            return (f"待办条被高频使用（{per_wk} 次/周）——值得投入下一档："
                    "统一卡片组件/简洁导航常驻入口", conf)
        return (f"待办条低频有效（{per_wk} 次/周）——保留现状即可，"
                "独立聚合页暂不投入", conf)

    push("待办条值不值得加码", list(_PILL_ACTIONS), _q1)

    # Q2 各 pill 去留（零点击 pill 点名；条本身留去由 Q1 定）
    def _q2(total: int) -> Tuple[str, str]:
        conf = "high" if (total >= min_total or total == 0) else "low"
        dead = [a for a in _PILL_ACTIONS if g(a) == 0]
        live = {a: g(a) for a in _PILL_ACTIONS if g(a) > 0}
        if not dead:
            return (f"四个 pill 全部有真实点击：{live}——全保留", conf)
        if len(dead) == len(_PILL_ACTIONS):
            return ("全部 pill 零点击——随 Q1 撤条处理", conf)
        return (f"可撤零点击 pill：{dead}（有效 pill：{live}）", conf)

    push("各 pill 去留", list(_PILL_ACTIONS), _q2)

    # Q3 结案喂料桥采纳（分母=同窗结案数；缺分母按绝对量判）
    def _q3(total: int) -> Tuple[str, str]:
        feed = g("ctdo_feed")
        if closes is not None and closes > 0:
            rate = round(feed / closes * 100.0, 1)
            conf = "high" if closes >= min_total else "low"
            if rate >= 20:
                return (f"桥被采纳（{feed}/{closes} 结案 = {rate}%）——保留；"
                        "可考虑对 ai_doubt/要人工来源默认勾选", conf)
            if feed == 0:
                return (f"桥零使用（{closes} 次结案无一喂料）——检查文案/预填是否"
                        "对味，两周仍零再考虑收起该行", conf)
            return (f"桥低频使用（{feed}/{closes} 结案 = {rate}%）——保留观察，"
                    "不加码不撤", conf)
        conf = "high" if (feed >= min_total or feed == 0) else "low"
        if feed == 0:
            return ("桥零使用（结案分母不可读，按绝对量判）——检查文案或收起", conf)
        return (f"桥有真实使用（{feed} 次，结案分母不可读）——保留", conf)

    push("结案喂料桥采纳", ["ctdo_feed"], _q3)

    # Q4 告警引导条（背景读数：shown 持续出现=运营一直没接通；dismiss 高=打扰）
    def _q4(total: int) -> Tuple[str, str]:
        shown, dism = g("ctdo_alertlink_shown"), g("ctdo_alertlink_dismiss")
        if shown == 0:
            return ("引导条从未现身：告警通道早已接通（或案例页无人访问）——无债", "high")
        note = f"引导条现身 {shown} 次"
        if dism > 0:
            note += f"、被关 {dism} 次"
        note += "——通道仍未接通，问题在执行不在提示；把接通任务派给运营"
        return (note, "high")

    push("告警引导条（背景读数）",
         ["ctdo_alertlink_shown", "ctdo_alertlink_dismiss"], _q4)
    return out


def _report_for_root(root: Path, *, days: int, min_days: int, min_total: int,
                     now: Optional[float] = None) -> Dict[str, Any]:
    db = Path(root) / "config" / "ui_event_trend.db"
    trend_db = Path(root) / "config" / "cases_trend.db"
    kb_db = Path(root) / "config" / "knowledge_base.db"
    rep: Dict[str, Any] = {"root": str(root), "db": str(db)}
    # 效果快照独立于埋点趋势库（埋点没开也要固化学习命中——两者数据源不同）
    rep["effect"] = read_effect(kb_db, now=now)
    try:
        rows = read_rows(db, days=days, now=now)
    except FileNotFoundError:
        rep["missing"] = True
        rep["note"] = "趋势库不存在（ops.ui_event_trend 未开或尚无数据）——未创建空库"
        return rep
    agg = aggregate(rows)
    closes = read_closes(trend_db, days=days, now=now)
    rep["window_days"] = days
    rep["totals"] = agg["totals"]
    rep["grand_total"] = agg["grand_total"]
    rep["first_seen"] = agg["first_seen"]
    rep["closes_in_window"] = closes
    rep["verdicts"] = build_verdicts(
        agg, now_day=_utc_day(now), closes=closes,
        min_days=min_days, min_total=min_total)
    return rep


def append_trend_lines(reports: List[Dict[str, Any]], out_path: Any,
                       now: Optional[float] = None) -> int:
    """把每根的原始计数追加成 JSONL 趋势行（周批固化易蒸发数据的落点）。

    只存原始计数不存判词（判词可随时由计数重推，存了反而钉死旧词表）；
    缺库根也落行（missing=true）——「那周为什么没数」本身是趋势的一部分。
    返回写入行数；目录不存在自动建。
    """
    t = now if now is not None else time.time()
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("a", encoding="utf-8") as f:
        for rep in reports or []:
            line = {
                "ts": round(t, 1),
                "day": _utc_day(t),
                "root": rep.get("root", ""),
                "missing": bool(rep.get("missing")),
                "grand_total": rep.get("grand_total"),
                "totals": rep.get("totals") or {},
                "closes_in_window": rep.get("closes_in_window"),
                "window_days": rep.get("window_days"),
                "effect": rep.get("effect"),
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            n += 1
    return n


def render_text(rep: Dict[str, Any]) -> List[str]:
    lines = [f"== 数据根 {rep['root']}", f"   库 {rep['db']}"]
    eff = rep.get("effect")
    if eff:
        zh = eff.get("zero_hit_aged")
        lines.append(
            f"   学习效果：入库条目 {eff.get('learner_entries', 0)}"
            f"（本周 +{eff.get('approved_7d', 0)}）· 7 天命中 {eff.get('hits_on_learner_7d', 0)} 次"
            + (f" · 零命中(≥3天) {zh}" if zh is not None else " · 零命中不可证（日志零流量）"))
    if rep.get("missing"):
        lines.append(f"   [MISS] {rep['note']}")
        return lines
    closes = rep.get("closes_in_window")
    lines.append(
        f"   窗口 {rep['window_days']} 天 · ctdo_* 总量 {rep['grand_total']}"
        + (f" · 同窗结案 {closes}" if closes is not None else " · 结案分母不可读"))
    totals = rep.get("totals") or {}
    for a, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"     {a:<26} {n}")
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
    ap = argparse.ArgumentParser(description="案例×学习融合采纳度裁决读数（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="读取窗口天数")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                    help="可裁决的最少观测天数")
    ap.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL,
                    help="比值型结论的高置信样本量")
    ap.add_argument("--data-root", default="", help="显式数据根（默认按 _data_root 契约解析）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out-jsonl", default="",
                    help="把各根原始计数追加成 JSONL 趋势行（周批用；判词不入行）")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    reports = [
        _report_for_root(Path(r), days=args.days, min_days=args.min_days,
                         min_total=args.min_total)
        for r in roots
    ]
    if args.out_jsonl:
        n = append_trend_lines(reports, args.out_jsonl)
        print(f"[trend] appended {n} line(s) -> {args.out_jsonl}")
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            for ln in render_text(rep):
                print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
