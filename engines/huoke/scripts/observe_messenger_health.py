# -*- coding: utf-8 -*-
"""P13 Messenger 运维观察看板 (B 独占)。

周期采样 DB + 各模块状态, 输出 markdown 报告到 logs/。

和漏斗/扩展漏斗的"即时快照"区别:
  * chat_funnel.get_funnel_metrics_extended = **实时查询** 接口
  * **本工具** = **周期观察** + **差异对比** + 文件归档

运维场景:
  1. 真机生产运行 1-2 周后, 定期 (cron / 手动) 跑本工具
  2. 对比最近两次报告 (--diff) 看趋势 (reply_rate 上升 / gate 分布漂移)
  3. 发现异常 (intent_health=degraded / errors 突增) 时给告警

用法:
    # 一次性报告
    python scripts/observe_messenger_health.py

    # 指定 device + 时间窗口
    python scripts/observe_messenger_health.py --device <did> --since-hours 24

    # 和上一个报告对比差异
    python scripts/observe_messenger_health.py --diff

    # 持续观察 (每 30 分钟)
    python scripts/observe_messenger_health.py --watch --interval 1800

    # 可选 preset_key 过滤 (生产多 preset 时切片)
    python scripts/observe_messenger_health.py --preset-key jp_growth
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 2026-04-24 (Phase 10.3): Windows 默认 cp936/gbk 撞 unicode emoji (🔴/🟢) 崩溃,
# 强制 stdout/stderr UTF-8 (Python 3.7+ reconfigure; 项目要求 >=3.9).
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("observe")


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HealthSnapshot:
    """某时刻的 Messenger 健康状态快照。"""
    timestamp_iso: str = ""
    device_id: str = ""
    preset_key: str = ""
    since_hours: int = 24
    # 基础漏斗 (get_funnel_metrics 返回的 stage_*)
    funnel_stages: Dict[str, Any] = field(default_factory=dict)
    # 扩展 reply_rate_by_intent
    reply_rate_by_intent: Dict[str, Any] = field(default_factory=dict)
    # 陌生人转化率
    stranger_conversion: Dict[str, Any] = field(default_factory=dict)
    # gate 分布 (wa_referral_sent event 切片)
    gate_block_distribution: Dict[str, Any] = field(default_factory=dict)
    # intent 健康报告
    intent_health: Dict[str, Any] = field(default_factory=dict)
    # contact_events 汇总
    contact_events_total: Dict[str, int] = field(default_factory=dict)
    # 错误
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


# ─────────────────────────────────────────────────────────────────────────────
# 采样
# ─────────────────────────────────────────────────────────────────────────────

def take_snapshot(device_id: Optional[str] = None,
                  since_hours: int = 24,
                  preset_key: Optional[str] = None) -> HealthSnapshot:
    """采样当前 Messenger 健康状态。"""
    now = _dt.datetime.utcnow()
    since_iso = (now - _dt.timedelta(hours=since_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    snap = HealthSnapshot(
        timestamp_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        device_id=device_id or "",
        preset_key=preset_key or "",
        since_hours=since_hours,
    )

    # 1. 扩展漏斗 (一站式)
    try:
        from src.analytics.chat_funnel import get_funnel_metrics_extended
        m = get_funnel_metrics_extended(
            device_id=device_id, since_iso=since_iso,
            preset_key=preset_key,
            include_intent_coverage=True,
            include_greeting_template=False,  # A 的 API 未必可用
        )
        # 拆出 stage_* 到 funnel_stages
        snap.funnel_stages = {
            k: v for k, v in m.items()
            if str(k).startswith("stage_")
            and isinstance(v, (int, float))
        }
        snap.reply_rate_by_intent = m.get("reply_rate_by_intent", {})
        snap.stranger_conversion = m.get("stranger_conversion_rate", {})
        snap.gate_block_distribution = m.get("gate_block_distribution", {})
        snap.intent_health = m.get("intent_health", {})
    except Exception as e:
        snap.errors.append(f"extended_funnel 失败: {str(e)[:80]}")

    # 2. contact_events 汇总 (Phase 5 未 merge 时空)
    try:
        from src.host.database import _connect
        where = ""
        params: list = []
        if device_id:
            where = " AND device_id = ?"
            params.append(device_id)
        with _connect() as conn:
            # 试探表是否存在
            try:
                rows = conn.execute(
                    "SELECT event_type, COUNT(*) FROM fb_contact_events"
                    " WHERE detected_at >= ?" + where
                    + " GROUP BY event_type",
                    [since_iso] + params,
                ).fetchall()
                snap.contact_events_total = {r[0]: int(r[1]) for r in rows}
            except Exception:
                # 表不存在 = Phase 5 未 merge, 不报 error 只留空 dict
                pass
    except Exception as e:
        snap.errors.append(f"contact_events 查询失败: {str(e)[:80]}")

    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Markdown render
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown(snap: HealthSnapshot,
                    diff_with: Optional[HealthSnapshot] = None) -> str:
    """生成 markdown 报告字符串。"""
    lines: List[str] = []
    lines.append(f"# Messenger Health Snapshot")
    lines.append("")
    lines.append(f"- **timestamp**: `{snap.timestamp_iso}` UTC")
    lines.append(f"- **device_id**: `{snap.device_id or '(全部)'}`")
    lines.append(f"- **preset_key**: `{snap.preset_key or '(全部)'}`")
    lines.append(f"- **since**: 过去 {snap.since_hours} 小时")
    lines.append("")

    if snap.errors:
        lines.append("## ⚠ 采样错误")
        for e in snap.errors:
            lines.append(f"- {e}")
        lines.append("")

    # 基础漏斗
    lines.append("## 基础漏斗 (stage_*)")
    if snap.funnel_stages:
        lines.append("| stage | count | Δ |")
        lines.append("|---|---|---|")
        for k, v in sorted(snap.funnel_stages.items()):
            delta = ""
            if diff_with and k in diff_with.funnel_stages:
                d = v - diff_with.funnel_stages[k]
                if d > 0:
                    delta = f"+{d} ↑"
                elif d < 0:
                    delta = f"{d} ↓"
                else:
                    delta = "0"
            lines.append(f"| {k} | {v} | {delta} |")
    else:
        lines.append("*(空数据)*")
    lines.append("")

    # reply_rate_by_intent
    lines.append("## reply_rate_by_intent")
    by_intent = snap.reply_rate_by_intent.get("by_intent", {}) or {}
    lines.append(f"- 总 incomings: {snap.reply_rate_by_intent.get('total_incomings', 0)}")
    lines.append(f"- classifiable: {snap.reply_rate_by_intent.get('classifiable', 0)}")
    if by_intent:
        lines.append("")
        lines.append("| intent | incomings | replied | reply_rate |")
        lines.append("|---|---|---|---|")
        for intent, stat in sorted(by_intent.items()):
            rate = stat.get("reply_rate", 0.0)
            lines.append(
                f"| {intent} | {stat.get('incomings', 0)} | "
                f"{stat.get('replied', 0)} | {rate:.0%} |"
            )
    lines.append("")

    # stranger
    lines.append("## 陌生人转化")
    s = snap.stranger_conversion
    lines.append(f"- stranger_peers: {s.get('stranger_peers', 0)}")
    lines.append(f"- reply_rate: {s.get('reply_rate', 0.0):.0%}")
    lines.append(f"- referral_rate: {s.get('referral_rate', 0.0):.0%}")
    lines.append("")

    # gate 分布
    lines.append("## Gate 分布 (wa_referral_sent 事件)")
    gd = snap.gate_block_distribution
    total = gd.get("total_referrals", 0)
    lines.append(f"- 总 wa_referral: {total}")
    if gd.get("by_channel"):
        lines.append("- by_channel:")
        for ch, n in sorted(gd["by_channel"].items(), key=lambda x: -x[1]):
            lines.append(f"  - `{ch}`: {n}")
    if gd.get("by_intent_at_referral"):
        lines.append("- by_intent_at_referral:")
        for it, n in sorted(gd["by_intent_at_referral"].items(), key=lambda x: -x[1]):
            lines.append(f"  - `{it}`: {n}")
    lines.append("")

    # intent health
    lines.append("## Intent Health (P9)")
    ih = snap.intent_health
    health = ih.get("health", "unknown")
    emoji = {"healthy": "✅", "needs_rules": "⚠️",
             "degraded": "🔴", "no_data": "·"}.get(health, "?")
    lines.append(f"- status: {emoji} `{health}`")
    lines.append(f"- rule_coverage: {ih.get('rule_coverage', 0.0):.0%}")
    lines.append(f"- llm_coverage: {ih.get('llm_coverage', 0.0):.0%}")
    lines.append(f"- fallback_coverage: {ih.get('fallback_coverage', 0.0):.0%}")
    if ih.get("recommendation"):
        lines.append(f"- **建议**: {ih['recommendation']}")
    lines.append("")

    # contact_events
    lines.append("## Contact Events (fb_contact_events)")
    if snap.contact_events_total:
        lines.append("| event_type | count | Δ |")
        lines.append("|---|---|---|")
        for k, v in sorted(snap.contact_events_total.items(), key=lambda x: -x[1]):
            delta = ""
            if diff_with and k in diff_with.contact_events_total:
                d = v - diff_with.contact_events_total[k]
                if d > 0:
                    delta = f"+{d} ↑"
                elif d < 0:
                    delta = f"{d} ↓"
            lines.append(f"| {k} | {v} | {delta} |")
    else:
        lines.append("*(空 — Phase 5 未 merge / 无事件)*")
    lines.append("")

    # 对比摘要
    if diff_with:
        lines.append(f"## 对比上次报告 ({diff_with.timestamp_iso})")
        # 只列关键 delta
        key_stages = ["stage_inbox_incoming", "stage_outgoing_replies",
                       "stage_wa_referrals"]
        for k in key_stages:
            if k in snap.funnel_stages and k in diff_with.funnel_stages:
                d = snap.funnel_stages[k] - diff_with.funnel_stages[k]
                arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
                lines.append(f"- {k}: {diff_with.funnel_stages[k]} → {snap.funnel_stages[k]} ({arrow} {d:+d})")
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 报告归档 + 读取
# ─────────────────────────────────────────────────────────────────────────────

def archive_report(snap: HealthSnapshot, markdown: str) -> Path:
    """写报告到 logs/observe_<timestamp>.md + 对应 .json。"""
    ts = snap.timestamp_iso.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    md_path = LOGS_DIR / f"observe_{ts}.md"
    json_path = LOGS_DIR / f"observe_{ts}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(snap.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return md_path


def load_last_snapshot() -> Optional[HealthSnapshot]:
    """找 logs/ 下最新的 observe_*.json 加载回 HealthSnapshot。"""
    jsons = sorted(LOGS_DIR.glob("observe_*.json"), reverse=True)
    if not jsons:
        return None
    try:
        d = json.loads(jsons[0].read_text(encoding="utf-8"))
        return HealthSnapshot(**d)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Messenger 运维观察看板 — 周期采样 + markdown 报告归档")
    parser.add_argument("--device", type=str, default=None,
                        help="指定 device_id (默认全部)")
    parser.add_argument("--since-hours", type=int, default=24,
                        help="时间窗口 (默认 24h)")
    parser.add_argument("--preset-key", type=str, default=None,
                        help="preset_key 过滤")
    parser.add_argument("--diff", action="store_true",
                        help="对比 logs/ 下最近的报告")
    parser.add_argument("--watch", action="store_true",
                        help="持续观察模式 (循环)")
    parser.add_argument("--interval", type=int, default=1800,
                        help="--watch 下轮询间隔秒 (默认 1800=30min)")
    parser.add_argument("--no-archive", action="store_true",
                        help="不写 logs/ (stdout only)")
    parser.add_argument("--no-color", action="store_true",
                        help="markdown 不用 ANSI 色")
    args = parser.parse_args()

    def one_round():
        prev = load_last_snapshot() if args.diff else None
        snap = take_snapshot(
            device_id=args.device,
            since_hours=args.since_hours,
            preset_key=args.preset_key,
        )
        md = render_markdown(snap, diff_with=prev)
        print(md)
        if not args.no_archive:
            path = archive_report(snap, md)
            print(f"\n---\n*报告已归档: {path}*", file=sys.stderr)
        return snap

    if args.watch:
        print(f"[watch] 每 {args.interval}s 跑一次 observe, Ctrl+C 停", file=sys.stderr)
        while True:
            try:
                one_round()
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n[watch] 停止", file=sys.stderr)
                break
    else:
        one_round()
    return 0


if __name__ == "__main__":
    sys.exit(main())
