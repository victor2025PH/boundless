# -*- coding: utf-8 -*-
"""已知问题周公示（tools/duty_known_issues.py）纯函数门禁（实施81 P1-3）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_known_issues import compose_digest  # noqa: E402

NOW = time.time()


def _row(tid, status, title="发不出消息", *, count=1, fix_note="",
         updated=None):
    return {"id": tid, "status": status, "title": title,
            "report_count": count, "fix_note": fix_note,
            "updated_ts": NOW if updated is None else updated}


def test_empty_ledger_returns_empty():
    assert compose_digest([], days=7, now=NOW) == ""
    # 全 closed 也不该发（没有内容就闭嘴，空公示=刷屏）
    assert compose_digest([_row(1, "closed")], days=7, now=NOW) == ""


def test_groups_and_order():
    rows = [
        _row(1, "fixed", "崩溃", fix_note="改了重试"),
        _row(2, "in_progress", "语音发不出"),
        _row(3, "new", "界面消失", count=3),
        _row(4, "closed", "旧账"),
    ]
    txt = compose_digest(rows, days=7, now=NOW)
    assert "🔧 已修复" in txt and "🚧 处理中" in txt and "🆕 新报待确认" in txt
    assert "旧账" not in txt
    assert "（改了重试）" in txt, "修复说明必须进公示（P1-4 的消费面）"
    assert "×3" in txt, "多人报告要标注（让报障人知道不孤单）"
    assert txt.index("🔧") < txt.index("🚧") < txt.index("🆕")
    assert "请报障的朋友验证" in txt, "fixed 未验证要带验证召唤"


def test_stale_fixed_rows_drop_out_of_window():
    rows = [_row(1, "fixed", "老修复", updated=NOW - 30 * 86400),
            _row(2, "confirmed", "还在修")]
    txt = compose_digest(rows, days=7, now=NOW)
    assert "老修复" not in txt, "修好很久的旧账不再刷屏"
    assert "还在修" in txt


def test_verified_has_no_verify_call():
    txt = compose_digest([_row(1, "verified", "已双确认")], days=7, now=NOW)
    assert "已双确认" in txt and "请报障的朋友验证" not in txt


def test_title_sanitized_single_line():
    # 真实台账形态：标题携带 VLM 图片摘要多行拼接——必须压成单行
    rows = [_row(1, "new", "语音失败\n[图片内容] 类型=B\n用户与AI对话")]
    txt = compose_digest(rows, days=7, now=NOW)
    line = [ln for ln in txt.split("\n") if ln.startswith("· #1")][0]
    assert "\n" not in line and "[图片内容]" in line


def test_stale_new_rows_excluded_and_sections_capped():
    rows = [_row(i, "new", f"陈年积压{i}", updated=NOW - 30 * 86400)
            for i in range(1, 30)]
    rows += [_row(100 + i, "new", f"本周新报{i}") for i in range(1, 15)]
    txt = compose_digest(rows, days=7, now=NOW)
    assert "陈年积压" not in txt, "窗口外的 new＝陈年积压，公示刷 40 行没人看"
    assert "…等 4 项" in txt, "超过每节上限要折叠（14 条 → 10 + 等4项）"
