"""租户 AI 用量趋势台账 + 「建议升级套餐」判据（纯决策核心）。

背景（2026-08-08 P7）：官网 AI 网关只保留 **3 天** 的按日用量（`pruneOldDays`），
且按日重置——「这家租户最近是不是天天顶着额度跑」这类升级判断没有数据地基。
本模块＝本地台账（采样归并）+ 纯函数判据：

- 采样源：``GET /api/admin/gw-budget`` 的 overrides（所有设过额度的主体自带
  ``used_today``；付费租户在履约时都会设覆写 → 一次调用全量覆盖）；
- 采样点：托管履约守护 tick（它本来就带官网密钥，每 5min 跑）内部 30min 节流；
- 归并语义：同 (日, 主体) 取 **max(used)**——网关按日累加，一天内后采样恒 ≥ 先采样；
  跨时区日界（网关 UTC vs 本机 UTC+8）的 ±1 天归属误差被 max 语义与 7 天窗判据吸收，
  刻意不做时区精确化（趋势判断不需要）；
- 判据：近 N 天内「用量/额度 ≥ hot_ratio」的天数 ≥ min_hot_days → 升级提示
  （只看 ``IID:`` 租户主体；提示经 hints_sent 台账 7 天间隔去重，防天天轰同一家）。

台账落 ``D:\\chengjie-instances\\.ops\\gw_usage_trend.json``（仓库外，随实例数据区）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_LEDGER = Path(r"D:\chengjie-instances\.ops") / "gw_usage_trend.json"
KEEP_DAYS = 60
SAMPLE_GAP_SEC = 30 * 60
HINT_GAP_DAYS = 7.0


def day_key(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def load_ledger(path: Path = DEFAULT_LEDGER) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data.setdefault("days", {})
            data.setdefault("hints_sent", {})
            return data
    except Exception:  # noqa: BLE001 - 首跑/损坏都从零攒（台账可再生）
        pass
    return {"days": {}, "hints_sent": {}}


def save_ledger(ledger: Dict[str, Any], path: Path = DEFAULT_LEDGER) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n",
                 encoding="utf-8")


def sample_due(ledger: Dict[str, Any], now: float,
               gap_sec: float = SAMPLE_GAP_SEC) -> bool:
    """采样节流：距上次采样 ≥ gap 才再打网关（履约 tick 5min 一次，30min 采一轮够画日线）。"""
    try:
        last = float(ledger.get("last_sample_ts") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return (now - last) >= gap_sec


def upsert_sample(ledger: Dict[str, Any], day: str, subject: str,
                  used: int, budget: int) -> bool:
    """归并一条采样（同日同主体取 max used；budget 记最新值）。返回是否有变化。"""
    subject = str(subject or "").strip()
    if not subject:
        return False
    try:
        u = max(0, int(used))
        b = max(0, int(budget))
    except (TypeError, ValueError):
        return False
    row = ledger.setdefault("days", {}).setdefault(day, {}).setdefault(
        subject, {"used": 0, "budget": 0})
    changed = False
    if u > int(row.get("used") or 0):
        row["used"] = u
        changed = True
    if b and b != int(row.get("budget") or 0):
        row["budget"] = b
        changed = True
    return changed


def prune_ledger(ledger: Dict[str, Any], today: str,
                 keep_days: int = KEEP_DAYS) -> int:
    """按日历序保留最近 keep_days 天（day key 是 ISO 串，字典序=时间序）。"""
    days = ledger.get("days", {})
    keys = sorted(k for k in days if k <= today)
    drop = keys[:-keep_days] if len(keys) > keep_days else []
    for k in drop:
        days.pop(k, None)
    return len(drop)


def _recent_days(ledger: Dict[str, Any], today: str, n: int) -> List[str]:
    return sorted(k for k in ledger.get("days", {}) if k <= today)[-n:]


def upgrade_hints(ledger: Dict[str, Any], today: str, *,
                  days: int = 7, hot_ratio: float = 0.8,
                  min_hot_days: int = 3) -> List[Dict[str, Any]]:
    """升级提示判据：近 days 天内 ``used/budget ≥ hot_ratio`` 的天数 ≥ min_hot_days。

    只看 ``IID:`` 租户主体（装机试用机没有升级套餐语义）；budget 缺失/0 的天不参与
    （没有分母就没有比率，宁可少判不误判）。返回按 hot_days 降序。
    """
    window = _recent_days(ledger, today, days)
    stat: Dict[str, Dict[str, Any]] = {}
    for d in window:
        for subject, row in (ledger.get("days", {}).get(d) or {}).items():
            if not subject.startswith("IID:"):
                continue
            b = int(row.get("budget") or 0)
            u = int(row.get("used") or 0)
            if b <= 0:
                continue
            s = stat.setdefault(subject, {"hot_days": 0, "days_seen": 0,
                                          "ratio_sum": 0.0, "budget": b})
            s["days_seen"] += 1
            s["budget"] = b  # 最新额度
            ratio = u / b
            s["ratio_sum"] += ratio
            if ratio >= hot_ratio:
                s["hot_days"] += 1
    out = []
    for subject, s in stat.items():
        if s["hot_days"] >= min_hot_days:
            out.append({
                "subject": subject,
                "hot_days": s["hot_days"],
                "days_seen": s["days_seen"],
                "avg_ratio": round(s["ratio_sum"] / max(1, s["days_seen"]), 3),
                "budget": s["budget"],
            })
    out.sort(key=lambda x: (-x["hot_days"], -x["avg_ratio"]))
    return out


def should_send_hint(ledger: Dict[str, Any], subject: str, now: float,
                     gap_days: float = HINT_GAP_DAYS) -> bool:
    """同一租户的升级提示 ≥ gap_days 才重发（判据每 tick 都算，发送必须去重）。"""
    try:
        last = float((ledger.get("hints_sent") or {}).get(subject) or 0)
    except (TypeError, ValueError):
        last = 0.0
    return (now - last) >= gap_days * 86400


def mark_hint_sent(ledger: Dict[str, Any], subject: str, now: float) -> None:
    ledger.setdefault("hints_sent", {})[subject] = int(now)


def render_report(ledger: Dict[str, Any], today: str, days: int = 14) -> str:
    """CLI 报表：主体 × 近 N 天利用率矩阵 + 升级提示行。"""
    window = _recent_days(ledger, today, days)
    if not window:
        return "（台账为空——采样随履约守护 tick 自动积累，或用 --fetch 立即采一轮）"
    subjects = sorted({s for d in window
                       for s in (ledger.get("days", {}).get(d) or {})})
    lines = ["主体" + " " * 28 + "  ".join(d[5:] for d in window) + "   额度"]
    for s in subjects:
        cells = []
        budget = 0
        for d in window:
            row = (ledger.get("days", {}).get(d) or {}).get(s)
            if not row or not int(row.get("budget") or 0):
                cells.append("  ·  ")
                continue
            budget = int(row["budget"])
            pct = round(100 * int(row.get("used") or 0) / budget)
            cells.append(f"{pct:>4}%")
        lines.append(f"{s:<32}" + " ".join(cells) + f"  {budget:,}")
    hints = upgrade_hints(ledger, today)
    if hints:
        lines.append("")
        for h in hints:
            lines.append(
                f"⤴ 建议升级：{h['subject']}  近窗 {h['hot_days']}/{h['days_seen']} 天"
                f" ≥80% 额度（均值 {h['avg_ratio']:.0%}，现额度 {h['budget']:,}/日）")
    return "\n".join(lines)
