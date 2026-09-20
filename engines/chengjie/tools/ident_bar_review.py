# -*- coding: utf-8 -*-
"""身份条「跳转→换绑」转化裁决读数 CLI（只读；人设绑定 P3/P4 的数据裁决工具）。

背景
----
2026-08-17 收件箱回复区身份条改为可点击（直达右栏人设卡），并埋两个事件：
``ident_bar_jump``（点了身份条）与 ``ident_bar_jump_bind``（跳转后 30s 内*同一会话*
换绑成功，含改回号默认/钉选；整号切换不算）。两周读数决定「要不要做身份条气泡换绑」
——在做第二套换绑 UI 之前，先证明坐席真的在这条路径上换绑。

埋点走 ``_uiBeacon`` → ``ops.ui_event_trend`` 按日落库
（``<数据根>/config/ui_event_trend.db``）。本 CLI 对齐
``tools/inbox_filter_usage_report.py`` 家法：只读（``mode=ro`` URI，绝不创建空库）、
逐数据根、判词纯函数带样本闸门与 ETA。

判词设计（纯函数，喂数即可单测）
----
- **纪元日** ``IDENT_EPOCH_DAY``＝2026-08-17：埋点、右缘示能（ib-go）、30s 转化窗
  同日上线。观测天数按「今天 - 纪元日」算，不按数据首现日——零点击本身是证据，
  不能拿「数据首见日」当起点。
- **链路自证比 iflt 更准**：本工具只有两个分桶，全零未必是链断。改看**整库**窗口内
  是否有任何 UI 事件（``db_total``）——库有流量而 ident_bar_* 为零＝链路健康、
  零就是答案；整库为零＝先查 ``ops.ui_event_trend`` 开关 / beacon 路由。
- **转化率阈值**（跳了多少、绑了多少）：
  · ≥ ``CONV_HIGH``（0.35）→ 值得做气泡换绑（跳转主要是换绑意图，气泡省一次右栏绕行）；
  · < ``CONV_LOW``（0.15）→ 先修右栏人设卡（跳了不绑＝承接掉链，气泡会继承同样的问题）；
  · 之间 → 维持跳转直达，气泡收益边际。
  比值型结论在 jumps < ``MIN_JUMPS``（20）时降置信（1 绑 2 跳这种别当真理）。
- **绝对需求量**同时可见（``per_week``）：周均跳转 < 2 次时即便转化率高，
  气泡也排不上优先级——判词里如实标注。

用法
----
    python tools/ident_bar_review.py [--days 30] [--min-days 14]
        [--min-jumps 20] [--data-root PATH] [--json]

数据根按 ``scripts/_data_root`` 契约解析（CLI → AITR_DATA_ROOT → 自动发现活跃实例 →
引擎根）；多实例逐根输出。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

# 埋点 + 示能 + 30s 转化窗的共同上线日（改任何一样都等于换了度量语义，必须换纪元日）。
IDENT_EPOCH_DAY = "2026-08-17"
ACTION_JUMP = "ident_bar_jump"
ACTION_BIND = "ident_bar_jump_bind"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_JUMPS = 20
CONV_HIGH = 0.35
CONV_LOW = 0.15
LOW_DEMAND_PER_WEEK = 2.0


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_window(db_path: Any, *, days: int = DEFAULT_DAYS,
                now: Optional[float] = None) -> Dict[str, Any]:
    """只读拉取窗口内 ident_bar_* 行 + 整库总量（链路自证用）。

    库不存在 → FileNotFoundError（绝不创建空库）。
    """
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            {"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
            for r in conn.execute(
                "SELECT day, action, n FROM ui_event_trend_daily "
                "WHERE day >= ? AND action LIKE 'ident\\_bar\\_%' ESCAPE '\\' "
                "ORDER BY day",
                (cut,),
            ).fetchall()
        ]
        total = conn.execute(
            "SELECT COALESCE(SUM(n), 0) AS s FROM ui_event_trend_daily WHERE day >= ?",
            (cut,),
        ).fetchone()
        return {"rows": rows, "db_total": int(total["s"] or 0)}
    finally:
        conn.close()


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {jumps, binds, by_day}（纯函数）。"""
    jumps = binds = 0
    by_day: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        a = str(r.get("action") or "")
        d = str(r.get("day") or "")
        n = int(r.get("n") or 0)
        if not a or not d or n <= 0:
            continue
        by_day.setdefault(d, {})
        by_day[d][a] = by_day[d].get(a, 0) + n
        if a == ACTION_JUMP:
            jumps += n
        elif a == ACTION_BIND:
            binds += n
    return {"jumps": jumps, "binds": binds, "by_day": by_day}


def observed_days(now_day: str, epoch_day: str = IDENT_EPOCH_DAY) -> int:
    """自纪元日起的观测天数（含首尾；纪元日当天=1）。解析失败→0（保守）。"""
    try:
        d = (date.fromisoformat(now_day) - date.fromisoformat(epoch_day)).days + 1
        return max(0, d)
    except Exception:
        return 0


def build_verdict(jumps: int, binds: int, obs: int, db_total: int, *,
                  min_days: int = DEFAULT_MIN_DAYS,
                  min_jumps: int = DEFAULT_MIN_JUMPS) -> Dict[str, Any]:
    """「要不要做身份条气泡换绑」单一判词（纯函数）。

    status: ready / insufficient；confidence: high / low（小样本比值降级；
    零点击不降——零就是答案，前提是整库有流量证明链路活着）。
    """
    jumps = max(0, int(jumps))
    binds = max(0, int(binds))
    conv = min(1.0, binds / jumps) if jumps > 0 else 0.0
    per_week = round(jumps / max(1, obs) * 7, 1)
    ev = {"jumps": jumps, "binds": binds, "conv_pct": round(conv * 100, 1),
          "per_week": per_week, "observed_days": obs}
    if int(db_total) == 0:
        return {"status": "insufficient", "evidence": ev,
                "eta_days": max(1, min_days - obs) if obs < min_days else None,
                "note": "窗口内整库 UI 事件为 0——先查链路"
                        "（ops.ui_event_trend 开关 / beacon 路由），别急着下裁决"}
    if obs < min_days:
        return {"status": "insufficient", "evidence": ev,
                "eta_days": max(1, min_days - obs),
                "note": f"观测 {obs}/{min_days} 天（纪元 {IDENT_EPOCH_DAY}）"}
    if jumps == 0:
        return {"status": "ready", "confidence": "high", "evidence": ev,
                "recommendation": "不做气泡：满窗零跳转（示能已于纪元日强化，"
                                  "零点击=需求本身低），身份条维持展示+跳转即可"}
    conf = "high" if jumps >= min_jumps else "low"
    demand_note = (f"；注意周均跳转仅 {per_week} 次，绝对需求低，气泡优先级仍应靠后"
                   if per_week < LOW_DEMAND_PER_WEEK else "")
    if conv >= CONV_HIGH:
        rec = (f"做身份条气泡换绑：转化 {ev['conv_pct']}%（{binds}/{jumps}），"
               f"跳转主要是换绑意图，气泡省一次右栏绕行" + demand_note)
    elif conv < CONV_LOW:
        rec = (f"先修右栏人设卡，不做气泡：转化仅 {ev['conv_pct']}%（{binds}/{jumps}）"
               f"——跳了不绑=卡在承接（搜索/确认/信息不够），气泡会继承同样的问题")
    else:
        rec = (f"维持跳转直达：转化 {ev['conv_pct']}%（{binds}/{jumps}）居中，"
               f"气泡收益边际" + demand_note)
    return {"status": "ready", "confidence": conf, "evidence": ev,
            "recommendation": rec}


def _report_for_root(root: Path, *, days: int, min_days: int, min_jumps: int,
                     now: Optional[float] = None) -> Dict[str, Any]:
    db = Path(root) / "config" / "ui_event_trend.db"
    rep: Dict[str, Any] = {"root": str(root), "db": str(db)}
    try:
        win = read_window(db, days=days, now=now)
    except FileNotFoundError:
        rep["missing"] = True
        rep["note"] = "趋势库不存在（ops.ui_event_trend 未开或尚无数据）——未创建空库"
        return rep
    agg = aggregate(win["rows"])
    rep["window_days"] = days
    rep["jumps"] = agg["jumps"]
    rep["binds"] = agg["binds"]
    rep["by_day"] = agg["by_day"]
    rep["db_total"] = win["db_total"]
    rep["verdict"] = build_verdict(
        agg["jumps"], agg["binds"], observed_days(_utc_day(now)),
        win["db_total"], min_days=min_days, min_jumps=min_jumps)
    return rep


def render_text(rep: Dict[str, Any]) -> List[str]:
    lines = [f"== 数据根 {rep['root']}", f"   库 {rep['db']}"]
    if rep.get("missing"):
        lines.append(f"   [MISS] {rep['note']}")
        return lines
    lines.append(f"   窗口 {rep['window_days']} 天 · 跳转 {rep['jumps']} · 转化 {rep['binds']}"
                 f" · 整库事件 {rep['db_total']}")
    for d in sorted(rep.get("by_day") or {}):
        acts = rep["by_day"][d]
        lines.append(f"     {d}  jump={acts.get(ACTION_JUMP, 0)}"
                     f"  bind={acts.get(ACTION_BIND, 0)}")
    v = rep.get("verdict") or {}
    if v.get("status") == "ready":
        tag = "裁决可下" + ("·低样本" if v.get("confidence") == "low" else "")
        lines.append(f"   [{tag}] 要不要做身份条气泡换绑：{v.get('recommendation', '')}")
    else:
        eta = v.get("eta_days")
        when = ""
        if eta:
            try:
                when = f" → 预计 {(date.today() + timedelta(days=int(eta))).isoformat()}"
            except Exception:
                when = ""
        lines.append(f"   [样本不足] {v.get('note', '')}"
                     + (f"（约还需 {eta} 天{when}）" if eta else ""))
    lines.append(f"           证据 {v.get('evidence')}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="身份条跳转→换绑转化裁决读数（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="读取窗口天数")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS, help="可裁决的最少观测天数")
    ap.add_argument("--min-jumps", type=int, default=DEFAULT_MIN_JUMPS, help="比值型结论的高置信跳转量")
    ap.add_argument("--data-root", default="", help="显式数据根（默认按 _data_root 契约解析）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    roots = resolve_data_roots(args.data_root)
    reports = [_report_for_root(Path(r), days=args.days, min_days=args.min_days,
                                min_jumps=args.min_jumps) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            for ln in render_text(rep):
                print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
