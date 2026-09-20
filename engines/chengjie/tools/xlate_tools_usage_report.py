# -*- coding: utf-8 -*-
"""翻译工具四件套（右键/对照矩阵/灯箱贴回/文档音视频口）用量裁决 CLI（只读）。

背景
----
2026-08-18 翻译 P0-P4 四批收敛后，遗留四个**读数决策**（8/31 前后裁决）：
1. 右键翻译组哪些入口活着（text/img/voice/video/cmp 分桶）——死入口清退；
2. 对照矩阵「加开语言」用不用（cmp_lang/cmp 比）——决定要不要记语言偏好/扩语言集；
3. 灯箱贴回值不值（patch/run 比 + patch_view 回看率）——决定要不要升级自动预生成；
4. 文档口音视频用量（docxl_audio/video）——GPU 容量规划 + 长音频分片管线立不立项，
   srt 下载率决定双语字幕是否默认勾选。

埋点全走 ``_uiBeacon`` 的 ``xlctx_/lbxl_/docxl_`` 前缀 → ``ops.ui_event_trend``
按日落库。**只读、不改任何行为**（对齐 xlate_usage_report 家法：mode=ro、
逐数据根、判词带样本闸门与 ETA、纪元前行隔离）。

用法
----
    python tools/xlate_tools_usage_report.py [--days 30] [--min-days 14]
        [--min-total 20] [--data-root PATH] [--json]
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

# 埋点纪元日：代码 2026-08-18 分四批上线，当天含本线开发/门禁真点击噪声
# （门禁自 P4 起 context 级吞 beacon，但当天早批的手工验证已入库）→ 纪元定次日，
# read 侧把早于纪元的行整体隔离，裁决样本从第一天起就是干净坐席行为。
EPOCH_DAY = "2026-08-19"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20

_PREFIXES = ("xlctx_", "lbxl_", "docxl_")
BUCKETS: Dict[str, List[str]] = {
    "ctx_entry": ["xlctx_text", "xlctx_img", "xlctx_voice", "xlctx_video", "xlctx_cmp"],
    "compare": ["xlctx_cmp", "xlctx_cmp_lang", "xlctx_cmp_copy"],
    "lightbox": ["lbxl_run", "lbxl_patch", "lbxl_patch_view"],
    "doc_av": ["docxl_audio", "docxl_video", "docxl_subtitle",
               "docxl_audio_done", "docxl_srt_dl"],
}


def _utc_day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天三前缀行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        out: List[Dict[str, Any]] = []
        for pref in _PREFIXES:
            rows = conn.execute(
                "SELECT day, action, n FROM ui_event_trend_daily "
                "WHERE day >= ? AND action LIKE ? ESCAPE '\\' ORDER BY day",
                (cut, pref.replace("_", "\\_") + "%"),
            ).fetchall()
            out.extend({"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                       for r in rows)
        return out
    finally:
        conn.close()


def quarantine_pre_epoch(rows: List[Dict[str, Any]]) -> tuple:
    kept = [r for r in rows if str(r.get("day") or "") >= EPOCH_DAY]
    dropped = len(rows) - len(kept)
    return kept, dropped


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    tot: Dict[str, int] = {}
    for r in rows:
        a = str(r.get("action") or "")
        tot[a] = tot.get(a, 0) + int(r.get("n") or 0)
    return tot


def observed_days(now: Optional[float] = None) -> int:
    try:
        epoch = date.fromisoformat(EPOCH_DAY)
    except Exception:
        return 0
    today = date.fromisoformat(_utc_day(now))
    return max(0, (today - epoch).days + 1)


def _eta(min_days: int, now: Optional[float] = None) -> str:
    try:
        need = max(0, min_days - observed_days(now))
        return (date.fromisoformat(_utc_day(now)) + timedelta(days=need)).isoformat()
    except Exception:
        return "?"


def verdicts(totals: Dict[str, int], *, min_days: int = DEFAULT_MIN_DAYS,
             min_total: int = DEFAULT_MIN_TOTAL,
             now: Optional[float] = None) -> List[Dict[str, str]]:
    """四问判词（纯函数，喂 totals 即可单测）。样本闸门：满 min_days 且总量达标。"""
    out: List[Dict[str, str]] = []
    days = observed_days(now)
    grand = sum(totals.get(k, 0) for ks in BUCKETS.values() for k in ks)

    def _gate() -> Optional[str]:
        if days < min_days:
            return f"样本不足（观察 {days}/{min_days} 天，约 {_eta(min_days, now)} 可裁决）"
        if grand < min_total:
            return (f"样本不足（总点击 {grand} < {min_total}；零点击若持续，"
                    "先查埋点链是否活着再谈清退）")
        return None

    gate = _gate()

    # Q1 右键入口活性
    entry = {k: totals.get(k, 0) for k in BUCKETS["ctx_entry"]}
    if gate:
        v1 = gate
    else:
        dead = [k for k, v in entry.items() if v == 0]
        v1 = (f"全部入口有流量 {entry}" if not dead
              else f"零点击入口 {dead}（右键项成本低建议保留；对应**行内按钮**若同样为零可清退）")
    out.append({"q": "右键翻译组入口活性", "data": json.dumps(entry, ensure_ascii=False),
                "verdict": v1})

    # Q2 多语言对照价值
    cmp_n, lang_n = totals.get("xlctx_cmp", 0), totals.get("xlctx_cmp_lang", 0)
    if gate:
        v2 = gate
    elif cmp_n == 0:
        v2 = "对照矩阵零打开——先裁 Q1 的 cmp 入口，再谈语言维度"
    else:
        r = lang_n / cmp_n
        v2 = (f"加开语言率 {r:.0%}（{lang_n}/{cmp_n}）——"
              + ("≥30%：值得做「记住语言组合」偏好" if r >= 0.3 else
                 ("10-30%：维持现状（手动加开够用）" if r >= 0.1 else
                  "<10%：默认单语已覆盖，语言集不扩")))
    out.append({"q": "对照矩阵多语言价值", "data": f"cmp={cmp_n} lang={lang_n}",
                "verdict": v2})

    # Q3 灯箱贴回价值
    run_n, patch_n = totals.get("lbxl_run", 0), totals.get("lbxl_patch", 0)
    view_n = totals.get("lbxl_patch_view", 0)
    if gate:
        v3 = gate
    elif run_n == 0:
        v3 = "灯箱识别翻译零使用——贴回无从谈起，优先review灯箱入口可发现性"
    else:
        r = patch_n / run_n
        v3 = (f"贴回转化 {r:.0%}（{patch_n}/{run_n}），回看 {view_n}——"
              + ("≥25%：立项「识别后自动预生成译文图」" if r >= 0.25 else
                 "维持手动按钮（低频高价值形态）"))
    out.append({"q": "灯箱贴回价值", "data": f"run={run_n} patch={patch_n} view={view_n}",
                "verdict": v3})

    # Q4 音视频文件加工用量
    au, vi = totals.get("docxl_audio", 0), totals.get("docxl_video", 0)
    srt_dl = totals.get("docxl_srt_dl", 0)
    done = totals.get("docxl_audio_done", 0)
    if gate:
        v4 = gate
    else:
        v4 = (f"音频 {au} / 视频 {vi} / 完成 {done} / 字幕下载 {srt_dl}——"
              + ("音频高频：立项长音频分片管线（>25MB/长时）" if au >= 30 else
                 "用量常规：现有 25MB/15min 上限够用")
              + ("；字幕下载活跃：双语勾选转默认开" if srt_dl >= 10 else ""))
    out.append({"q": "音视频加工用量", "data": f"audio={au} video={vi} srt_dl={srt_dl}",
                "verdict": v4})
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows GBK 控制台防崩
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    report: Dict[str, Any] = {"epoch": EPOCH_DAY, "roots": []}
    for root in roots:
        db = Path(root) / "config" / "ui_event_trend.db"
        entry: Dict[str, Any] = {"root": str(root), "db": str(db)}
        try:
            rows = read_rows(db, days=args.days)
        except FileNotFoundError:
            entry["skip"] = "ui_event_trend.db 不存在（该根未开 ops.ui_event_trend）"
            report["roots"].append(entry)
            continue
        rows, dropped = quarantine_pre_epoch(rows)
        totals = summarize(rows)
        entry["quarantined_pre_epoch"] = dropped
        entry["totals"] = totals
        entry["verdicts"] = verdicts(totals, min_days=args.min_days,
                                     min_total=args.min_total)
        report["roots"].append(entry)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"== 翻译工具用量裁决（纪元 {EPOCH_DAY}，观察 {observed_days()} 天）==")
    for e in report["roots"]:
        print(f"\n-- 数据根: {e['root']}")
        if e.get("skip"):
            print(f"   [SKIP] {e['skip']}")
            continue
        if e.get("quarantined_pre_epoch"):
            print(f"   （纪元前隔离 {e['quarantined_pre_epoch']} 行）")
        tot = e.get("totals") or {}
        if tot:
            for k in sorted(tot):
                print(f"   {k:<22} {tot[k]}")
        else:
            print("   （窗口内零埋点行）")
        for v in e.get("verdicts") or []:
            print(f"   ▸ {v['q']}: {v['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
