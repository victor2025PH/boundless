# -*- coding: utf-8 -*-
"""风险放行影子台账读数 CLI（#160 I-1-C，2026-09-04）——一个月后定新拦截规则就靠它。

    python tools/autosend_shadow_report.py [--days 30] [--reason stop_contact]
                                           [--data-root D:\\chengjie-instances\\zhiliao\\data]
                                           [--dir <直接指定 jsonl 目录>] [--samples 20] [--json]

只读、走 JSONL、不碰 DB。数据根按 ``scripts/_data_root`` 契约解析（CLI 值 →
``AITR_DATA_ROOT`` → 自动发现本机活跃实例 → 引擎根），每根下扫
``logs/autosend_shadow/shadow_YYYYMMDD.jsonl``；``--dir`` 直指目录时只读它。

输出四段（读法写在每段标题里）：
  1. 按 reason 分桶的频次 —— 哪类「本会被扣」最多；
  2. 按账号 / 平台分布 —— 是不是某个号在集中触发；
  3. 命中词频次 Top —— **直接告诉你哪个正则在误伤**（同一个词高频且抽样复核都是
     正常聊天 → 该词该收窄或删掉）；
  4. 抽样 draft_id 清单（含 conv_key / ts）—— 供人工回溯 protocol_media 里的原始
     会话判「这条到底该不该拦」。

字段契约见 ``src/inbox/autosend_shadow_log.RECORD_FIELDS``。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.inbox.autosend_shadow_log import (  # noqa: E402
    DEFAULT_DIR, iter_records,
)


def _roots(cli_root: str) -> List[Path]:
    try:
        from scripts._data_root import resolve_data_roots
        return resolve_data_roots(cli_root)
    except Exception:
        return [Path(cli_root)] if cli_root else [Path.cwd()]


def collect(days: int, *, dirs: Iterable[Path], reason: str = "",
            now: Optional[float] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for d in dirs:
        d = Path(d)
        if str(d.resolve()) in seen:
            continue
        seen.add(str(d.resolve()))
        for rec in iter_records(days, base=d, now=now):
            if reason and str(rec.get("hold_reason") or "") != reason:
                continue
            rec = dict(rec)
            rec["_src"] = str(d)
            out.append(rec)
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def summarize(all_rows: List[Dict[str, Any]], *, samples: int = 20) -> Dict[str, Any]:
    """纯函数：台账行 → 五段汇总（门禁按它钉口径）。

    行分两类：``kind=hold``（缺 kind 视为 hold，兼容 v1 行）与 ``kind=outcome``；
    outcome 按 draft_id 关联到 hold（一稿一终局）。
    """
    records = [r for r in all_rows if str(r.get("kind") or "hold") != "outcome"]
    outcome_rows = [r for r in all_rows if str(r.get("kind") or "") == "outcome"]
    by_reason: Counter = Counter()
    by_level: Counter = Counter()
    by_stage: Counter = Counter()
    by_account: Counter = Counter()
    by_platform: Counter = Counter()
    by_day: Counter = Counter()
    by_lang: Counter = Counter()
    by_persona: Counter = Counter()
    hits: Counter = Counter()
    hits_by_reason: Dict[str, Counter] = defaultdict(Counter)
    for r in records:
        reason = str(r.get("hold_reason") or "unknown")
        by_reason[reason] += 1
        by_level[str(r.get("would_hold_level") or "?")] += 1
        by_stage[str(r.get("stage") or "?")] += 1
        by_account[f"{r.get('platform') or '?'}:{r.get('account_id') or '?'}"] += 1
        by_platform[str(r.get("platform") or "?")] += 1
        by_day[time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0)))] += 1
        by_lang[str(r.get("lang") or "?")] += 1
        by_persona[str(r.get("persona_id") or "?")] += 1
        for h in (r.get("risk_hits") or []):
            hits[str(h)] += 1
            hits_by_reason[reason][str(h)] += 1
    # 放行后的去向：outcome 行按 draft_id 关联（一稿取最后一条终局）
    outcome_by_draft: Dict[str, Dict[str, Any]] = {}
    for o in sorted(outcome_rows, key=lambda x: float(x.get("ts") or 0)):
        outcome_by_draft[str(o.get("draft_id") or "")] = o
    outcomes: Counter = Counter()
    outcomes_by_reason: Dict[str, Counter] = defaultdict(Counter)
    cancel_reasons: Counter = Counter()
    latencies: List[float] = []
    pending = 0
    for r in records:
        did = str(r.get("draft_id") or "")
        o = outcome_by_draft.get(did)
        reason = str(r.get("hold_reason") or "unknown")
        if o is None:
            pending += 1
            outcomes_by_reason[reason]["pending"] += 1
            continue
        oc = str(o.get("outcome") or "unknown")
        outcomes[oc] += 1
        outcomes_by_reason[reason][oc] += 1
        if oc in ("cancelled", "approved_unsent", "rejected"):
            cancel_reasons[f"{oc}:{o.get('reason') or '?'}"] += 1
        try:
            latencies.append(float(o.get("latency_sec") or 0))
        except Exception:
            pass
    settled = sum(outcomes.values())
    sent_n = outcomes.get("sent", 0)
    # 抽样：每个 reason 均匀取，保证小桶也有样本可复核
    per_reason = max(1, samples // max(1, len(by_reason))) if by_reason else 0
    sample_rows: List[Dict[str, Any]] = []
    taken: Counter = Counter()
    for r in reversed(records):          # 新的优先（原始会话更容易还在）
        reason = str(r.get("hold_reason") or "unknown")
        if taken[reason] >= per_reason:
            continue
        taken[reason] += 1
        sample_rows.append({
            "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r.get("ts") or 0))),
            "reason": reason,
            "level": str(r.get("would_hold_level") or ""),
            "platform": str(r.get("platform") or ""),
            "account_id": str(r.get("account_id") or ""),
            "conv_key": str(r.get("conv_key") or ""),
            "draft_id": str(r.get("draft_id") or ""),
            "hits": list(r.get("risk_hits") or [])[:4],
            "stage": str(r.get("stage") or ""),
        })
        if len(sample_rows) >= samples:
            break
    return {
        "total": len(records),
        "days_with_hits": len(by_day),
        "by_reason": dict(by_reason.most_common()),
        "by_level": dict(by_level.most_common()),
        "by_stage": dict(by_stage.most_common()),
        "by_account": dict(by_account.most_common(20)),
        "by_platform": dict(by_platform.most_common()),
        "by_day": dict(sorted(by_day.items())),
        "top_hits": [{"hit": h, "n": n} for h, n in hits.most_common(25)],
        "top_hits_by_reason": {
            k: [{"hit": h, "n": n} for h, n in v.most_common(8)]
            for k, v in hits_by_reason.items()
        },
        "stop_contact": by_reason.get("stop_contact", 0),
        "self_harm": by_reason.get("self_harm", 0),
        "by_lang": dict(by_lang.most_common()),
        "by_persona": dict(by_persona.most_common(12)),
        # 放行后的去向
        "outcomes": dict(outcomes.most_common()),
        "outcomes_by_reason": {k: dict(v) for k, v in outcomes_by_reason.items()},
        "cancel_reasons": dict(cancel_reasons.most_common(12)),
        "settled": settled,
        "pending_outcome": pending,
        "sent_rate": (round(sent_n / settled, 3) if settled else None),
        "outcome_latency_p50_sec": (sorted(latencies)[len(latencies) // 2] if latencies else None),
        "samples": sample_rows,
    }


def render(summary: Dict[str, Any], *, days: int, dirs: List[Path], reason: str) -> str:
    L: List[str] = []
    L.append(f"== 风险放行影子台账 · 近 {days} 天 · {summary['total']} 行"
             + (f" · reason={reason}" if reason else "") + " ==")
    L.append("目录: " + " | ".join(str(d) for d in dirs))
    if not summary["total"]:
        L.append("（零命中：这段时间没有任何「旧规则本会扣」的稿子。要么流量里没敏感话题，"
                 "要么台账目录不对——确认 --data-root 指向在跑的实例数据根。）")
        return "\n".join(L)
    L.append("")
    L.append("[1] 按 reason（哪类「本会被扣」最多；stop_contact/self_harm 是要人看的两个）")
    for k, v in summary["by_reason"].items():
        L.append(f"    {k:<32} {v:>6}")
    L.append(f"    档位分布 {summary['by_level']} · 阶段 {summary['by_stage']}")
    L.append(f"    语言 {summary['by_lang']} · 人设 {summary['by_persona']}")
    L.append("")
    L.append("[1b] 放行后的去向（放出去的稿到底发出去了没；cancelled/approved_unsent 说明 worker 的"
             "其它守卫替它拦了）")
    if summary["settled"]:
        L.append(f"    已终局 {summary['settled']} · 送达率 {summary['sent_rate']} · "
                 f"待终局 {summary['pending_outcome']} · 终局中位耗时 {summary['outcome_latency_p50_sec']}s")
        for k, v in summary["outcomes"].items():
            L.append(f"    {k:<32} {v:>6}")
        for k, v in summary["outcomes_by_reason"].items():
            L.append(f"      {k:<30} {v}")
        if summary["cancel_reasons"]:
            L.append(f"    未送达明细: {summary['cancel_reasons']}")
    else:
        L.append(f"    （无 outcome 行，待终局 {summary['pending_outcome']}：worker 尚未跑过 "
                 "reconcile，或本批代码刚装载；每个 worker tick 末尾会补齐）")
    L.append("")
    L.append("[2] 按账号（是不是某个号在集中触发） / 按平台")
    for k, v in summary["by_account"].items():
        L.append(f"    {k:<32} {v:>6}")
    L.append(f"    平台: {summary['by_platform']}")
    L.append("")
    L.append("[3] 命中词 Top（同一个词高频、抽样复核又都是正常聊天 → 该正则在误伤，收窄或删）")
    for row in summary["top_hits"]:
        L.append(f"    {row['n']:>5}  {row['hit']}")
    L.append("")
    L.append("[4] 抽样 draft_id（回溯 protocol_media/<platform>/<conv>.md 判「到底该不该拦」）")
    for s in summary["samples"]:
        L.append(f"    {s['ts']}  {s['reason']:<28} {s['level']}  {s['platform']}:{s['account_id']}"
                 f"  conv={s['conv_key']}  draft={s['draft_id']}  hits={s['hits']}")
    L.append("")
    L.append(f"按日: {summary['by_day']}")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--reason", default="", help="只看某个 hold_reason（如 stop_contact）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省按 scripts/_data_root 契约自动发现）")
    ap.add_argument("--dir", default="", help="直接指定 jsonl 目录（绕过数据根解析）")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.dir:
        dirs = [Path(args.dir)]
    else:
        dirs = [Path(r) / DEFAULT_DIR for r in _roots(args.data_root)]
    records = collect(args.days, dirs=dirs, reason=args.reason)
    summary = summarize(records, samples=args.samples)
    if args.json:
        summary["dirs"] = [str(d) for d in dirs]
        summary["days"] = args.days
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary, days=args.days, dirs=dirs, reason=args.reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
