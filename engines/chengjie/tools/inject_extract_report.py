# -*- coding: utf-8 -*-
"""注入抽取率校准读数 CLI（只读；「归零判 → 比率阈值」升级的决策工具）。

背景
----
``inbox.desktop_inject.trend_log`` 开启后，每条桌面壳健康上报的抽取计数按 (日,平台,账号)
落 ``config/inject_extract_trend.db``（见 ``src/web/inject_extract_trend.py``）。数据在攒，
``suggest_extract_threshold`` 也能给建议阈值，但**没有一个地方把「到没到切换时机、该切到多少」
读出来**——运营只能盯一个 SQLite 文件。本 CLI 补这块：读近 N 天分布，逐平台给出

  · 样本量 / 装饰率直方图 / 回流归零率
  · 建议阈值（复用 ``suggest_extract_thresholds``，误伤预算法，单一事实源）
  · 一句话判词：[可升级到 T] / [样本不足 n/min，约还需 X 天] / [维持归零判]

**只读、不改任何告警行为**：真正把 ``classify_inject_health`` 的归零判换成比率阈值，仍是
运营看完本报告后的显式决定（且应等样本够、分布稳）。本工具就是那个「看完」的依据。

用法
----
    python tools/inject_extract_report.py [--days 14] [--min-samples 300]
                                          [--max-fp 0.05] [--data-root PATH] [--json]

数据根按 ``scripts/_data_root`` 契约解析（CLI → AITR_DATA_ROOT → 自动发现活跃实例 → 引擎根）；
多实例逐根输出。DB 不存在（trend_log 未开 / 尚无数据）→ 明确提示，不创建空库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.web.inject_extract_trend import (  # noqa: E402
    InjectExtractTrendStore,
    suggest_extract_thresholds,
)

DEFAULT_MIN_SAMPLES = 300
DEFAULT_MAX_FP = 0.05
_BUCKET_LABELS = ["=0", "(0,.3]", "(.3,.7]", "(.7,1)", "=1"]


def build_report(
    rows: List[Dict[str, Any]], *, days: int,
    min_samples: int = DEFAULT_MIN_SAMPLES, max_fp: float = DEFAULT_MAX_FP,
) -> Dict[str, Any]:
    """把 daily() 行聚合成逐平台决策读数（纯函数：喂 rows 即可单测，无需 DB）。"""
    suggestions = suggest_extract_thresholds(
        rows, min_samples=min_samples, max_false_positive=max_fp)
    # 逐平台聚合直方图/回流/覆盖天数（供展示 + ETA 估算）
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        p = str(r.get("platform") or "")
        if not p:
            continue
        a = agg.setdefault(p, {"buckets": [0, 0, 0, 0, 0], "ingest_reports": 0,
                               "ingest_zero": 0, "days": set()})
        for i in range(5):
            a["buckets"][i] += int(r.get(f"ratio_b{i}") or 0)
        a["ingest_reports"] += int(r.get("ingest_reports") or 0)
        a["ingest_zero"] += int(r.get("ingest_zero") or 0)
        a["days"].add(r.get("day"))

    platforms: List[Dict[str, Any]] = []
    for s in suggestions:
        p = s["platform"]
        a = agg.get(p, {"buckets": [0] * 5, "ingest_reports": 0,
                        "ingest_zero": 0, "days": set()})
        reports = int(s.get("samples") or 0)
        ndays = len(a["days"]) or 0
        eta_days: Optional[float] = None
        if s.get("status") == "insufficient" and reports > 0 and ndays > 0:
            rate = reports / ndays
            if rate > 0:
                eta_days = round(max(0.0, (min_samples - reports) / rate), 1)
        ig = int(a["ingest_reports"])
        platforms.append({
            "platform": p,
            "status": s.get("status"),
            "threshold": s.get("threshold"),
            "samples": reports,
            "frac_le_03": s.get("frac_le_03", 0.0),
            "frac_le_07": s.get("frac_le_07", 0.0),
            "zero_rate": s.get("zero_rate", 0.0),
            "buckets": a["buckets"],
            "ingest_reports": ig,
            "ingest_zero_rate": round(a["ingest_zero"] / ig, 4) if ig else 0.0,
            "eta_days": eta_days,
            "verdict": _verdict(s, eta_days, min_samples, max_fp),
        })
    platforms.sort(key=lambda d: d["platform"])
    return {"days": days, "min_samples": min_samples, "max_fp": max_fp,
            "platforms": platforms}


def _verdict(s: Dict[str, Any], eta_days: Optional[float],
             min_samples: int, max_fp: float) -> str:
    reports = int(s.get("samples") or 0)
    if s.get("status") == "insufficient":
        if reports <= 0:
            return "无数据（trend_log 刚开或暂无壳上报）"
        eta = f"，按当前速率约还需 {eta_days} 天" if eta_days is not None else ""
        return f"样本不足 {reports}/{min_samples}{eta}"
    thr = s.get("threshold") or 0.0
    if thr and thr > 0:
        frac = s.get("frac_le_07", 0.0) if thr >= 0.7 else s.get("frac_le_03", 0.0)
        return (f"[可升级] 装饰率<{thr} 判失配（健康分布仅 {frac:.1%} 落该区，"
                f"≤误伤预算 {max_fp:.0%}）")
    return (f"[维持归零判] {reports} 样本，装饰率≤0.3 占 {s.get('frac_le_03', 0.0):.1%}"
            "（该平台天然低装饰/当前在坏，升阈值会误报）")


def render_text(report: Dict[str, Any], root: Path) -> str:
    lines = [f"== 注入抽取率校准读数 · {root} ==",
             f"   窗口 {report['days']} 天 · 样本门槛 {report['min_samples']} · "
             f"误伤预算 {report['max_fp']:.0%}"]
    plats = report.get("platforms") or []
    if not plats:
        lines.append("   （窗口内无样本——trend_log 刚开、或还没有带 extract 计数的壳上报）")
        return "\n".join(lines)
    for p in plats:
        b = p["buckets"]
        hist = " ".join(f"{_BUCKET_LABELS[i]}:{b[i]}" for i in range(5))
        lines.append("")
        lines.append(f"   [{p['platform']}] 样本 {p['samples']} · "
                     f"归零率 {p['zero_rate']:.1%} · 回流归零率 {p['ingest_zero_rate']:.1%}")
        lines.append(f"     装饰率直方图  {hist}")
        lines.append(f"     判词          {p['verdict']}")
    return "\n".join(lines)


def _report_for_root(root: Path, *, days: int, min_samples: int,
                     max_fp: float = DEFAULT_MAX_FP) -> Optional[Dict[str, Any]]:
    """打开该根的 trend DB（不存在→None，绝不创建空库），构建报告。"""
    db = Path(root) / "config" / "inject_extract_trend.db"
    if not db.is_file():
        return None
    store = InjectExtractTrendStore(str(db))
    rows = store.daily(days=days)
    return build_report(rows, days=days, min_samples=min_samples, max_fp=max_fp)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="注入抽取率校准读数（只读决策工具）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    ap.add_argument("--max-fp", type=float, default=DEFAULT_MAX_FP)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    out_json: List[Dict[str, Any]] = []
    text_blocks: List[str] = []
    for root in roots:
        rep = _report_for_root(root, days=args.days, min_samples=args.min_samples,
                               max_fp=args.max_fp)
        if rep is None:
            text_blocks.append(f"== {root} ==\n   未落库（inbox.desktop_inject.trend_log "
                               "未开，或该根下尚无 inject_extract_trend.db）")
            out_json.append({"root": str(root), "db": False})
            continue
        rep["root"] = str(root)
        out_json.append(rep)
        text_blocks.append(render_text(rep, root))

    if args.json:
        print(json.dumps(out_json, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(text_blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
