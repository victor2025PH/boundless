# -*- coding: utf-8 -*-
"""自动修复派单器（tools/duty_autofix_dispatch.py）纯函数门禁（实施81 L2）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_autofix_dispatch import (  # noqa: E402
    build_summary,
    build_work_order,
    parse_report_sections,
    pick_actionable,
)


def _t(tid, sev="P2", status="new", cat="bug", title="发不出消息"):
    return {"id": tid, "severity": sev, "status": status, "category": cat,
            "title": title, "body": "b", "reporter_name": "张三"}


def test_pick_actionable_filters_and_orders():
    tickets = [
        _t(1, "P2"), _t(2, "P0"), _t(3, "P1"),
        _t(4, "P0", status="fixed"),          # 非 new/confirmed 不派
        _t(5, "P0", cat="usage"),             # 非 bug 不派
        _t(6, "P0", status="confirmed"),
        _t(7, "P2"),
    ]
    state = {"3": {"ts": 1}}                  # 已派过不重派
    got = [t["id"] for t in pick_actionable(tickets, state)]
    assert got == [6, 2, 7, 1], f"P0 先、同级新单（大 id）先、剔除已派/非bug/已修：{got}"
    assert [t["id"] for t in pick_actionable(tickets, state, limit=2)] == [6, 2]


def test_work_order_contract():
    wo = build_work_order(_t(44, "P1", title="line 收不到对方消息"))
    # 约束契约：只读、禁改树、禁重启——自动 agent 在共享树上的安全边界
    assert "只读分析" in wo and "不修改任何文件" in wo and "不重启" in wo
    # 产出契约：五个段落标题必须点名（parse_report_sections 依赖）
    for k in ("根因分析", "建议补丁", "验证方案", "修复说明一句话", "置信度"):
        assert f"## {k}" in wo
    assert "#44" in wo and "line 收不到对方消息" in wo
    assert "AGENTS.md" in wo, "必须先读共享树纪律"


def test_parse_report_sections_and_summary():
    report = """前言废话
## 根因分析
send_gate 在 X 处拦截，见 src/foo.py:120
## 建议补丁
```diff
- a
+ b
```
## 验证方案
pytest tests/test_x.py
## 修复说明一句话
修复了 LINE 收不到消息的问题
## 置信度
high——证据齐全
"""
    s = parse_report_sections(report)
    assert "send_gate" in s["根因分析"]
    assert "diff" in s["建议补丁"]
    assert s["置信度"].startswith("high")
    summary = build_summary(44, s, r"D:\x\report.md")
    assert "#44" in summary and "high" in summary
    assert "标 fixed" in summary, "摘要必须写清批准后的下一步（人保留说已修复的权力）"
    # 缺段不崩
    s2 = parse_report_sections("")
    assert s2["根因分析"] == ""
