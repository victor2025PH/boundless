# -*- coding: utf-8 -*-
"""出站语言错配复盘扫描（P1-198，2026-08-03，只读）。

背景：7/31 与 8/03 两起「中文原样发给外语客户」事故都是**人翻聊天记录**才发现的。
本工具把「过去 N 天里有多少外语客户收到过中文」变成一条命令——量化爆炸半径 +
产出需要人工安抚/复盘的会话清单；接进周批后即语言一致性的趋势轨道。

判定口径（与线上语言硬闸同源，全部走证据剥离）：
  - 会话客户语言 = 窗口内入站消息的加权多数决（``vote_language``，系统注入的
    emoji 加注/识图描述/媒体占位不投票）；判不出或本就是 CJK 语种的会话跳过；
  - 错配出站 = 剥离系统注入后**仍含 CJK**的出站行（``[图片] caption`` 这类
    占位行剥空后不误报；语音行的中文转写会命中——克隆声念中文同样是错配）。

已知噪声（如实交代）：镜像行存的是「发出的文本」，历史上个别路径存原文而非
译文（media caption 族），此类会被计入——本工具是**复盘清单**不是告警，
清单逐条人工确认后再安抚客户。

用法::

    python tools/scan_outbound_lang_mismatch.py                # 全部实例根，近 30 天
    python tools/scan_outbound_lang_mismatch.py --days 7 --top 10
    python tools/scan_outbound_lang_mismatch.py --json
    python tools/scan_outbound_lang_mismatch.py --db D:\\path\\inbox.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.ai.lang_policy import strip_neutral_tokens  # noqa: E402
from src.ai.translation_service import detect_language  # noqa: E402
from src.inbox.outbound_translate import (  # noqa: E402
    cjk_substantial,
    lang_is_cjk,
    vote_language,
)

_VOTE_WINDOW = 12
_SNIPPET = 48


def classify_conversation(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """单会话窗口内的错配判定（纯函数，供门禁直测）。

    rows: ``[{direction, text, ts}]``（按 ts 升序）。
    返回 None＝无判定依据（客户语言判不出）或客户本就说 CJK 语种（不在本口径）；
    否则 ``{expected, out_rows, mismatches: [{ts, snippet}]}``。
    """
    inbound = [r for r in rows if str(r.get("direction") or "in") != "out"]
    expected = vote_language(inbound[-_VOTE_WINDOW:], detect=detect_language)
    if not expected or lang_is_cjk(expected):
        return None
    out_rows = [r for r in rows if str(r.get("direction") or "") == "out"]
    mismatches: List[Dict[str, Any]] = []
    for r in out_rows:
        text = str(r.get("text") or "")
        if not text:
            continue
        # 先剥系统注入（[图片] 占位/表情加注），再按「实质性 CJK」口径判定——
        # 与线上语言硬闸同一函数（cjk_substantial）：英文消息引用「村BA」这类
        # 中文专名不算错配（2026-08-04 生产首扫实锤的误伤面）。
        core = strip_neutral_tokens(text)
        if core and cjk_substantial(core):
            mismatches.append({
                "ts": float(r.get("ts") or 0),
                "snippet": text[:_SNIPPET],
            })
    return {
        "expected": expected,
        "out_rows": len(out_rows),
        "out_ts": [float(r.get("ts") or 0) for r in out_rows],
        "mismatches": mismatches,
    }


def _day(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts or 0)))
    except Exception:
        return "unknown"


def scan_db(db_path: Path, since_ts: float) -> Dict[str, Any]:
    """扫一个 inbox.db（只读连接，对活体生产库零写事务）。

    口径要点（2026-08-04 首周批实锤修正）：**客户语言证据不限窗口**（每会话取
    最近 12 条入站，会话属性），出站只数窗口内的（行为窗口）——否则被主动触达
    打扰却没回话的「沉默客户」在短窗里零入站证据 → 整会话被跳过，而这正是
    proactive 错配的高发人群（Yasmin 6 条中文晨安在 7 天窗下曾这样隐身）。
    """
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    by_conv: Dict[str, Dict[str, Any]] = {}
    try:
        out_rows = con.execute(
            "SELECT m.conversation_id, COALESCE(m.text,''), COALESCE(m.ts,0),"
            "       COALESCE(c.platform,''), COALESCE(c.display_name,'')"
            " FROM messages m JOIN conversations c"
            "   ON c.conversation_id = m.conversation_id"
            " WHERE m.ts >= ? AND m.direction = 'out'"
            " ORDER BY m.conversation_id, m.ts", (since_ts,),
        ).fetchall()
        for cid, text, ts, platform, name in out_rows:
            slot = by_conv.setdefault(str(cid), {
                "platform": str(platform), "display_name": str(name), "rows": []})
            slot["rows"].append({"direction": "out", "text": text, "ts": ts})
        # 每会话的客户语言证据：最近 12 条入站，**不限时间窗**（会话属性）
        for cid, slot in by_conv.items():
            inbound = con.execute(
                "SELECT COALESCE(text,''), COALESCE(ts,0) FROM messages"
                " WHERE conversation_id = ? AND direction != 'out'"
                " ORDER BY ts DESC LIMIT ?", (cid, _VOTE_WINDOW),
            ).fetchall()
            slot["rows"] = (
                [{"direction": "in", "text": t, "ts": ts}
                 for t, ts in reversed(inbound)]
                + slot["rows"])
    finally:
        con.close()

    summary = {
        "convs_scanned": len(by_conv), "convs_judged": 0,
        "out_rows_checked": 0, "mismatch_rows": 0, "mismatch_convs": 0,
    }
    by_day: Dict[str, Dict[str, int]] = {}
    detail: List[Dict[str, Any]] = []
    for cid, slot in by_conv.items():
        res = classify_conversation(slot["rows"])
        if res is None:
            continue
        summary["convs_judged"] += 1
        summary["out_rows_checked"] += res["out_rows"]
        for _ts in res.get("out_ts") or []:
            _d = by_day.setdefault(_day(_ts), {"out_rows": 0, "mismatch_rows": 0})
            _d["out_rows"] += 1
        for _m in res["mismatches"]:
            _d = by_day.setdefault(
                _day(_m["ts"]), {"out_rows": 0, "mismatch_rows": 0})
            _d["mismatch_rows"] += 1
        if res["mismatches"]:
            summary["mismatch_convs"] += 1
            summary["mismatch_rows"] += len(res["mismatches"])
            last = max(m["ts"] for m in res["mismatches"])
            detail.append({
                "conversation_id": cid,
                "platform": slot["platform"],
                "display_name": slot["display_name"],
                "expected": res["expected"],
                "mismatch_count": len(res["mismatches"]),
                "last_ts": last,
                "last_snippet": [
                    m["snippet"] for m in res["mismatches"] if m["ts"] == last][0],
            })
    detail.sort(key=lambda d: (-d["mismatch_count"], -d["last_ts"]))
    summary["mismatch_rate"] = (
        round(summary["mismatch_rows"] / summary["out_rows_checked"], 4)
        if summary["out_rows_checked"] else 0.0)
    summary["by_day"] = {k: by_day[k] for k in sorted(by_day)}
    return {"summary": summary, "conversations": detail}


def main() -> int:
    try:  # Windows GBK 控制台防乱码（repo 已知坑：PS5.1 默认编码）
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="出站语言错配复盘扫描（只读）")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--db", default="", help="直接指定 inbox.db 路径（跳过数据根解析）")
    ap.add_argument("--out-jsonl", default="",
                    help="按根追加一行趋势摘要（周批：语言一致率 KPI 时间线）")
    args = ap.parse_args()

    since = time.time() - args.days * 86400
    report: Dict[str, Any] = {}
    if args.db:
        db = Path(args.db)
        if db.is_file():
            report[str(db)] = scan_db(db, since)
    else:
        for root in resolve_data_roots(args.data_root):
            db = Path(root) / "config" / "inbox.db"
            if db.is_file():
                report[str(root)] = scan_db(db, since)

    if args.out_jsonl:
        # 趋势行：每根一行（对齐 translation/proactive 周批 JSONL 口径——
        # 摘要含 by_day，周审画「语言一致率」时间线无需重扫历史窗口）
        out = Path(args.out_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            for root, rep in report.items():
                fh.write(json.dumps({
                    "ts": time.time(), "days": args.days, "root": root,
                    "summary": rep["summary"],
                }, ensure_ascii=False) + "\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("-- outbound language mismatch scan (last %.0f days) --" % args.days)
    if not report:
        print("  (未找到 inbox.db)")
    for root, rep in report.items():
        s = rep["summary"]
        print("  %s" % root)
        print("    会话扫描=%s 可判定(非CJK客户)=%s 出站行=%s"
              % (s["convs_scanned"], s["convs_judged"], s["out_rows_checked"]))
        print("    错配行=%s (%.2f%%)  涉及会话=%s"
              % (s["mismatch_rows"], s["mismatch_rate"] * 100, s["mismatch_convs"]))
        for d in rep["conversations"][:max(0, args.top)]:
            when = time.strftime("%m-%d %H:%M", time.localtime(d["last_ts"]))
            print("      [%s] %-12s %-16s expected=%-4s x%-3s last=%s  %s"
                  % (d["platform"], (d["display_name"] or "-")[:12],
                     d["conversation_id"][:16], d["expected"],
                     d["mismatch_count"], when, d["last_snippet"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
