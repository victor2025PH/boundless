# -*- coding: utf-8 -*-
"""工单↔B案对账（tools/duty_reconcile.py）纯函数门禁（实施81 L1）。

红线：**台账没有已修标记的匹配绝不建议 mark_fixed**——把待开发说成已修复
＝对用户撒谎；「已修」标记出现在否定语境（未解决/待修）同样不算。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_reconcile import (  # noqa: E402
    parse_bcase_rows,
    propose,
    render_proposals,
    similarity,
)

MD = """
| 案 | 一句话 | 状态 |
|---|---|---|
| B114 | 克隆语音绑定人设后生成音色仍是默认（根因 hub 超时，B61 族） | ✅ 已修待发版 |
| B116 | Messenger 不可达联系人弹窗循环=P1-9 残留 | 📋 待开发 |
| B114 | 0827 复报：客户端仍偶现，等下一版 | 观察中 |
| B120 | 语音关了仍发：B线/手动链不吃全局闸，未解决 ✅标记是引用别人的话 | 未解决 |
"""


def test_parse_rows_merge_and_fixed_markers():
    rows = {r["bid"]: r for r in parse_bcase_rows(MD, source="实施68")}
    assert set(rows) == {"B114", "B116", "B120"}
    assert rows["B114"]["fixed"] is True          # 任一行已修标记即真
    assert "0827 复报" in rows["B114"]["text"]     # 多行合并
    assert rows["B116"]["fixed"] is False
    # 否定语境里的 ✅ 不算已修（未解决）
    assert rows["B120"]["fixed"] is False
    assert rows["B114"]["source"] == "实施68"


def test_similarity_discriminates():
    ticket = "克隆语音上传了新的语音绑定人设，生成的音色仍然是默认的"
    good = "克隆音色回落=hub 超时（B61 族）语音绑定人设默认音色"
    bad = "Messenger 不可达联系人弹窗循环"
    assert similarity(ticket, good) > similarity(ticket, bad)
    assert similarity(ticket, good) >= 0.25
    assert similarity("", good) == 0.0


def test_propose_never_marks_fixed_without_marker():
    cases = parse_bcase_rows(MD)
    tickets = [
        {"id": 41, "title": "克隆语音绑定人设音色仍是默认",
         "body": "上传语音显示登记成功但生成音色默认"},
        {"id": 34, "title": "Messenger 不可达联系人弹窗循环重试",
         "body": "退出重新登录后仍弹窗"},
        {"id": 99, "title": "完全无关的新问题 xyzabc", "body": "qqq"},
    ]
    props = {p["ticket_id"]: p for p in propose(tickets, cases, min_score=0.2)}
    assert props[41]["action"] == "mark_fixed" and props[41]["bid"] == "B114"
    assert "B114" in props[41]["suggested_fix_note"]
    assert props[34]["action"] == "follow_up", "台账未标已修 → 只许跟进不许 fixed"
    assert props[99]["action"] == "none"
    txt = render_proposals(list(props.values()))
    assert "建议标 fixed" in txt and '--apply "41=B114"' in txt
    assert "勿标 fixed" in txt and "#99" in txt


def test_render_empty():
    assert "无开放工单" in render_proposals([])
