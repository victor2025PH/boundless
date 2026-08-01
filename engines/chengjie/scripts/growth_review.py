"""获客增长环周审（P13 运营工具）——一屏读完「卡在哪 / 谁在流失 / 下一步」。

数据来自**运行中实例**的只读接口（目标计数驻留进程内存，离线读不到）：
  - ``GET /api/goals/readiness``  就绪度 + 校准建议（P10/P12）
  - ``GET /api/goals/report?days=N``  窗口结果聚合（含 churn_outcomes 矩阵）

用法::

    python -m scripts.growth_review                       # 默认 127.0.0.1:18799
    python -m scripts.growth_review --base http://127.0.0.1:18899
    python -m scripts.growth_review --days 7 --json
    python -m scripts.growth_review --out-jsonl logs/goals/growth_trend.jsonl
    python -m scripts.growth_review --days 7 --samples 20  # P23 周审口径：
        win-rate 表 + 指令拟稿/画像填充漏斗 + 指令遵循人耳抽检样本

令牌取 ``--token`` 或 env ``AITR_WEB_TOKEN``（桌面壳默认 ``admin``）。
实例没起 / 接口 403 时如实报错，不编数字。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_BASE = "http://127.0.0.1:18799"


def _get(url: str, token: str, timeout: float = 8.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, dict) else {"_error": "bad payload"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def collect(
    base: str, token: str, days: int, *, samples: int = 0
) -> Dict[str, Any]:
    b = base.rstrip("/")
    snap = {
        "base": b,
        "days": days,
        "ts": time.time(),
        "readiness": _get(f"{b}/api/goals/readiness", token),
        "report": _get(f"{b}/api/goals/report?days={int(days)}", token),
    }
    if samples > 0:
        snap["samples"] = _get(
            f"{b}/api/goals/instr-samples?limit={int(samples)}", token)
    return snap


def _pct(v: Any) -> str:
    try:
        return f"{float(v):.0%}"
    except (TypeError, ValueError):
        return "-"


def _funnel_rows(outcomes: Dict[str, Any]) -> List[str]:
    rows: List[str] = []
    ranked = sorted(
        ((k, v) for k, v in outcomes.items() if isinstance(v, dict)),
        key=lambda kv: -int(kv[1].get("n") or 0))
    for reason, cell in ranked:
        n = int(cell.get("n") or 0)
        won = int(cell.get("won") or 0)
        rows.append(f"  {reason:<8} n={n:<4} won={won:<4} "
                    f"won_rate={_pct(cell.get('won_rate'))}")
    return rows


def render(snap: Dict[str, Any]) -> str:
    out: List[str] = []
    ready = snap.get("readiness") or {}
    rep = snap.get("report") or {}
    out.append(f"=== 获客增长环周审（{snap.get('base')} / 近 {snap.get('days')} 天）===")

    if ready.get("_error"):
        out.append(f"[readiness] 读取失败：{ready['_error']}")
    else:
        checks = ready.get("checks") or {}
        out.append(f"就绪：{ready.get('status')}"
                   f"（绑定账号 {checks.get('bound_count', 0)} 个）")
        if ready.get("blockers"):
            out.append("卡点：" + "、".join(str(x) for x in ready["blockers"]))
        offers = checks.get("offers") or {}
        if int(offers.get("active") or 0):
            out.append(f"授权活动：{offers.get('active')} 条在效"
                       f"（最近到期 {offers.get('soonest_id')} / "
                       f"{offers.get('soonest_days')} 天）")
        cal = ready.get("calibration") or {}
        if cal:
            out.append(f"校准优先级：{cal.get('priority')}"
                       + (f"（主因 {cal.get('focus')}）" if cal.get("focus") else ""))
            for h in (cal.get("hints") or []):
                out.append(f"  · {h}")

    # P16 漏斗过程读数（进程计数，随 readiness.stats 下发；老实例无此段自动跳过）
    st = ready.get("stats") or {}
    if st:
        beats = st.get("beats") or {}
        inj = st.get("injected") or {}
        out.append(
            f"漏斗（自 {time.strftime('%m-%d %H:%M', time.localtime(st.get('since') or 0))} 起）："
            f"自动建 {st.get('auto_created', 0)} · 拍 {beats.get('planned', 0)}"
            f"（让路 情绪{beats.get('hold_emotion', 0)}/沉默{beats.get('hold_silent', 0)}）"
            f" · 注入 {inj.get('total', 0)}"
            f"（稿{inj.get('draft', 0)}/回{inj.get('reply', 0)}/主动{inj.get('proactive', 0)}）"
            f" · 目录 {st.get('catalog_injected', 0)}"
            f" · 画像槽 {st.get('profile_captured', 0)}")
        guard_bits = []
        if int(st.get("link_stripped") or 0):
            guard_bits.append(f"链接剥离 {st.get('link_stripped')}")
        if int(st.get("offer_claims_stripped") or 0):
            smp = st.get("offer_strip_samples") or {}
            top = sorted(smp.items(), key=lambda kv: -int(kv[1] or 0))[:3]
            frag = " ".join(f"{k}×{v}" for k, v in top)
            by_p = st.get("offer_strip_by_persona") or {}
            who = " ".join(f"{k}×{v}" for k, v in sorted(
                by_p.items(), key=lambda kv: -int(kv[1] or 0))[:3])
            guard_bits.append(
                f"折扣守卫剥离 {st.get('offer_claims_stripped')}"
                + (f"（{frag}）" if frag else "")
                + (f" 人设 {who}" if who else ""))
        if int(st.get("offer_cited") or 0):
            guard_bits.append(f"授权活动引用 {st.get('offer_cited')}")
        if guard_bits:
            out.append("出站纪律：" + " · ".join(guard_bits))

    if rep.get("_error"):
        out.append(f"[report] 读取失败：{rep['_error']}")
    else:
        totals = rep.get("totals") or {}
        out.append(f"终态目标：n={totals.get('n', 0)} "
                   f"done={totals.get('done', 0)} "
                   f"done_rate={_pct(totals.get('done_rate'))} "
                   f"won={totals.get('won', 0)} "
                   f"won_rate={_pct(totals.get('won_rate'))} "
                   f"avg_days_to_done={totals.get('avg_days_to_done')}")

        # P23 win-rate 表（won=order:/manual: 真成交，winback「回话」不算；
        # organic<5 标注样本不足——小样本读数只看不判，防按噪声调模板）
        byt = rep.get("by_template") or {}
        if byt:
            out.append("模板 win-rate（organic 分母排除 cancelled）：")
            ranked_t = sorted(
                ((k, v) for k, v in byt.items() if isinstance(v, dict)),
                key=lambda kv: -int(kv[1].get("n") or 0))
            for tmpl, bt in ranked_t:
                organic = (int(bt.get("done") or 0) + int(bt.get("failed") or 0)
                           + int(bt.get("expired") or 0))
                mark = "" if organic >= 5 else "  ⚠样本不足只读不判"
                out.append(
                    f"  {tmpl:<22} n={int(bt.get('n') or 0):<3} "
                    f"done_rate={_pct(bt.get('done_rate'))} "
                    f"won={int(bt.get('won') or 0)} "
                    f"won_rate={_pct(bt.get('won_rate'))}{mark}")

        # P23 「采纳并拟稿」使用面（DB 口径，重启免疫）+ 画像填充漏斗
        dd = rep.get("drive_draft") or {}
        dd_total = int(dd.get("total") or 0)
        if dd_total:
            by_s = dd.get("by_source") or {}
            frag = " ".join(f"{k}×{v}" for k, v in sorted(
                by_s.items(), key=lambda kv: -int(kv[1] or 0)))
            out.append(f"「采纳并拟稿」指令生成：{dd_total} 次（{frag}）")
        else:
            out.append("「采纳并拟稿」指令生成：0 —— P22 链路无人用，"
                       "查坐席是否知道目标卡/英雄行入口")
        pf = rep.get("profile_fills") or {}
        if int(pf.get("total") or 0):
            by_src = pf.get("by_src") or {}
            by_trk = pf.get("by_track") or {}
            out.append(
                f"画像槽填充：{pf.get('total')}"
                f"（src " + " ".join(f"{k}×{v}" for k, v in sorted(
                    by_src.items(), key=lambda kv: -int(kv[1] or 0)))
                + " · 轨 " + " ".join(f"{k}×{v}" for k, v in sorted(
                    by_trk.items(), key=lambda kv: -int(kv[1] or 0))) + "）")
            # 判词：追问在发、商机轨没进账 = 问了没记/客户没答，查抽取或话术
            if dd_total and not int(by_trk.get("bant") or 0):
                out.append("  ⚠ 有指令拟稿但 bant 轨零填充——「问了没采到」，"
                           "查采集正则/坐席有没有把答案补录进画像")
        elif dd_total:
            out.append("画像槽填充：0 —— 拟稿在用但画像没进账，同上排查")

        outcomes = rep.get("churn_outcomes") or {}
        if outcomes:
            out.append("流失原因 × 转化：")
            out.extend(_funnel_rows(outcomes))
        else:
            out.append("流失原因 × 转化：暂无终态生命周期目标（样本 0）")

    # P23 指令遵循抽检（人耳判「有没有照做」，无客户原文）
    smp = snap.get("samples")
    if isinstance(smp, dict):
        if smp.get("_error"):
            out.append(f"[samples] 读取失败：{smp['_error']}")
        else:
            rows = smp.get("samples") or []
            if rows:
                out.append(f"指令遵循抽检（最近 {len(rows)} 条，"
                           "逐条人耳判：产出是否执行了指令）：")
                for r in rows:
                    d8 = time.strftime("%m-%d %H:%M",
                                       time.localtime(float(r.get("ts") or 0)))
                    out.append(
                        f"  [{d8}]({r.get('source') or '-'}) "
                        f"指令: {str(r.get('instruction') or '')[:60]}")
                    out.append(
                        f"      产出: {str(r.get('reply') or '')[:90]}")
            else:
                out.append("指令遵循抽检：暂无样本（坐席还没用过指令拟稿）")
    return "\n".join(out)


def trend_row(snap: Dict[str, Any]) -> Dict[str, Any]:
    """周批趋势行（供 --out-jsonl；只留可比数字，不含明细）。"""
    ready = snap.get("readiness") or {}
    rep = snap.get("report") or {}
    totals = rep.get("totals") or {}
    cal = ready.get("calibration") or {}
    outcomes = rep.get("churn_outcomes") or {}
    st = ready.get("stats") or {}
    beats = st.get("beats") or {}
    inj = st.get("injected") or {}
    return {
        "ts": snap.get("ts"),
        "date": time.strftime("%Y-%m-%d", time.localtime(snap.get("ts") or 0)),
        "base": snap.get("base"),
        "days": snap.get("days"),
        "status": ready.get("status"),
        "priority": cal.get("priority"),
        "focus": cal.get("focus"),
        "n": int(totals.get("n") or 0),
        "done": int(totals.get("done") or 0),
        "done_rate": totals.get("done_rate"),
        # P23：win-rate / 指令拟稿 / 画像填充（DB 口径，跨重启可比）
        "won": int(totals.get("won") or 0),
        "won_rate": totals.get("won_rate"),
        "drive_draft": int((rep.get("drive_draft") or {}).get("total") or 0),
        "profile_fills": int(
            (rep.get("profile_fills") or {}).get("total") or 0),
        "churn": {k: {"n": int((v or {}).get("n") or 0),
                      "won": int((v or {}).get("won") or 0),
                      "won_rate": (v or {}).get("won_rate")}
                  for k, v in outcomes.items() if isinstance(v, dict)},
        # P16 过程计数（进程口径——stats_since 标注窗口起点，重启后归零，
        # 趋势读数须按 since 对齐再比，不能跨重启直接相减）
        "stats_since": st.get("since"),
        "funnel": {
            "auto_created": int(st.get("auto_created") or 0),
            "beats_planned": int(beats.get("planned") or 0),
            "injected": int(inj.get("total") or 0),
            "catalog": int(st.get("catalog_injected") or 0),
            "profile_captured": int(st.get("profile_captured") or 0),
            "link_stripped": int(st.get("link_stripped") or 0),
            "offer_claims_stripped": int(
                st.get("offer_claims_stripped") or 0),
            "offer_cited": int(st.get("offer_cited") or 0),
        } if st else {},
    }


def append_jsonl(path: str, row: Dict[str, Any]) -> Optional[str]:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="获客增长环周审")
    ap.add_argument("--base", default=DEFAULT_BASE, help="实例地址")
    ap.add_argument("--token", default=os.environ.get("AITR_WEB_TOKEN", "admin"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--out-jsonl", default="", help="追加趋势行到 JSONL")
    ap.add_argument("--samples", type=int, default=0,
                    help="附最近 N 条「指令→产出」抽检样本（0=不取）")
    args = ap.parse_args()

    snap = collect(args.base, args.token, max(1, int(args.days)),
                   samples=max(0, int(args.samples)))
    if args.out_jsonl:
        err = append_jsonl(args.out_jsonl, trend_row(snap))
        if err:
            print(f"[warn] 趋势行写入失败：{err}")
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(render(snap))
    failed = bool((snap.get("readiness") or {}).get("_error")
                  and (snap.get("report") or {}).get("_error"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
