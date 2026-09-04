# -*- coding: utf-8 -*-
"""回访积压告警判定 + 汇总记账筛选（I-6 F1，2026-09-04）。

事故：61 单 fixed 从 08-30 静默积压到 09-04——标 fixed 走 CLI 不触发回访、
且没有任何计划任务看积压。这里钉住两件纯函数：
- ``backlog_verdict``：够多、够老才响；去抖窗内不重提；空积压永不响。
- ``duty_notify_summary.select_rows``：按人收窄，两个过滤都空＝拒绝全冲。
"""
from __future__ import annotations

import time

from tools.duty_notify_pending import backlog_verdict, pending_rows
from tools.duty_notify_summary import select_rows

NOW = 1_800_000_000.0
H = 3600.0


def _row(i, age_h, rep="r1", status="fixed", notify_ts=0, chat="-100"):
    return {"id": i, "status": status, "notify_ts": notify_ts, "chat_id": chat,
            "reporter_id": rep, "updated_ts": NOW - age_h * H}


def test_verdict_quiet_when_few_or_young():
    assert backlog_verdict([], NOW) == (False, 0.0, 0)
    fire, oldest, n = backlog_verdict([_row(1, 30), _row(2, 30)], NOW)
    assert (fire, n) == (False, 2)                      # 够老但不够多
    fire, oldest, n = backlog_verdict([_row(i, 2) for i in range(5)], NOW)
    assert (fire, n) == (False, 5)                      # 够多但太新（等发版的正常态）


def test_verdict_fires_and_debounces():
    rows = [_row(1, 40), _row(2, 3), _row(3, 1)]
    fire, oldest, n = backlog_verdict(rows, NOW)
    assert fire and n == 3 and abs(oldest - 40) < 1e-6  # 最老口径＝min(updated_ts)
    assert backlog_verdict(rows, NOW, last_alert_ts=NOW - 5 * H)[0] is False   # 12h 去抖
    assert backlog_verdict(rows, NOW, last_alert_ts=NOW - 13 * H)[0] is True   # 过窗重提


def test_pending_rows_excludes_webuser_and_notified():
    rows = [_row(1, 30), _row(2, 30, notify_ts=1.0), _row(3, 30, chat="webuser:x"),
            _row(4, 30, status="confirmed")]
    assert [r["id"] for r in pending_rows(rows)] == [1]


def test_select_rows_by_reporter_refuses_blank_filters():
    rows = [_row(1, 30, "a"), _row(2, 30, "b"), _row(3, 30, "a")]
    assert select_rows(rows, [], []) == []                        # 两空＝拒绝全冲
    assert [r["id"] for r in select_rows(rows, ["a"], [])] == [1, 3]
    assert [r["id"] for r in select_rows(rows, ["a"], [3])] == [3]
    assert [r["id"] for r in select_rows(rows, ["a", "b"], [])] == [1, 2, 3]
