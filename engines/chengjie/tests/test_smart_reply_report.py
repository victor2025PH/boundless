# -*- coding: utf-8 -*-
"""smart-reply 分段耗时报表纯函数门禁（scripts/smart_reply_report.py）。

钉住：新旧两种日志行格式的解析（分段/无分段/脏行）、分位数、路径分布、
最慢 Top 排序——报表是周读决策（提速方向/弱段归因）的读数面，算错比没有更糟。
"""

from __future__ import annotations

from scripts.smart_reply_report import (
    parse_smart_reply_line,
    render_text,
    summarize,
)

_NEW = ("[2026-08-01 11:47:54] [INFO] src.web.routes.unified_inbox_desktop_routes: "
        "[smart_reply] mode=reply ok=1 gloss=0 instr=0 ms=68269 gen=68269 xlate=0 "
        "gloss_ms=0 path=unified conv=telegram::gate_selfcheck")
_OLD = ("[2026-08-01 09:49:30] [INFO] src.web.routes.unified_inbox_desktop_routes: "
        "[smart_reply] mode=reply ok=1 gloss=0 ms=3397 conv=telegram:a:b")


def test_parse_new_format_with_segments():
    e = parse_smart_reply_line(_NEW)
    assert e is not None
    assert e["has_segments"] is True
    assert e["ms"] == 68269 and e["gen"] == 68269
    assert e["xlate"] == 0 and e["gloss_ms"] == 0
    assert e["path"] == "unified"
    assert e["ok"] is True
    assert e["conv"] == "telegram::gate_selfcheck"


def test_parse_legacy_format_without_segments():
    e = parse_smart_reply_line(_OLD)
    assert e is not None
    assert e["has_segments"] is False
    assert e["ms"] == 3397 and e["gen"] is None


def test_parse_rejects_unrelated_and_dirty_lines():
    assert parse_smart_reply_line("random text") is None
    assert parse_smart_reply_line(
        "[2026-08-01 09:00:00] [INFO] x: [smart_reply] mode=reply ok=1") is None
    # 数值脏（ms=abc）→ ms 缺失按 None → 行被拒（ms 是必要字段）
    assert parse_smart_reply_line(
        "[2026-08-01 09:00:00] [INFO] x: [smart_reply] mode=reply ok=1 ms=abc "
        "conv=c") is not None  # ms 解析失败回落 0，行保留（宁可保守计入）


def test_summarize_splits_legacy_and_segmented():
    entries = [parse_smart_reply_line(_NEW), parse_smart_reply_line(_OLD)]
    s = summarize([e for e in entries if e])
    assert s["total"] == 2
    assert s["segmented"] == 1
    assert s["legacy_no_segments"] == 1
    assert s["by_path"] == {"unified": 1}
    # 分段统计只吃分段行：p50/max 都应是 68269，不被旧行的 3397 稀释
    assert s["ms"]["p50"] == 68269 and s["ms"]["max"] == 68269


def test_summarize_percentiles_and_slowest_order():
    lines = []
    for i, (ms, path) in enumerate([(1000, "unified"), (2000, "unified"),
                                    (3000, "direct"), (10000, "fallback")]):
        lines.append(
            f"[2026-08-01 10:00:0{i}] [INFO] x: [smart_reply] mode=reply ok=1 "
            f"gloss=0 ms={ms} gen={ms - 100} xlate=50 gloss_ms=50 path={path} conv=c{i}")
    entries = [parse_smart_reply_line(ln) for ln in lines]
    s = summarize([e for e in entries if e], top=2)
    assert s["segmented"] == 4
    assert s["ms"]["p50"] == 2500          # (2000+3000)/2 线性插值
    assert s["ms"]["max"] == 10000
    assert s["by_path"] == {"unified": 2, "direct": 1, "fallback": 1}
    assert [e["ms"] for e in s["slowest"]] == [10000, 3000]   # 降序 + top 截断
    assert s["ok_rate"] == 1.0


def test_render_text_mentions_key_numbers():
    e = parse_smart_reply_line(_NEW)
    txt = render_text(summarize([e]))
    assert "68269" in txt and "unified" in txt
    # 旧格式为 0 时提示语不出现（有分段数据）
    assert "尚无分段数据" not in txt


def test_render_text_empty_and_legacy_only():
    txt = render_text(summarize([]))
    assert "0 次" in txt
    e = parse_smart_reply_line(_OLD)
    txt2 = render_text(summarize([e]))
    assert "尚无分段数据" in txt2


def test_filter_window_keeps_recent_and_dirty_ts():
    import time as _t
    from scripts.smart_reply_report import filter_window
    now = _t.time()
    fresh = dict(parse_smart_reply_line(_NEW), epoch=now - 3600)
    stale = dict(parse_smart_reply_line(_NEW), epoch=now - 10 * 86400)
    dirty = dict(parse_smart_reply_line(_NEW), epoch=0.0)   # ts 解析失败 → 保守保留
    out = filter_window([fresh, stale, dirty], 7, now=now)
    assert fresh in out and dirty in out and stale not in out
    assert filter_window([fresh, stale], 0, now=now) == [fresh, stale]  # 0=全量


def test_trend_row_excludes_conv_and_keeps_aggregates(tmp_path):
    from scripts.smart_reply_report import trend_row
    e = parse_smart_reply_line(_NEW)
    row = trend_row(summarize([e]), 7)
    assert row["days"] == 7 and row["total"] == 1
    assert row["gen"]["max"] == 68269
    assert "slowest" not in row          # conv/chat 键不进趋势文件
    assert "by_path" in row
