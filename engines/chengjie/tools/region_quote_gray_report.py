"""I-4 灰度周读：地区语气档命中率 + 自动链引用回复决策分布（只读）。

    python tools/region_quote_gray_report.py [--days 7] [--data-root D:\\chengjie-instances\\zhiliao\\data] [--json]

读 `<数据根>/logs/i4_gray/{quote,region}_*.jsonl`（`src/ops/region_quote_gray.py` 落盘），
多实例数据根自动发现（`scripts/_data_root` 契约）。输出两段判词：

* 引用回复：决策数 / 引用率 / 不引用原因分布 / low_relevance 分数直方图 /
  **建议 min_relevance**（low_relevance ≥ 20 条才给；否则「样本不足，继续攒」）。
  `min_relevance` 当前值从实例合并配置读（`inbox.l2_autosend.quote_reply.min_relevance`）。
* 地区档：每档 observed / 禁用词命中率 / 简体漏出率 / Top 命中词 / **L4 判词**
  （observed ≥ 30 且命中率 ≥ 10% → 建议开 L4 负样本改写；否则观测即可）。

零流量不是错误：exit 0 并如实说「无记录」。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.ops import region_quote_gray as g  # noqa: E402


def _cur_min_relevance(root: Path) -> float:
    try:
        cfg = load_merged_config(root)
        from src.inbox.reply_quote_policy import parse_quote_cfg
        return float(parse_quote_cfg(cfg).get("min_relevance", 0.15))
    except Exception:  # noqa: BLE001
        return 0.15


def report_root(root: Path, days: int) -> Dict[str, Any]:
    base = root / g.DEFAULT_DIR
    q = g.summarize_quote(g.iter_records("quote", days, base=base),
                          cur_min_relevance=_cur_min_relevance(root))
    r = g.summarize_region(g.iter_records("region", days, base=base))
    return {"root": str(root), "days": days, "ledger_dir": str(base),
            "ledger_exists": base.exists(), "quote": q, "region": r}


def render(rep: Dict[str, Any]) -> str:
    out = []
    out.append(f"== I-4 灰度周读  root={rep['root']}  近 {rep['days']} 天 ==")
    if not rep["ledger_exists"]:
        out.append(f"  (无账本目录 {rep['ledger_dir']}：该实例尚未装载账本代码或零流量)")
        return "\n".join(out)
    q = rep["quote"]
    out.append("-- 自动链引用回复 (#37) --")
    out.append(f"  决策 {q['decided']}  引用 {q['quoted']}  引用率 {q['quote_rate']:.1%}"
               f"  已应用 {q['applied']}  去引用重发 {q['fallback_plain']}")
    if q["skipped"]:
        out.append("  不引用原因: " + ", ".join(f"{k}={v}" for k, v in q["skipped"].items()))
    if q["low_relevance_hist"]:
        out.append("  low_relevance 最高分直方图(0.05桶): "
                   + ", ".join(f"[{k}]={v}" for k, v in q["low_relevance_hist"].items()))
    if q["suggest_min_relevance"] is not None:
        out.append(f"  ▶ 建议 min_relevance {q['cur_min_relevance']} → {q['suggest_min_relevance']}"
                   f"（按有候选决策 25% 引用率分位；overlay 键 inbox.l2_autosend.quote_reply.min_relevance）")
    elif not q["sample_gate_ok"]:
        out.append(f"  · low_relevance 样本 <20，阈值建议暂不给（当前 {q['cur_min_relevance']}），继续攒")
    else:
        out.append(f"  · 阈值 {q['cur_min_relevance']} 维持（下调不会把引用率推到 25%，或已足够）")
    r = rep["region"]
    out.append("-- 地区语气档 (#40) --")
    if not r["by_region"]:
        out.append("  (非 CN 档零出站观测)")
    for reg, d in r["by_region"].items():
        out.append(f"  {reg}: observed={d['observed']} 禁用词命中={d['banned_hit']}"
                   f" 简体漏出={d['script_hit']} 合计命中率={d['hit_rate']:.1%}  L4={d['l4_verdict']}")
        if d["top_banned"]:
            out.append("     Top 禁用词: " + ", ".join(f"{k}×{v}" for k, v in d["top_banned"].items()))
        if d["top_script"]:
            out.append("     Top 简体字: " + ", ".join(f"{k}×{v}" for k, v in d["top_script"].items()))
    for v in r["verdicts"]:
        out.append("  ▶ " + v)
    if not r["verdicts"] and r["by_region"]:
        out.append("  · 无档位达到 L4 阈值（≥30 条且命中率 ≥10%），维持观测")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:  # Windows 控制台默认 GBK，中文判词会成乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    reps = [report_root(Path(r), args.days) for r in resolve_data_roots(args.data_root)]
    if args.json:
        print(json.dumps(reps, ensure_ascii=False, indent=2))
    else:
        for rep in reps:
            print(render(rep))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
