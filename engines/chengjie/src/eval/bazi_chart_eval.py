"""命盘质量评测（BaziQA 式门禁的确定性底座；缺 lunar_python 优雅跳过）。

四条轨全部**确定性、零网络、零 LLM**——守的是排盘/解释层的算法正确性，
不是 LLM 解读文采（那属于将来 opt-in 的 LLM 轨）：

  1. **四柱金标**（外部真值回归钉）：锚点含公开史料可查的日柱（1949-10-01 开国大典
     =甲子日、2000-01-01 千禧元旦=戊午日）+ 编写时经万年历交叉核对的命例；升级
     lunar_python / 改造引擎时任何漂移立刻点名。
  2. **十神双实现交叉验证**：日期扫描下 ``shishen_between``（本仓独立实现）与
     lunar_python 的 ``getShiShenGan`` 全盘比对——两套实现口径必须一致。
  3. **强弱/喜用一致性不变量**：比例远离阈值时判词不得矛盾、判词↔喜用候选映射
     不得漂移、五行计数守恒（防「各处喜忌口径不一致」的竞品级事故）。
  4. **K 线评分健全性**：分数夹界 + 确定性 + 喜忌年份分差方向正确。

CLI：``python -m scripts.run_eval --bazi [--json]``；门禁 ``tests/test_bazi_chart_eval.py``。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

# ── 轨 1：四柱金标（编写时外部核对；日柱两例有公开史料锚点） ─────────────────────
# (y, m, d, 年柱, 月柱, 日柱, 备注)；一律按正午计（只校验年/月/日柱，不含时柱）。
GOLDEN_PILLARS: List[tuple] = [
    (1949, 10, 1, "己丑", "癸酉", "甲子", "开国大典（甲子日=公开史料）"),
    (2000, 1, 1, "己卯", "丙子", "戊午", "千禧元旦（戊午日=万年历公开）"),
    (1995, 3, 5, "乙亥", "戊寅", "乙未", "Phase1 金标命例"),
    (2008, 8, 8, "戊子", "庚申", "庚辰", "京奥开幕"),
    (1984, 2, 4, "癸亥", "乙丑", "戊辰", "甲子年立春日正午（立春时刻在午后→仍属癸亥）"),
    (2012, 12, 21, "壬辰", "壬子", "丙辰", ""),
    (1975, 6, 30, "乙卯", "壬午", "丁未", ""),
    (2026, 7, 12, "丙午", "乙未", "丁亥", "2026-07 实测锚点"),
]

# 强弱不变量的保守边界（引擎阈值 0.55/0.40 外留 margin，防边界抖动误报）
_STRONG_FLOOR = 0.60   # 比例 ≥ 此值绝不可判「偏弱」
_WEAK_CEIL = 0.35      # 比例 ≤ 此值绝不可判「偏强」


def _sweep_dates(start_year: int, years: int, step_days: int) -> List[_dt.date]:
    out: List[_dt.date] = []
    d = _dt.date(int(start_year), 1, 7)
    end = _dt.date(int(start_year) + int(years), 1, 1)
    step = _dt.timedelta(days=max(1, int(step_days)))
    while d < end:
        out.append(d)
        d += step
    return out


def evaluate_bazi_chart(
    *,
    sweep_start: int = 1970,
    sweep_years: int = 50,
    sweep_step_days: int = 53,
) -> Dict[str, Any]:
    """跑四条确定性轨；缺 lunar_python → ``{"available": False, "passed": None}``。"""
    from src.companion.bazi_engine import bazi_available

    if not bazi_available():
        return {"available": False, "passed": None,
                "reason": "lunar_python 未安装（pip install lunar_python）"}

    from src.companion.bazi_engine import (
        BirthInfo, compute_bazi, reset_chart_cache, shishen_between,
    )
    from src.companion.bazi_kline import year_score

    reset_chart_cache()

    # ── 轨 1：四柱金标 ──
    pillar_errors: List[Dict[str, Any]] = []
    for (y, m, d, gy, gm, gd, note) in GOLDEN_PILLARS:
        c = compute_bazi(BirthInfo(y, m, d, 12, 0))
        got = ("?", "?", "?") if not c else (
            c["pillars"]["year"]["ganzhi"],
            c["pillars"]["month"]["ganzhi"],
            c["pillars"]["day"]["ganzhi"],
        )
        if got != (gy, gm, gd):
            pillar_errors.append({
                "date": f"{y}-{m:02d}-{d:02d}", "expect": [gy, gm, gd],
                "got": list(got), "note": note})
    pillars_total = len(GOLDEN_PILLARS)
    pillars_ok = pillars_total - len(pillar_errors)

    # ── 轨 2/3：日期扫描（十神交叉验证 + 强弱不变量 + 五行守恒） ──
    dates = _sweep_dates(sweep_start, sweep_years, sweep_step_days)
    shishen_mismatch: List[Dict[str, Any]] = []
    strength_violations: List[Dict[str, Any]] = []
    charts_ok = 0
    for dt in dates:
        c = compute_bazi(BirthInfo(dt.year, dt.month, dt.day, 12, 0))
        if not c:
            strength_violations.append({"date": str(dt), "kind": "chart_none"})
            continue
        charts_ok += 1
        day_gan = c["day_master"][0]
        # 十神：本仓独立实现 vs lunar_python（日柱=日主跳过）
        for name in ("year", "month", "time"):
            p = c["pillars"].get(name)
            if not p:
                continue
            ours = shishen_between(day_gan, p["gan"])
            if ours != p["shishen_gan"]:
                shishen_mismatch.append({
                    "date": str(dt), "pillar": name, "gan": p["gan"],
                    "ours": ours, "lunar": p["shishen_gan"]})
        # 强弱不变量
        st = c.get("strength") or {}
        ratio = float(st.get("same_party_ratio") or 0)
        verdict = str(st.get("verdict") or "")
        xy = list(st.get("xi_yong_candidates") or [])
        if verdict not in ("偏强", "偏弱", "中和"):
            strength_violations.append(
                {"date": str(dt), "kind": "bad_verdict", "verdict": verdict})
        if ratio >= _STRONG_FLOOR and verdict == "偏弱":
            strength_violations.append(
                {"date": str(dt), "kind": "ratio_verdict_conflict",
                 "ratio": ratio, "verdict": verdict})
        if ratio <= _WEAK_CEIL and verdict == "偏强":
            strength_violations.append(
                {"date": str(dt), "kind": "ratio_verdict_conflict",
                 "ratio": ratio, "verdict": verdict})
        # 判词 ↔ 喜用候选映射（单一事实源不得漂移）
        n_xy = len(xy)
        if (verdict == "偏强" and n_xy != 3) or (verdict == "偏弱" and n_xy != 2) \
                or (verdict == "中和" and n_xy != 0):
            strength_violations.append(
                {"date": str(dt), "kind": "xiyong_mapping_drift",
                 "verdict": verdict, "xi_yong": xy})
        # 五行计数守恒（时辰已知=8 字 + 月令双计 = 9）
        total_wx = sum((c.get("wuxing_counts") or {}).values())
        if abs(total_wx - 9.0) > 1e-6:
            strength_violations.append(
                {"date": str(dt), "kind": "wuxing_total", "total": total_wx})

    # ── 轨 4：K 线评分健全性（用金标偏强命例：火土喜、水忌） ──
    kline_errors: List[str] = []
    kc = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))
    if kc:
        good = year_score(kc, 2026)   # 丙午（火火=双喜）
        bad = year_score(kc, 2032)    # 壬子（水水=双忌）
        again = year_score(kc, 2026)
        if not good or not bad:
            kline_errors.append("score_none")
        else:
            if good != again:
                kline_errors.append("nondeterministic")
            if good["score"] <= bad["score"]:
                kline_errors.append(
                    f"favorable_not_above_unfavorable ({good['score']} vs {bad['score']})")
            for y in range(2024, 2036):
                s = year_score(kc, y)
                if s and not (8 <= s["score"] <= 92):
                    kline_errors.append(f"score_out_of_range {y}={s['score']}")
    else:
        kline_errors.append("golden_chart_none")

    tracks = {
        "pillars": {
            "total": pillars_total, "ok": pillars_ok,
            "errors": pillar_errors, "passed": not pillar_errors,
        },
        "shishen_cross": {
            "charts": charts_ok, "mismatches": len(shishen_mismatch),
            "errors": shishen_mismatch[:10], "passed": not shishen_mismatch,
        },
        "strength_invariants": {
            "charts": charts_ok, "violations": len(strength_violations),
            "errors": strength_violations[:10], "passed": not strength_violations,
        },
        "kline_sanity": {
            "errors": kline_errors, "passed": not kline_errors,
        },
    }
    return {
        "available": True,
        "sweep": {"start": sweep_start, "years": sweep_years,
                  "step_days": sweep_step_days, "charts": charts_ok},
        "tracks": tracks,
        "passed": all(t["passed"] for t in tracks.values()),
    }


def format_bazi_chart_report(report: Dict[str, Any]) -> str:
    if not report.get("available"):
        return f"=== 命盘质量评测 ===\n[SKIP] {report.get('reason', '')}"
    t = report["tracks"]
    sw = report["sweep"]
    lines = [
        "=== 命盘质量评测报告（确定性四轨） ===",
        f"扫描: {sw['start']} 起 {sw['years']} 年 / 步长 {sw['step_days']} 天 / "
        f"{sw['charts']} 盘",
        f"1) 四柱金标      : {t['pillars']['ok']}/{t['pillars']['total']}  "
        f"{'[PASS]' if t['pillars']['passed'] else '[FAIL]'}",
        f"2) 十神交叉验证  : 不一致 {t['shishen_cross']['mismatches']}  "
        f"{'[PASS]' if t['shishen_cross']['passed'] else '[FAIL]'}",
        f"3) 强弱/喜用不变量: 违例 {t['strength_invariants']['violations']}  "
        f"{'[PASS]' if t['strength_invariants']['passed'] else '[FAIL]'}",
        f"4) K线评分健全性 : {'[PASS]' if t['kline_sanity']['passed'] else '[FAIL] ' + '; '.join(t['kline_sanity']['errors'])}",
        f"总判: {'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    for err in (t["pillars"]["errors"] or [])[:5]:
        lines.append(f"  四柱漂移: {err}")
    for err in (t["shishen_cross"]["errors"] or [])[:5]:
        lines.append(f"  十神不一致: {err}")
    for err in (t["strength_invariants"]["errors"] or [])[:5]:
        lines.append(f"  强弱违例: {err}")
    return "\n".join(lines)


__all__ = ["GOLDEN_PILLARS", "evaluate_bazi_chart", "format_bazi_chart_report"]
