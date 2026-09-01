# -*- coding: utf-8 -*-
"""值守 SLA 报告（tools/duty_sla_report.py）纯函数门禁（实施81 P2-1）。

口径守卫：burst 折叠按**首条**计等待（客户视角最坏值）/ 员工亲号入站算应答 /
未应答超龄也计入超时（等 3 小时没人理不算超时才是假账）/ 窗口过滤。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_sla_report import (  # noqa: E402
    merge_totals,
    pair_first_response,
    render_report,
    summarize,
)

NOW = time.time()


def _msg(direction, ts, *, sender="500", name="客户A", text="发不出消息",
         media=""):
    return {"direction": direction, "ts": ts, "sender_id": sender,
            "sender_name": name, "text": text, "media_type": media}


def test_burst_collapses_to_first_ask():
    # 客户连发三条 → 一段 burst，等待从**首条**起算
    msgs = [_msg("in", 100), _msg("in", 160, text="在吗"),
            _msg("in", 200, text="怎么回事"), _msg("out", 700, sender="")]
    pairs = pair_first_response(msgs)
    assert len(pairs) == 1
    assert pairs[0]["ask_ts"] == 100 and pairs[0]["resp_ts"] == 700


def test_staff_inbound_closes_burst_and_opens_none():
    msgs = [_msg("in", 100), _msg("in", 300, sender="777", name="值守"),
            _msg("in", 500, text="新问题")]
    pairs = pair_first_response(msgs, staff_ids={"777"})
    assert len(pairs) == 2
    assert pairs[0]["resp_ts"] == 300      # 员工亲号入站算应答
    assert pairs[1]["ask_ts"] == 500 and pairs[1]["resp_ts"] is None


def test_out_of_order_input_ok():
    msgs = [_msg("out", 700, sender=""), _msg("in", 100)]
    pairs = pair_first_response(msgs)
    assert pairs[0]["resp_ts"] == 700


def test_empty_shell_rows_ignored():
    assert pair_first_response([_msg("in", 100, text="", media="")]) == []


def test_media_only_ask_counts():
    pairs = pair_first_response([_msg("in", 100, text="", media="photo")])
    assert len(pairs) == 1 and pairs[0]["text"].startswith("[photo")


def test_summarize_percentiles_threshold_and_window():
    pairs = [
        {"ask_ts": NOW - 3600, "resp_ts": NOW - 3540},         # 1 分钟
        {"ask_ts": NOW - 7200, "resp_ts": NOW - 4500},         # 45 分钟（超）
        {"ask_ts": NOW - 90 * 86400, "resp_ts": NOW - 89 * 86400},  # 窗口外
        {"ask_ts": NOW - 7200, "resp_ts": None},               # 未应答 2h（超）
    ]
    s = summarize(pairs, now=NOW, days=7, threshold_min=30)
    assert s["bursts"] == 3 and s["answered"] == 2 and s["unanswered"] == 1
    assert s["over_threshold"] == 2, "45m 已答 + 2h 未应答都要计超时"
    assert s["p50_min"] == 23.0 and s["max_min"] == 45.0
    assert s["over_rate"] == round(2 / 3, 3)


def test_summarize_empty():
    s = summarize([], now=NOW, days=7)
    assert s["bursts"] == 0 and s["over_rate"] == 0.0 and s["p95_min"] == 0.0


def test_render_and_totals():
    g = {"-1004345824259": summarize(
        [{"ask_ts": NOW - 600, "resp_ts": NOW - 300}], now=NOW, days=7),
         "-1004290740529": summarize([], now=NOW, days=7)}
    tot = merge_totals(g, days=7, threshold_min=30)
    assert tot["bursts"] == 1 and tot["over_threshold"] == 0
    txt = render_report(g, tot, now=NOW)
    assert "内测bug群" in txt and "官方报障群" in txt and "合计" in txt
    assert "🟢" in txt
    # 数据截断要如实标注
    txt2 = render_report(g, tot, now=NOW, partial_groups={"-1004345824259"})
    assert "数据截断" in txt2
