# -*- coding: utf-8 -*-
"""运维通报脚本（tools/send_ops_group_report.py）数据驱动化门禁——2026-09-10 运维群降噪 P1.2。

此前「本次上线」六条要点写死在代码里、成本接口 404 时整个脚本崩（连通报都发不出）。
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mod():
    p = Path(__file__).resolve().parents[1] / "tools" / "send_ops_group_report.py"
    spec = importlib.util.spec_from_file_location("send_ops_group_report", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _summary_ok(**over):
    s = {"available": True, "provider": "siliconflow", "pricing_configured": True,
         "today": {"calls": 412, "cost": 3.2}, "budget": {"daily": 10.0, "recon_at": "09:40"},
         "balance": None, "last_recon": {}}
    s.update(over)
    return s


def test_release_notes_only_when_file_given(mod, tmp_path):
    assert mod.load_release_notes(tmp_path / "missing.txt") == []
    f = tmp_path / "notes.txt"
    f.write_text("# 注释行\n• 07:31 重启一次\n- 成本计量落盘\n\n第三条\n", encoding="utf-8")
    assert mod.load_release_notes(f) == ["07:31 重启一次", "成本计量落盘", "第三条"]


def test_open_items_from_remind_ledger_json(mod, tmp_path):
    now = time.time()
    st = tmp_path / "health_remind_state.json"
    st.write_text(json.dumps({
        "draft_backlog": {"alerted": True, "first_seen": now - 30 * 86400,
                          "meta": {"summary": "待审草稿 5 条无人处理（最久 30 天）"}},
        "lan_gpu:http://192.168.0.173:8001": {"alerted": True, "first_seen": now - 16 * 3600,
                                             "meta": {"summary": "173 探测失败"}},
        "avatar_voice": {"alerted": False, "first_seen": now - 100},   # 只观察未首提 → 不列
        "_daily_digest": {"alerted": True, "meta": {"last_day": "2026-09-10"}},  # 内部键 → 不列
    }), encoding="utf-8")
    items = mod.load_open_items(st, now=now)
    assert [(x["label"], x["summary"]) for x in items] == [
        ("待审草稿", "待审草稿 5 条无人处理（最久 30 天）"), ("LAN GPU", "173 探测失败")]
    assert items[0]["hours"] > 24 * 29
    assert mod.load_open_items(tmp_path / "nope.json") == []


def test_ops_message_is_data_driven(mod):
    notes = ["成本计量落盘", "演练降频"]
    items = [{"label": "待审草稿", "summary": "待审草稿 5 条无人处理（最久 30 天）", "hours": 712.0},
             {"label": "LAN GPU", "summary": "173 探测失败", "hours": 30.0 * 24}]
    msg = mod.build_ops_message("tok", _summary_ok(), notes=notes, open_items=items,
                                probe_txt="8/8 域正常", now=1_757_500_000.0)
    assert "<b>本次上线</b>" in msg and "• 成本计量落盘" in msg and "• 演练降频" in msg
    assert "07:31 按预检流程重启" not in msg        # 09-09 写死的老文案不许再出现
    assert "真活探针：8/8 域正常" in msg and "412 次云端调用已入账" in msg
    assert "仍未处理</b>（2 项）" in msg
    assert "• 待审草稿 5 条无人处理（最久 30 天）\n" in msg          # 自带时长 → 不追加「已开」
    assert "• LAN GPU：173 探测失败（已开 30 天）" in msg              # 没有时长 → 追加
    # 需要管理员做：没填余额→提余额；没账单真值→提导 CSV；再加未处理项
    assert "1. 成本页填一次厂商余额" in msg and "2. 导一次厂商「费用明细」CSV" in msg
    assert "3. 处理「待审草稿」" in msg and "4. 处理「LAN GPU」" in msg
    assert "/admin/ops" in msg and "/workspace/cost" in msg

    # 没有 notes → 「本次上线」整段不出现；余额与真值都有 → 不再催
    msg2 = mod.build_ops_message(
        "tok", _summary_ok(balance=88.0, last_recon={"truth": 3.1, "internal": 3.0, "verdict": "ok"}),
        notes=[], open_items=[], probe_txt="8/8 域正常")
    assert "本次上线" not in msg2 and "仍未处理</b>　无 ✅" in msg2
    assert "需要管理员做</b>　无" in msg2


def test_ops_message_survives_cost_api_down(mod):
    msg = mod.build_ops_message(
        "tok", {"available": False, "error": "成本页路由未装载（404）——实例需重启以挂上 /workspace/cost"},
        notes=[], open_items=[], probe_txt="7/8 域正常，异常：vision")
    assert "成本账本：⚠️ 成本页路由未装载（404）" in msg
    assert "1. 成本页打不开：按重启纪律重启实例后再发成本日报" in msg
    assert "主链厂商" not in msg
