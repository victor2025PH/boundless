# -*- coding: utf-8 -*-
"""引流闭环转化质量台账 + 退化信号（2026-08-13，对标 AvatarHub song_quality_report）。

消费 data/referral_quality.jsonl（由 src/host/referral_probe 每次真实产出追加），
算：
  * 转化漏斗——**双键 (canonical_id, platform) 去重**（INTEGRATION_CONTRACT §7.7-3），
    而非 legacy line_pool.referral_funnel 的 peer_name 单键：同名跨设备不再塌成 1，
    platform 进分母，未来 TikTok/WhatsApp 引流也能各算各的。
  * 回复延迟 p95 / region 分布 / no_match 率 / stale 率。
  * --judge：机器判定退化（转化率地板 + 比自身历史窗回落 + 样本闸），
    doctor/告警只消费本工具的 verdict（判定单一真相在这里）。

诚实边界：dry_run 档产生的探针带 dry_run=True，**不计入**转化分子分母（只在
--include-dry-run 时纳入观察）。sent/reply 用双键关联，reply 命中但其键不在 sent
集合里（比如手工种子）不计入 conversion 分子。

用法：
  python tools/referral_quality_report.py [--days N] [--json] [--judge]
  python tools/referral_quality_report.py --selftest   # 合成红绿双向，零副作用
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 判定阈值（务实档，可后续按真实流量校准；绝对水位当地板、退化比自身历史）
CONVERSION_FLOOR = 0.05      # 近窗转化率 < 5% 且样本够 → 偏低告警
DEGRADE_RATIO = 0.6          # 近窗转化率 < 历史窗 × 0.6 → 退化告警
MIN_SENT_SAMPLES = 8         # 两窗各需 ≥ 该 sent 键数才判（不拿小样本吓人）


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        v = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _key(ev: Dict[str, Any]) -> Tuple[str, str]:
    """双键：canonical_id 优先，缺失回退 peer:<name>；platform 缺失记 unknown。"""
    cid = str(ev.get("canonical_id") or "").strip()
    plat = str(ev.get("platform") or "").strip() or "unknown"
    if cid:
        return (cid, plat)
    peer = str(ev.get("peer_name") or "").strip()
    return (f"peer:{peer}", plat)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _p95(vals: List[float]) -> Optional[float]:
    xs = sorted(v for v in vals if isinstance(v, (int, float)))
    if not xs:
        return None
    return round(xs[int(0.95 * (len(xs) - 1))], 2)


def compute(events: List[Dict[str, Any]],
            include_dry_run: bool = False) -> Dict[str, Any]:
    """从探针事件流算转化质量报告（双键去重）。"""
    def _live(ev):
        return include_dry_run or not ev.get("dry_run")

    sent_keys = set()
    reply_keys = set()
    reply_latencies: List[float] = []
    region_counter: Dict[str, int] = {}
    platform_counter: Dict[str, int] = {}
    reply_rows = 0
    no_match_total = 0
    replied_batch_total = 0
    stale_total = 0
    dead_total = 0

    for ev in events:
        kind = ev.get("kind")
        if kind == "sent" and _live(ev):
            sent_keys.add(_key(ev))
            platform_counter[str(ev.get("platform") or "unknown")] = \
                platform_counter.get(str(ev.get("platform") or "unknown"), 0) + 1
        elif kind == "reply" and _live(ev):
            reply_rows += 1
            reply_keys.add(_key(ev))
            lm = ev.get("latency_min")
            if isinstance(lm, (int, float)):
                reply_latencies.append(float(lm))
            reg = str(ev.get("region") or "unknown")
            region_counter[reg] = region_counter.get(reg, 0) + 1
        elif kind == "reply_batch" and _live(ev):
            no_match_total += int(ev.get("no_match") or 0)
            replied_batch_total += int(ev.get("replied_now") or 0)
        elif kind == "stale_batch" and _live(ev):
            stale_total += int(ev.get("marked_stale") or ev.get("stale") or 0)
            dead_total += int(ev.get("marked_dead") or ev.get("dead") or 0)

    n_sent = len(sent_keys)
    converted_keys = reply_keys & sent_keys       # 回复且确实是我们发过的
    n_converted = len(converted_keys)
    n_replied_unique = len(reply_keys)
    conversion = (n_converted / n_sent) if n_sent else 0.0
    # no_match 率：在批次口径下，未命中 /（命中+未命中）
    denom_match = replied_batch_total + no_match_total
    no_match_rate = (no_match_total / denom_match) if denom_match else 0.0

    return {
        "unique_sent": n_sent,
        "unique_replied": n_replied_unique,
        "converted": n_converted,
        "conversion_rate": round(conversion, 4),
        "reply_events": reply_rows,
        "reply_latency_min_p95": _p95(reply_latencies),
        "no_match_rate": round(no_match_rate, 4),
        "stale_total": stale_total,
        "dead_total": dead_total,
        "by_region": dict(sorted(region_counter.items(),
                                 key=lambda kv: -kv[1])),
        "by_platform": platform_counter,
        "dedup_key": "(canonical_id|peer, platform)",
    }


def _window(events, days, now=None):
    if not days:
        return events
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=days)
    out = []
    for ev in events:
        dt = _parse_ts(str(ev.get("ts") or ""))
        if dt and dt >= cut:
            out.append(ev)
    return out


def judge(events: List[Dict[str, Any]], now=None) -> Dict[str, Any]:
    """机器判定：转化率地板 + 比历史窗回落 + 样本闸。"""
    now = now or datetime.now(timezone.utc)
    recent = _window(events, 7, now)
    prior = [e for e in _window(events, 28, now) if e not in recent]
    r_recent = compute(recent)
    r_prior = compute(prior)
    verdict = "ok"
    reasons: List[str] = []
    if r_recent["unique_sent"] < MIN_SENT_SAMPLES:
        verdict = "insufficient_data"
        reasons.append(
            f"近7天 sent 键数 {r_recent['unique_sent']} < {MIN_SENT_SAMPLES}，样本不足不判")
    else:
        if r_recent["conversion_rate"] < CONVERSION_FLOOR:
            verdict = "warn"
            reasons.append(
                f"近7天转化率 {_pct(r_recent['conversion_rate'])} < 地板 "
                f"{_pct(CONVERSION_FLOOR)}")
        if (r_prior["unique_sent"] >= MIN_SENT_SAMPLES
                and r_prior["conversion_rate"] > 0
                and r_recent["conversion_rate"]
                < r_prior["conversion_rate"] * DEGRADE_RATIO):
            verdict = "warn"
            reasons.append(
                f"近7天转化率 {_pct(r_recent['conversion_rate'])} < 前21天 "
                f"{_pct(r_prior['conversion_rate'])} × {DEGRADE_RATIO}（退化）")
    return {
        "verdict": verdict,
        "reasons": reasons,
        "recent_7d": r_recent,
        "prior_21d": r_prior,
    }


def _selftest() -> int:
    now = datetime.now(timezone.utc)
    def ev(kind, off_days, **f):
        return {"ts": (now - timedelta(days=off_days)).isoformat(),
                "kind": kind, **f}
    # 绿：近窗高转化（10 sent，6 converted）
    green = []
    for i in range(10):
        green.append(ev("sent", 1, canonical_id=f"c{i}", platform="facebook"))
    for i in range(6):
        green.append(ev("reply", 1, canonical_id=f"c{i}", platform="facebook",
                        latency_min=12.0, region="jp"))
    g = judge(green, now)
    assert g["verdict"] == "ok", g
    assert g["recent_7d"]["conversion_rate"] == 0.6, g["recent_7d"]

    # 双键：同名跨设备不塌成 1（canonical 不同 → 2 个键）
    dk = [ev("sent", 1, canonical_id="cA", platform="facebook", peer_name="花子"),
          ev("sent", 1, canonical_id="cB", platform="facebook", peer_name="花子")]
    assert compute(dk)["unique_sent"] == 2, compute(dk)

    # dry_run 不计入
    dr = [ev("sent", 1, canonical_id="cX", platform="facebook", dry_run=True),
          ev("reply", 1, canonical_id="cX", platform="facebook", dry_run=True)]
    assert compute(dr)["unique_sent"] == 0, compute(dr)
    assert compute(dr, include_dry_run=True)["unique_sent"] == 1

    # 红：近窗转化崩（10 sent，0 converted）vs 历史好
    red = []
    for i in range(10):
        red.append(ev("sent", 20, canonical_id=f"p{i}", platform="facebook"))
    for i in range(8):
        red.append(ev("reply", 20, canonical_id=f"p{i}", platform="facebook"))
    for i in range(10):
        red.append(ev("sent", 1, canonical_id=f"q{i}", platform="facebook"))
    # 近窗 0 reply → conversion 0
    r = judge(red, now)
    assert r["verdict"] == "warn", r
    assert any("退化" in x or "地板" in x for x in r["reasons"]), r

    # 样本不足
    few = [ev("sent", 1, canonical_id="z1", platform="facebook")]
    assert judge(few, now)["verdict"] == "insufficient_data"

    print("[referral_quality] selftest OK（双键/干跑排除/红绿/样本闸 全通过）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--include-dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    from src.host import referral_probe
    events = referral_probe.read_all()
    scoped = _window(events, args.days) if args.days else events
    report = compute(scoped, include_dry_run=args.include_dry_run)
    if args.judge:
        report = {"report": report, "judge": judge(events)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("引流闭环转化质量（双键去重）")
    r = report.get("report", report)
    print(f"  唯一发送(sent键)     : {r['unique_sent']}")
    print(f"  唯一回复(reply键)     : {r['unique_replied']}")
    print(f"  转化(回复∩发送)       : {r['converted']}  转化率 {_pct(r['conversion_rate'])}")
    print(f"  回复延迟 p95(min)     : {r['reply_latency_min_p95']}")
    print(f"  未命中率(批次口径)     : {_pct(r['no_match_rate'])}")
    print(f"  stale/dead           : {r['stale_total']}/{r['dead_total']}")
    print(f"  按 region            : {r['by_region']}")
    if args.judge:
        j = report["judge"]
        print(f"  判定                 : {j['verdict']}  {'; '.join(j['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
