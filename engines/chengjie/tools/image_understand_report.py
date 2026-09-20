# -*- coding: utf-8 -*-
"""入站图片理解周读（DB 口径，只读；重启不清零的持久观测）。

背景：media_enrich_stats 的 by_type/garbled 是**进程级**计数——本机重启频繁，
周复盘要能回看，得走库口径（与 proactive 线 outreach_mode_histogram 同一教训）。
本 CLI 直接从 inbox.db 消息文本推导（``[图片内容]``/``[视频内容]`` 标记 + 首行
``类型=X`` + 汤判定纯函数），零新表零埋点，跨重启恒真。

读数口径：
  - 窗口内入站图片/视频消息数、有识别描述的占比（＝识别覆盖率，AI 处理过才有）；
  - 类型分布（A 单据 / B 聊天截图 / C 普通图 / untyped 旧产出）；
  - 描述长度 p50/p95（P0 治理目标：非单据 p95 应 ≤150 字级别）；
  - 残余汤（desc_looks_garbled 仍判真的存量/漏网条数，应趋 0）+ 闸门替换文案条数。

用法：python tools/image_understand_report.py [--data-root ...] [--days 7] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT))
sys.path.insert(0, str(ENGINE_ROOT / "scripts"))

from src.inbox.media_enrich import (  # noqa: E402
    GARBLED_DESC_NOTE, MEDIA_DESC_MARKERS, desc_looks_garbled, parse_desc_type,
)

_IMG_KINDS = ("image", "photo", "img", "sticker")
_VID_KINDS = ("video", "video_note", "animation", "gif")


def extract_desc(text: str) -> str:
    """消息文本 → 识别描述段（无标记返回空串）。与 strip_media_desc 互补的另一半。"""
    t = str(text or "")
    idx, tok = -1, 0
    for mk in MEDIA_DESC_MARKERS:
        i = t.find(mk)
        if i >= 0 and (idx < 0 or i < idx):
            idx, tok = i, len(mk)
    if idx < 0:
        return ""
    return t[idx + tok:].strip()


def _pct(xs: List[int], p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return xs[k]


def analyze_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """纯函数：消息行 → 报告 dict（可单测）。"""
    n = len(rows)
    with_desc, by_type = 0, {"A": 0, "B": 0, "C": 0, "untyped": 0}
    lens: List[int] = []
    soup_left, gated = 0, 0
    for r in rows:
        desc = extract_desc(r.get("text") or "")
        if not desc:
            continue
        with_desc += 1
        code, body = parse_desc_type(desc)
        by_type[code if code in ("A", "B", "C") else "untyped"] += 1
        lens.append(len(body))
        if GARBLED_DESC_NOTE in desc:
            gated += 1
        elif desc_looks_garbled(desc):
            soup_left += 1
    return {
        "inbound_media_msgs": n,
        "with_desc": with_desc,
        "desc_coverage": round(with_desc / n, 3) if n else 0.0,
        "by_type": by_type,
        "desc_len_p50": _pct(lens, 0.50),
        "desc_len_p95": _pct(lens, 0.95),
        "soup_residual": soup_left,
        "garbled_gated": gated,
    }


def _scan_root(root: Path, days: int) -> Dict[str, Any]:
    db = root / "config" / "inbox.db"
    if not db.is_file():
        return {"root": str(root), "error": "no inbox.db"}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        kinds = _IMG_KINDS + _VID_KINDS
        cut = time.time() - days * 86400
        rows = [dict(r) for r in con.execute(
            "SELECT text FROM messages WHERE direction='in' AND media_type IN (%s) "
            "AND ts >= ?" % ",".join("?" * len(kinds)), (*kinds, cut))]
    finally:
        con.close()
    out = analyze_rows(rows)
    out["root"] = str(root)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.data_root:
        roots = [Path(args.data_root)]
    else:
        try:
            from _data_root import discover_instance_roots
            roots = discover_instance_roots() or [ENGINE_ROOT]
        except Exception:
            roots = [ENGINE_ROOT]
    reports = [_scan_root(r, args.days) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"== {rep.get('root')} (近 {args.days} 天)")
        if rep.get("error"):
            print("   ", rep["error"])
            continue
        print("   入站图/视频消息 %d 条，识别覆盖率 %.1f%%（%d 条有描述）" % (
            rep["inbound_media_msgs"], rep["desc_coverage"] * 100, rep["with_desc"]))
        bt = rep["by_type"]
        print("   类型分布 A单据=%d B聊天截图=%d C普通图=%d 未标记=%d" % (
            bt["A"], bt["B"], bt["C"], bt["untyped"]))
        print("   描述长度 p50=%d p95=%d 字；残余汤=%d 闸门替换=%d" % (
            rep["desc_len_p50"], rep["desc_len_p95"],
            rep["soup_residual"], rep["garbled_gated"]))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
