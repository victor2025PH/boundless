#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周报 — 读 daily_snapshot.csv 7 行 → 生成 markdown 周报.

为什么:
- daily_snapshot 落底数据, 但 1 周后想看趋势不能让 victor 自己 Excel 拼
- 自动出 markdown 周报, 含趋势图 (ASCII) + 启发式建议

用法:
    python scripts/weekly_report.py                    # 默认 reports/daily_snapshot.csv
    python scripts/weekly_report.py --csv path/to.csv
    python scripts/weekly_report.py --output reports/weekly_YYYYMMDD.md

输出: markdown, 含
  - 7 天对比表 (今天 vs 一周前)
  - 漏斗 ASCII 趋势 sparkline
  - SLA / refer 率 / push 失败率 趋势
  - 启发式调参建议
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ── 数据加载 ─────────────────────────────────────────────────────────
def load_snapshot(path: Path, days: int = 7) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"⚠️  {path} 不存在", file=sys.stderr)
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    # 取最近 N 行
    return rows[-days:]


def _to_int(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


# ── ASCII sparkline ──────────────────────────────────────────────────
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: List[float]) -> str:
    """8 档 ASCII 趋势."""
    if not values:
        return ""
    vmax = max(values)
    if vmax == 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(
        SPARK_CHARS[min(7, int(v / vmax * 7))] for v in values
    )


# ── 对比 + 启发式 ────────────────────────────────────────────────────
def diff_arrow(now: float, prev: float) -> str:
    if prev == 0 and now == 0:
        return "—"
    if prev == 0:
        return "📈 (新)"
    delta = (now - prev) / max(abs(prev), 1) * 100
    if delta > 5:
        return f"📈 +{delta:.0f}%"
    if delta < -5:
        return f"📉 {delta:.0f}%"
    return f"➖ {delta:+.0f}%"


def heuristic_advice(rows: List[Dict[str, Any]]) -> List[str]:
    """启发式调参建议."""
    if len(rows) < 3:
        return ["数据不足 3 天, 暂不出建议"]
    advice = []
    last = rows[-1]
    avg = lambda key: sum(_to_float(r.get(key)) for r in rows) / len(rows)

    refer_rate = _to_float(last.get("rd_refer_rate"))
    avg_refer = avg("rd_refer_rate")
    if refer_rate > 0.30:
        advice.append(
            "🔴 **refer 率偏高 ({0:.0%})** — 建议调高 `early_refer_readiness` "
            "(0.8 → 0.85) 或 `min_emotion_score` (0.5 → 0.6) 防止过激引流"
            .format(refer_rate))
    elif refer_rate < 0.05 and refer_rate > 0:
        advice.append(
            "🟡 **refer 率偏低 ({0:.0%})** — 建议调低 `delay_refer_readiness` "
            "(0.3 → 0.25) 或 `min_turns` (7 → 5)"
            .format(refer_rate))

    breach = _to_int(last.get("handoff_breach_30min"))
    if breach >= 5:
        advice.append(
            f"🔴 **接管超时积压 {breach} 个** — 客服响应不足, "
            "考虑加人或调 SLA `medium=30min → 45min`")
    elif breach >= 2:
        advice.append(f"🟡 接管有 {breach} 超时, 注意盯盘")

    fail = _to_int(last.get("push_failure"))
    total = _to_int(last.get("push_total"))
    if total >= 50 and fail / total > 0.30:
        advice.append(
            f"🔴 **push 失败率 {fail/total*100:.0f}%** — "
            "查 coordinator 网络 / PG 健康")

    fr_avg = avg("ev_friend_req")
    fr_today = _to_float(last.get("ev_friend_req"))
    if fr_avg > 10 and fr_today < fr_avg * 0.5:
        advice.append(
            f"🟡 今日加好友 {fr_today:.0f} 远低于均值 {fr_avg:.0f} — "
            "检查设备在线 / 拉黑率")

    conv_rate = _to_float(last.get("sla_conversion_rate"))
    if _to_int(last.get("sla_total_handled")) >= 5 and conv_rate < 0.20:
        advice.append(
            f"🟡 转化率 {conv_rate:.0%} 偏低 — 人工 review handoff 接管后的对话")

    avg_accept = _to_float(last.get("sla_avg_accept_min"))
    if avg_accept > 30:
        advice.append(
            f"🔴 客服平均响应 {avg_accept:.1f} min — 加客服 / 调班次")

    if not advice:
        advice.append("✅ 各项健康, 维持当前配置")
    return advice


# ── markdown 渲染 ────────────────────────────────────────────────────
def render_report(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "# 周报\n\n暂无快照数据.\n"

    last = rows[-1]
    first = rows[0]
    n_days = len(rows)

    lines = []
    lines.append(f"# 周报 — {last.get('date')}")
    lines.append("")
    lines.append(f"覆盖天数: **{n_days}** ({first.get('date')} → {last.get('date')})")
    lines.append("")

    # ── 漏斗趋势 ─────────────────────────────────────────────────
    lines.append("## 1. 漏斗趋势 (近 " + str(n_days) + " 天)")
    lines.append("")
    lines.append("| 指标 | 趋势 | 今 | 一周前 | 变化 |")
    lines.append("|---|---|---:|---:|---|")
    for label, key in [
        ("加好友发出", "ev_friend_req"),
        ("打招呼发出", "ev_greeting"),
        ("客户回复", "ev_msg_recv"),
        ("引流发出", "ev_wa_referral"),
        ("成交", "ev_converted"),
        ("流失", "ev_lost"),
    ]:
        vals = [_to_float(r.get(key)) for r in rows]
        sp = sparkline(vals)
        now = _to_int(last.get(key))
        prev = _to_int(first.get(key))
        lines.append(f"| {label} | `{sp}` | {now} | {prev} | "
                     f"{diff_arrow(now, prev)} |")
    lines.append("")

    # ── 客户分桶 ─────────────────────────────────────────────────
    lines.append("## 2. 客户分桶 (今日 vs 一周前)")
    lines.append("")
    lines.append("| 状态 | 趋势 | 今 | 一周前 |")
    lines.append("|---|---|---:|---:|")
    for label, key in [
        ("in_funnel", "cust_in_funnel"),
        ("in_messenger", "cust_in_messenger"),
        ("in_line", "cust_in_line"),
        ("accepted", "cust_accepted"),
        ("converted", "cust_converted"),
        ("lost", "cust_lost"),
    ]:
        vals = [_to_float(r.get(key)) for r in rows]
        sp = sparkline(vals)
        now = _to_int(last.get(key))
        prev = _to_int(first.get(key))
        lines.append(f"| {label} | `{sp}` | {now} | {prev} |")
    lines.append("")

    # ── 决策 + SLA ───────────────────────────────────────────────
    lines.append("## 3. 决策 + SLA")
    lines.append("")
    lines.append("| 指标 | 趋势 | 今 |")
    lines.append("|---|---|---:|")
    for label, key, fmt in [
        ("决策总数", "rd_total", "{:.0f}"),
        ("refer 率", "rd_refer_rate", "{:.1%}"),
        ("hard_block", "rd_hard_block", "{:.0f}"),
        ("hard_allow", "rd_hard_allow", "{:.0f}"),
        ("接管超时(30min)", "handoff_breach_30min", "{:.0f}"),
        ("SLA 转化率", "sla_conversion_rate", "{:.1%}"),
        ("平均响应分钟", "sla_avg_accept_min", "{:.1f}"),
        ("push 失败", "push_failure", "{:.0f}"),
    ]:
        vals = [_to_float(r.get(key)) for r in rows]
        sp = sparkline(vals)
        now = _to_float(last.get(key))
        lines.append(f"| {label} | `{sp}` | {fmt.format(now)} |")
    lines.append("")

    # ── 建议 ─────────────────────────────────────────────────────
    lines.append("## 4. 启发式建议")
    lines.append("")
    for ad in heuristic_advice(rows):
        lines.append(f"- {ad}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 周报仅基于 daily_snapshot.csv 启发式分析. ")
    lines.append("> 真改动前请人工 review 50 条对话 (`scripts/chat_review.py`).")
    lines.append("> 配阈值见 `config/referral_strategies.yaml` (hot reload, 不重启).")

    return "\n".join(lines) + "\n"


# ── main ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="reports/daily_snapshot.csv")
    p.add_argument("--days", type=int, default=7,
                   help="覆盖最近几天 (默认 7)")
    p.add_argument("--output", default="",
                   help="输出 markdown 路径; 默认 reports/weekly_YYYYMMDD.md")
    p.add_argument("--print", action="store_true",
                   help="只打印不写文件")
    args = p.parse_args()

    rows = load_snapshot(Path(args.csv), days=args.days)
    if not rows:
        print("没数据可生成周报")
        return 1

    md = render_report(rows)

    if args.print:
        print(md)
        return 0

    output = (Path(args.output) if args.output
              else Path("reports") /
              f"weekly_{datetime.date.today().strftime('%Y%m%d')}.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(md, encoding="utf-8")
    print(f"✅ 周报: {output}")
    print()
    # 终端预览
    print(md[:1500] + ("...(更多见文件)" if len(md) > 1500 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
