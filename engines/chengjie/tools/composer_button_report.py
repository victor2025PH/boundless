# -*- coding: utf-8 -*-
"""composer 工具行按钮用量裁决 CLI（只读；B3「按钮瘦身」的数据决策工具）。

背景
----
坐席 composer 工具行常驻 7+ 个入口（AI回复/语音▾/知识/媒体/翻译▾/表情/快捷键速查），
到底哪些配得上黄金位、哪些该收进「+」更多菜单，此前全凭感觉。2026-08-19 起全部
顶行入口带 ``cbtn_*`` 埋点（``_uiBeacon`` → ``ops.ui_event_trend`` 日聚合落
``<数据根>/config/ui_event_trend.db``）；本 CLI 把「样本到没到、证据指向哪」读出来
——完整对齐 ``tools/inbox_filter_usage_report.py`` 家法（纪元日常量 / 零点击是合法
证据 / 埋点链自证 / 只读 mode=ro）。

分桶
----
- ``cbtn_send``＝发送动作分母（按钮+Enter，空回车/只读拦截不计；含媒体发送与编辑保存）；
- ``cbtn_ai / cbtn_voice / cbtn_kb / cbtn_media / cbtn_xlate / cbtn_xlate_chip /
  cbtn_emoji / cbtn_kbdhelp``＝各入口点击。

判词
----
- ``keep``：份额 ≥ keep_share（默认 5% of send 动作）或日均 ≥ keep_daily（默认 3）；
- ``fold_candidate``：满观察期且样本充分但低于双线——收进「+」菜单的候选；
- ``insufficient``：观察天数不足 / 全链零样本（先查埋点链，别当「没人用」）。
**只读不改行为**：真正收按钮是看完报告后的显式施工决定。

用法
----
    python tools/composer_button_report.py [--days 30] [--min-days 7]
        [--keep-share 0.05] [--keep-daily 3] [--data-root PATH] [--json]
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

#: 全部 cbtn_* 埋点同日上线（B3 前置批次）。观测天数按「今天 - 纪元日」算，
#: 不按数据首现日——零点击本身是证据（iflt 主线同一教训）。
CBTN_EPOCH_DAY = "2026-08-19"

#: 顶行入口 → 人话名（xlate_chip 是翻译弹层的第二入口，单列观测）
BUTTONS = {
    "cbtn_ai": "AI回复",
    "cbtn_voice": "语音▾",
    "cbtn_kb": "知识",
    "cbtn_media": "媒体",
    "cbtn_xlate": "翻译▾",
    "cbtn_xlate_chip": "翻译状态chip",
    "cbtn_emoji": "表情",
    "cbtn_kbdhelp": "快捷键速查",
}
DENOM = "cbtn_send"


def _utc_day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = 30,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 cbtn_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'cbtn\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def observed_days(now: Optional[float] = None) -> int:
    """纪元日口径的观测天数（今天含当天；纪元日在未来＝0）。"""
    today = date.fromisoformat(_utc_day(now))
    epoch = date.fromisoformat(CBTN_EPOCH_DAY)
    return max(0, (today - epoch).days + 1)


def analyze_rows(rows: List[Dict[str, Any]], *, now: Optional[float] = None,
                 min_days: int = 7, keep_share: float = 0.05,
                 keep_daily: float = 3.0) -> Dict[str, Any]:
    """纯函数：日聚合行 → 各按钮判词（可单测）。"""
    totals: Dict[str, int] = {}
    for r in rows:
        a = str(r.get("action") or "")
        totals[a] = totals.get(a, 0) + int(r.get("n") or 0)
    obs = observed_days(now)
    send_total = totals.get(DENOM, 0)
    chain_alive = any(totals.get(k, 0) > 0 for k in list(BUTTONS) + [DENOM])
    out: Dict[str, Any] = {
        "observed_days": obs,
        "epoch_day": CBTN_EPOCH_DAY,
        "send_total": send_total,
        "chain_alive": chain_alive,
        "buttons": {},
    }
    for key, label in BUTTONS.items():
        n = totals.get(key, 0)
        daily = (n / obs) if obs > 0 else 0.0
        share = (n / send_total) if send_total > 0 else 0.0
        if obs < min_days or not chain_alive:
            verdict = "insufficient"
            why = ("观察期不足（%d/%d 天）" % (obs, min_days)) if obs < min_days \
                else "cbtn_* 全链零样本——先查埋点链（上线当天就该有 send 样本）"
        elif share >= keep_share or daily >= keep_daily:
            verdict = "keep"
            why = "份额 %.1f%% / 日均 %.1f 次，配得上黄金位" % (share * 100, daily)
        else:
            verdict = "fold_candidate"
            why = "满观察期仍低频（份额 %.1f%% / 日均 %.1f）——收进「+」菜单候选" % (
                share * 100, daily)
        out["buttons"][key] = {
            "label": label, "total": n, "daily_avg": round(daily, 2),
            "share_of_send": round(share, 4), "verdict": verdict, "why": why,
        }
    return out


def _scan_root(root: Path, *, days: int, min_days: int,
               keep_share: float, keep_daily: float) -> Dict[str, Any]:
    db = root / "config" / "ui_event_trend.db"
    try:
        rows = read_rows(db, days=days)
    except FileNotFoundError:
        return {"root": str(root),
                "error": "ui_event_trend.db 不存在（ops.ui_event_trend 未开或尚无数据）"}
    rep = analyze_rows(rows, min_days=min_days,
                       keep_share=keep_share, keep_daily=keep_daily)
    rep["root"] = str(root)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=7)
    ap.add_argument("--keep-share", type=float, default=0.05)
    ap.add_argument("--keep-daily", type=float, default=3.0)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    roots = [Path(args.data_root)] if args.data_root else \
        [Path(r) for r in resolve_data_roots()]
    reports = [_scan_root(r, days=args.days, min_days=args.min_days,
                          keep_share=args.keep_share, keep_daily=args.keep_daily)
               for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"== {rep.get('root')}")
        if rep.get("error"):
            print("   ", rep["error"])
            continue
        print("   纪元 %s 起观测 %d 天；发送动作分母 %d 次%s" % (
            rep["epoch_day"], rep["observed_days"], rep["send_total"],
            "" if rep["chain_alive"] else "（⚠ 全链零样本）"))
        for key, b in rep["buttons"].items():
            print("   %-16s %-8s 总 %5d  日均 %6.1f  份额 %5.1f%%  [%s] %s" % (
                key, b["label"], b["total"], b["daily_avg"],
                b["share_of_send"] * 100, b["verdict"], b["why"]))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
