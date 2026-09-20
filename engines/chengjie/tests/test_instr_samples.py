# -*- coding: utf-8 -*-
"""P23：坐席指令 → 拟稿产出 留样 ring（instr_samples）门禁。

守住三条硬契约：
1. 写读闭环 + 截断上限（指令 200 / 产出 240）——样本是抽检面不是全文备份；
2. ring 保尾重写（超限后旧行被挤出，文件永不无界膨胀）；
3. 全程 best-effort：坏路径写返 False 不抛、坏行读跳过、缺文件返空表
   ——绝不影响拟稿主链（record 挂在 smart-reply 收尾）。
"""

from __future__ import annotations

import json

import pytest

from src.inbox import instr_samples as m


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "instr_samples.jsonl"


def test_record_and_read_roundtrip(path):
    assert m.record_instr_sample(
        instruction="今日工作意图：摸痛点", reply="老板，最近店里忙不忙呀？",
        conv="telegram:a1:100", source="beat", mode="reply",
        persona="lin_jiaxin", path=path, now=1000.0)
    assert m.record_instr_sample(
        instruction="按缺口追问预算", reply="咱们工具这块预算大概什么档呀？",
        conv="telegram:a1:100", source="slot", path=path, now=2000.0)
    rows = m.read_instr_samples(limit=10, path=path)
    assert len(rows) == 2
    # 新→旧
    assert rows[0]["source"] == "slot" and rows[0]["ts"] == 2000.0
    assert rows[1]["instruction"] == "今日工作意图：摸痛点"
    assert rows[1]["persona"] == "lin_jiaxin"
    # limit 生效
    assert len(m.read_instr_samples(limit=1, path=path)) == 1


def test_truncation_caps(path):
    m.record_instr_sample(
        instruction="长" * 500, reply="答" * 500, path=path)
    row = m.read_instr_samples(limit=1, path=path)[0]
    assert len(row["instruction"]) == 200
    assert len(row["reply"]) == 240


def test_empty_fields_not_recorded(path):
    assert not m.record_instr_sample(instruction="", reply="有产出", path=path)
    assert not m.record_instr_sample(instruction="有指令", reply="", path=path)
    assert m.read_instr_samples(path=path) == []


def test_ring_rewrite_keeps_tail(path, monkeypatch):
    monkeypatch.setattr(m, "_MAX_BYTES", 400)
    monkeypatch.setattr(m, "_KEEP", 3)
    for i in range(10):
        m.record_instr_sample(
            instruction=f"指令{i}", reply=f"产出{i}", path=path, now=float(i))
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) <= 4          # 保尾 3 + 本次追加 1
    rows = m.read_instr_samples(limit=10, path=path)
    assert rows[0]["instruction"] == "指令9"   # 最新永远在


def test_read_skips_bad_lines(path):
    m.record_instr_sample(instruction="好指令", reply="好产出", path=path)
    with path.open("a", encoding="utf-8") as f:
        f.write("not-json\n")
        f.write(json.dumps({"no_instruction": 1}) + "\n")
    rows = m.read_instr_samples(limit=10, path=path)
    assert len(rows) == 1 and rows[0]["instruction"] == "好指令"


def test_best_effort_never_raises(tmp_path):
    # 把「目录」当文件路径 → 打开必失败，但只返 False 不抛
    bad = tmp_path / "as_dir"
    bad.mkdir()
    assert m.record_instr_sample(
        instruction="x", reply="y", path=bad) is False
    assert m.read_instr_samples(path=bad) == []


def test_route_uses_reader():
    """路由与 smart-reply 接线的静态契约（重实现漂移时这里先红）。"""
    from pathlib import Path
    import src.web.routes.goal_routes as gr
    import src.web.routes.unified_inbox_desktop_routes as desk
    gr_src = Path(gr.__file__).read_text(encoding="utf-8")
    assert "/api/goals/instr-samples" in gr_src
    assert "read_instr_samples" in gr_src
    desk_src = Path(desk.__file__).read_text(encoding="utf-8")
    assert "record_instr_sample" in desk_src
    assert 'body.get("instruction_source")' in desk_src
    assert 'body.get("goal_id")' in desk_src
    assert "drive_draft" in desk_src
