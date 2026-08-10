# -*- coding: utf-8 -*-
"""A 线出站媒体镜像的验收探针（只读；2026-07-31 随镜像修复落地）。

修复前的病象：Telegram 出站消息里，真正带 ``media_type`` 的行几乎为零，而文本形态的
媒体占位（``[图片] 配文`` / ``[语音]×3``）成百上千——因为 A 线（pyrogram 直发）只往
收件箱写一行纯文字。后果：坐席看不到自家人设发出去的图；任何按 ``media_type`` 统计
出站媒体的看板把 A 线整条漏掉。

**验收方式**：修复只在「A 线下一次真发媒体」时生效，所以这里比的是**时间切面**——
指定 ``--since``（缺省=最近 2 小时），看该时间点之后新增的 TG 出站媒体里，
带 ``media_type`` 的占比是否已经起来。

⚠ 口径演进（2026-08-02 分条语音逐条镜像 + 媒体行去前缀）：新写入的媒体行 text
不再带 ``[图片]/[语音]`` 标记（语义由 media_type 承载），故 ``marked_text`` 只会
命中**存量旧行**与「发布失败退回纯文本」的兜底行；新的核心缺口指标是
``ref_gap = media_rows - with_ref``（落了媒体行、却没有可回放归档的条数——
对应 outbound_mirror_stats 的 publish 失败计数）。

用法::

    python tools/check_outbound_media_mirror.py                # 最近 2 小时
    python tools/check_outbound_media_mirror.py --since-hours 24
    python tools/check_outbound_media_mirror.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

_MEDIA_MARKS = ("[图片]", "[语音]", "[视频]")


def collect(db_path: Path, since_ts: float) -> dict:
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT c.platform, COALESCE(m.media_type,''), COALESCE(m.text,''),"
            "       COALESCE(m.media_ref,'')"
            " FROM messages m JOIN conversations c"
            "   ON c.conversation_id = m.conversation_id"
            " WHERE m.direction='out' AND m.ts >= ?", (since_ts,),
        ).fetchall()
    finally:
        con.close()

    out: dict = {}
    for platform, mtype, text, mref in rows:
        st = out.setdefault(platform, {
            "outbound": 0, "media_rows": 0, "marked_text": 0, "with_ref": 0})
        st["outbound"] += 1
        if mtype:
            st["media_rows"] += 1
        if mref:
            st["with_ref"] += 1
        if any(k in text for k in _MEDIA_MARKS):
            st["marked_text"] += 1
    for st in out.values():
        # text_gap＝发了媒体但只记成文字（旧病，修复后新增应趋 0）；
        # ref_gap＝落了媒体行但无可回放归档（publish 失败，配 outbound_mirror_stats 查因）。
        st["text_gap"] = max(0, st["marked_text"] - st["media_rows"])
        st["ref_gap"] = max(0, st["media_rows"] - st["with_ref"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="A 线出站媒体镜像验收（只读）")
    ap.add_argument("--since-hours", type=float, default=2.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="")
    args = ap.parse_args()

    since = time.time() - args.since_hours * 3600
    report = {}
    for root in resolve_data_roots(args.data_root):
        db = Path(root) / "config" / "inbox.db"
        if not db.is_file():
            continue
        report[str(root)] = collect(db, since)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("-- outbound media mirror check (since %.1fh) --" % args.since_hours)
    for root, per in report.items():
        print("  %s" % root)
        if not per:
            print("    (窗口内无出站消息)")
        for platform in sorted(per):
            s = per[platform]
            # marked_text = 文本里带媒体标记的条数（新口径下只剩存量旧行）；
            # media_rows = 真正落了 media_type 的；ref_gap = 媒体行里无可回放归档的。
            print("    %-10s outbound=%-5s media_rows=%-4s marked_text=%-4s "
                  "with_ref=%-4s 漏账=%-4s 无档=%s"
                  % (platform, s["outbound"], s["media_rows"], s["marked_text"],
                     s["with_ref"], s["text_gap"], s["ref_gap"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
