# -*- coding: utf-8 -*-
"""收件箱筛选区用量裁决读数 CLI（只读；P0-P3 收敛后「数据裁决周」的决策工具）。

背景
----
2026-08-11 筛选区四批收敛（P0 需人工一等公民 / P1 统一筛选面板 / P2 chip 规格统一 /
P3 口径对齐 72h）后，遗留四个**读数决策**：主行「我的」chip 去留、tag-strip 退役与否、
视图预设值不值得建、面板采用度如何。埋点全走 ``_uiBeacon`` 的 ``iflt_*`` 前缀 →
``ops.ui_event_trend`` 按日落库（``<数据根>/config/ui_event_trend.db``）。数据在攒，
但**没有一个地方把「样本到没到、证据指向哪」读出来**——本 CLI 补这块（对齐
``tools/inject_extract_report.py`` 家法：只读、逐数据根、判词带样本闸门与 ETA）。

**只读、不改任何行为**：真正撤 chip / 退役 strip / 建预设，是看完本报告后的显式施工
决定。DB 不存在（``ops.ui_event_trend`` 未开 / 尚无数据）→ 明确提示，**不创建空库**；
读取走 ``mode=ro`` URI，对活体生产库零写事务。

判词设计（纯函数，喂 rows 即可单测）
----
- **埋点纪元日** ``IFLT_EPOCH_DAY``：全部 iflt_* 埋点同日上线（2026-08-11 P0-P4）。
  观测天数按「今天 - 纪元日」算而**不是**按数据首现日——否则「strip 两周 0 点击」
  分不清是「没人用」还是「埋点还没装」。
- **零点击是合法证据**：满 ``min_days`` 后 total==0 照样出裁决（零就是答案），
  ``min_total`` 只用来给「小样本比值」降置信（1 vs 2 这种别当真理）。
- **埋点链自证**：窗口内 iflt_* 全量为 0 → 一律「样本不足」并点名先查链路
  （上线当天实测已有数据，两周后全 0 更可能是链断了而不是没人点）。

用法
----
    python tools/inbox_filter_usage_report.py [--days 30] [--min-days 14]
        [--min-total 20] [--data-root PATH] [--json]

数据根按 ``scripts/_data_root`` 契约解析（CLI → AITR_DATA_ROOT → 自动发现活跃实例 →
引擎根）；多实例逐根输出。
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

# 全部 iflt_* 埋点的上线日（P0 主行 chips / P1 面板项 / P2 claimed_m / P4 striptag、
# sort_sel、view_save 同日）。晚于纪元日的新分桶**按问题挂独立纪元**（push(epoch=)），
# 否则新桶的「零点击」会被全局窗口读成「满窗无人用」——分不清没人用还是还没装。
IFLT_EPOCH_DAY = "2026-08-11"
# 群焦点过滤分桶（iflt_gfocus 主行 / iflt_gfocus_m 面板镜像）2026-08-20 上线，
# 晚于全局纪元 9 天，观测天数必须按自己的纪元算。
IFLT_GFOCUS_EPOCH_DAY = "2026-08-20"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 iflt_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'iflt\\_%' ESCAPE '\\' ORDER BY day",
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


def observed_days(now_day: str, epoch_day: str = IFLT_EPOCH_DAY) -> int:
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
                   epoch_day: str = IFLT_EPOCH_DAY) -> List[Dict[str, Any]]:
    """四个裁决问题 → 判词列表（纯函数）。

    status: ready（可裁决）/ insufficient（样本不足，带 eta_days）。
    confidence: high / low（比值型结论样本 < min_total 时降级；零点击不降——零就是答案）。
    """
    totals: Dict[str, int] = agg.get("totals") or {}

    def g(k: str) -> int:
        return int(totals.get(k) or 0)

    obs = observed_days(now_day, epoch_day)
    broken_chain = int(agg.get("grand_total") or 0) == 0
    out: List[Dict[str, Any]] = []

    def push(question: str, actions: List[str],
             decide: Callable[[int], Tuple[str, str]], *,
             epoch: Optional[str] = None) -> None:
        # epoch=晚于全局纪元上线的分桶专属纪元（观测天数按它算，防「还没装」误读「无人用」）
        q_epoch = epoch or epoch_day
        q_obs = observed_days(now_day, q_epoch) if epoch else obs
        total = sum(g(a) for a in actions)
        ev = {a: g(a) for a in actions}
        if broken_chain:
            out.append({"question": question, "status": "insufficient",
                        "evidence": ev, "eta_days": max(1, min_days - q_obs) if q_obs < min_days else None,
                        "note": "窗口内 iflt_* 全量为 0——先查埋点链"
                                "（ops.ui_event_trend 开关 / beacon 路由），别急着下裁决"})
            return
        if q_obs < min_days:
            out.append({"question": question, "status": "insufficient",
                        "evidence": ev,
                        "eta_days": _eta_days(q_obs, total, min_days, min_total),
                        "note": f"观测 {q_obs}/{min_days} 天（埋点纪元 {q_epoch}）"})
            return
        rec, conf = decide(total)
        out.append({"question": question, "status": "ready",
                    "confidence": conf, "evidence": ev, "recommendation": rec})

    # Q1 主行「我的」chip 去留（P2 起双入口分桶：iflt_claimed=主行 / iflt_claimed_m=面板）
    def _q1(total: int) -> Tuple[str, str]:
        row, panel = g("iflt_claimed"), g("iflt_claimed_m")
        conf = "high" if (total >= min_total or total == 0) else "low"
        if row == 0 and panel == 0:
            return ("两处入口都无人点——撤主行 chip（面板入口保留即可，`我的` 语义由认领流承载）", conf)
        if row == 0:
            return (f"撤主行 chip：面板入口已完全承接（面板 {panel} 次 vs 主行 0 次）", conf)
        if row >= panel:
            return (f"保留主行 chip：主行 {row} 次 >= 面板 {panel} 次，是真实高频入口", conf)
        return (f"保留但可降序：面板为主（{panel} 次），主行仍有 {row} 次真实点击", conf)

    push("主行「我的」chip 去留", ["iflt_claimed", "iflt_claimed_m"], _q1)

    # Q2 tag-strip 退役与否（P4 起 strip 点击有独立分桶 iflt_striptag）
    def _q2(total: int) -> Tuple[str, str]:
        strip, ptag, lib = g("iflt_striptag"), g("iflt_ptag"), g("iflt_tags_open")
        conf = "high" if (total >= min_total or strip == 0) else "low"
        if strip == 0 and (ptag + lib) > 0:
            return (f"退役 tag-strip（省一常驻行）：strip 0 次，面板标签 {ptag} 次 + 标签库 {lib} 次已承接", conf)
        if strip == 0:
            return ("标签筛选整体无人用（strip/面板/库全 0）——退役 strip，面板标签组维持隐藏语义即可", conf)
        return (f"保留 tag-strip：{strip} 次真实点击（面板 {ptag} / 库 {lib}）", conf)

    push("tag-strip 退役与否", ["iflt_striptag", "iflt_ptag", "iflt_tags_open"], _q2)

    # Q3 视图预设值不值得建（保存视图的真实使用习惯是最直接的需求信号）
    def _q3(total: int) -> Tuple[str, str]:
        save = g("iflt_view_save")
        if save >= 3:
            return (f"值得建内置预设：{save} 次真实保存（有组合筛选习惯，预设=帮他们省这一步）", "high")
        return (f"内置预设优先级低：保存视图仅 {save} 次——先不投入，复用保存视图机制即可", "high")

    push("视图预设值不值得建", ["iflt_view_save"], _q3)

    # Q4 面板采用度（背景读数：其余判词的解释语境，恒 ready）
    def _q4(total: int) -> Tuple[str, str]:
        opens = g("iflt_more_open")
        mirror = g("iflt_sort_recent") + g("iflt_sort_urgent") + g("iflt_sort_unread")
        sel = g("iflt_sort_sel")
        per_wk = round(opens / max(1, obs) * 7, 1)
        return (f"面板打开 {opens} 次（约 {per_wk} 次/周）；排序：面板镜像 {mirror} vs 原生下拉 {sel}"
                f"（镜像恒 0 可在下轮收掉排序组）", "high")

    push("面板采用度（背景读数）", ["iflt_more_open", "iflt_sort_recent",
                                    "iflt_sort_urgent", "iflt_sort_unread", "iflt_sort_sel"], _q4)

    # Q5 群焦点过滤「@我/未读」去留（P2 2026-08-20 上线，独立纪元）。
    # 入口只在群/频道 scope 可见 → 基数天然小于 Q1-Q4：零点击先对照 scope 本身
    # 用量（iflt_scope_group/channel）再定性——scope 没人进就不是过滤的问题。
    gf_obs = observed_days(now_day, IFLT_GFOCUS_EPOCH_DAY)

    def _q5(total: int) -> Tuple[str, str]:
        row, panel = g("iflt_gfocus"), g("iflt_gfocus_m")
        scope_n = g("iflt_scope_group") + g("iflt_scope_group_m") \
            + g("iflt_scope_channel") + g("iflt_scope_channel_m")
        conf = "high" if (total >= min_total or total == 0) else "low"
        if row == 0 and panel == 0:
            if scope_n == 0:
                return (f"{gf_obs} 天零点击但群/频道 scope 本身也零进入（scope {scope_n} 次）"
                        "——不是过滤的问题，维持现状，先看 scope 推广/群量", conf)
            return (f"{gf_obs} 天零点击而 scope 有 {scope_n} 次进入——收掉主行 chip，"
                    "面板镜像保留即可（代码路径零成本）", conf)
        if row == 0:
            return (f"撤主行 chip：面板镜像已承接（面板 {panel} 次 vs 主行 0 次）", conf)
        if row >= 5 and row >= panel:
            return (f"保留主行 chip：{row} 次真实点击（面板 {panel} 次）；"
                    "下一步可实验「群量大的坐席默认开」", conf)
        return (f"保留观察：主行 {row} / 面板 {panel} 次，量小先不动", conf)

    push("群焦点过滤「@我/未读」去留", ["iflt_gfocus", "iflt_gfocus_m"], _q5,
         epoch=IFLT_GFOCUS_EPOCH_DAY)
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
    if rep.get("missing"):
        lines.append(f"   [MISS] {rep['note']}")
        return lines
    lines.append(f"   窗口 {rep['window_days']} 天 · iflt_* 总量 {rep['grand_total']}")
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
    ap = argparse.ArgumentParser(description="收件箱筛选区用量裁决读数（只读）")
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
