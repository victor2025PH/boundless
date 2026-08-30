"""_goal_block 构建器（context_block，纯函数）门禁。

不变量：≤3 行结构（标题 · 里程碑 / 今日意图 / 恒在纪律行）、suppress_push
只降 direct 不动其他档、max_chars 截断优先截意图行而保头尾安全语义、
无标题（无目标）→ None、goal_view_block 从 service 视图组块。
"""

from __future__ import annotations

from src.companion.goals.context_block import (
    DEFAULT_MAX_CHARS,
    build_goal_block,
    goal_view_block,
)


def _block(**kw):
    base = dict(title="冲一单详批", milestone_label="破冰回暖", milestone_idx=0,
                day_index=3, total_days=14, intent="顺着近况自然回暖",
                push_level="soft")
    base.update(kw)
    return build_goal_block(**base)


# ── 3 行结构 ────────────────────────────────────────────────────────────────

def test_three_line_structure():
    block = _block()
    lines = block.split("\n")
    assert len(lines) == 3
    assert lines[0] == "【工作目标】冲一单详批 · 里程碑 1/4 破冰回暖（第3/14天）"
    assert lines[1].startswith("【今日意图】顺着近况自然回暖（力度：")
    assert lines[2].startswith("【推进纪律】")


def test_remaining_sec_replaces_day_fraction():
    block = _block(remaining_sec=42 * 60)
    assert "剩余42分钟" in block
    assert "第3/14天" not in block
    assert _block(remaining_sec=0).count("即将到期") == 1


def test_discipline_line_always_present():
    for kw in ({}, {"intent": ""}, {"push_level": "direct"},
               {"suppress_push": True}, {"total_days": 0}):
        block = _block(**kw)
        assert "【推进纪律】" in block, kw
        assert "彻底放下目标只陪伴" in block, kw


def test_no_intent_drops_middle_line_only():
    block = _block(intent="")
    lines = block.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("【工作目标】")
    assert lines[1].startswith("【推进纪律】")


def test_day_index_clamped_and_no_days_when_total_zero():
    assert "（第14/14天）" in _block(day_index=99)      # 越界夹到总天数
    assert "（第1/14天）" in _block(day_index=0)        # 下限 1
    assert "（第" not in _block(total_days=0)           # 无总天数 → 不出天数段


def test_negative_milestone_clamped_to_first():
    assert "里程碑 1/4" in _block(milestone_idx=-3)


# ── 力度语义 / suppress_push ────────────────────────────────────────────────

def test_push_labels_by_level():
    assert "营销内容只字不提" in _block(push_level="none")
    assert "轻轻带到" in _block(push_level="soft")
    assert "可以直说" in _block(push_level="direct")
    assert "轻轻带到" in _block(push_level="mega")       # 非法档回落 soft


def test_suppress_push_downgrades_direct_to_soft_only():
    blk = _block(push_level="direct", suppress_push=True)
    assert "可以直说" not in blk and "轻轻带到" in blk
    # 非 direct 档不受 suppress 影响
    assert "营销内容只字不提" in _block(push_level="none", suppress_push=True)
    assert "轻轻带到" in _block(push_level="soft", suppress_push=True)


# ── max_chars 截断 ──────────────────────────────────────────────────────────

def test_max_chars_truncates_intent_keeps_head_and_discipline():
    long_intent = "聊" * 300
    block = _block(intent=long_intent, max_chars=200)
    assert len(block) <= 200
    lines = block.split("\n")
    assert lines[0].startswith("【工作目标】冲一单详批")   # 标题行完整保留
    assert "【推进纪律】" in block                        # 纪律行未被截掉
    assert "…" in lines[1]                               # 意图行被截断标记


def test_max_chars_floor_120():
    block = _block(intent="聊" * 300, max_chars=10)      # 过小 → 地板 120
    assert len(block) <= 120


def test_default_max_chars_no_truncate_for_normal_block():
    block = _block()
    assert len(block) <= DEFAULT_MAX_CHARS
    assert "…" not in block


# ── 无标题 → None ───────────────────────────────────────────────────────────

def test_missing_title_returns_none():
    assert _block(title="") is None
    assert _block(title="   ") is None


# ── goal_view_block 便捷口 ──────────────────────────────────────────────────

def test_goal_view_block_from_view_dict():
    view = {
        "title": "冲会员", "milestone_label": "价值铺垫", "milestone_idx": 1,
        "day_index": 2, "total_days": 21,
        "today": {"intent": "自然带到会员的好处", "push_level": "direct"},
    }
    block = goal_view_block(view)
    assert "【工作目标】冲会员 · 里程碑 2/4 价值铺垫（第2/21天）" in block
    assert "自然带到会员的好处" in block
    assert "可以直说" in block


def test_goal_view_block_suppress_and_bad_inputs():
    view = {
        "title": "冲会员", "milestone_label": "价值铺垫", "milestone_idx": 1,
        "day_index": 2, "total_days": 21,
        "today": {"intent": "带一句", "push_level": "direct"},
    }
    assert "可以直说" not in goal_view_block(view, suppress_push=True)
    assert goal_view_block("not-a-dict") is None
    assert goal_view_block({"today": {"intent": "x"}}) is None   # 无标题 → None


def test_goal_view_block_without_today_beat():
    view = {"title": "冲会员", "milestone_label": "破冰回暖", "milestone_idx": 0,
            "day_index": 1, "total_days": 14, "today": None}
    block = goal_view_block(view)
    assert block.count("\n") == 1                # 无今日意图 → 2 行
    assert "【今日意图】" not in block
    assert "【推进纪律】" in block


# ── P1 2026-08-30：close 收口档 + 冲刺纪律行 ────────────────────────────────

def test_close_push_label_and_sprint_discipline():
    view = {
        "title": "拿到微信号", "milestone_label": "收口", "milestone_idx": 3,
        "day_index": 1, "total_days": 0, "pace": "today",
        "remaining_sec": 1500.0, "total_sec": 10800.0,
        "today": {"intent": "大方求一个答复", "push_level": "close"},
    }
    block = goal_view_block(view)
    assert "剩余25分钟" in block
    assert "可以明确报价" in block                 # close 档语义
    assert "换个角度再推一次" in block              # 冲刺纪律行
    assert "彻底放下目标只陪伴" not in block        # 不再用 natural 纪律
    # 越界红线在冲刺纪律里原样保留
    assert "线下见面" in block


def test_close_demoted_by_suppress_push():
    view = {
        "title": "拿到微信号", "milestone_label": "收口", "milestone_idx": 3,
        "day_index": 1, "total_days": 0, "pace": "today",
        "remaining_sec": 1500.0,
        "today": {"intent": "收口", "push_level": "close"},
    }
    block = goal_view_block(view, suppress_push=True)
    assert "可以明确报价" not in block             # 同轮双线推销 → 降 soft
    assert "轻轻带到" in block


def test_natural_goal_keeps_original_discipline():
    view = {
        "title": "冲会员", "milestone_label": "价值铺垫", "milestone_idx": 1,
        "day_index": 2, "total_days": 21, "pace": "natural",
        "today": {"intent": "带一句", "push_level": "soft"},
    }
    block = goal_view_block(view)
    assert "彻底放下目标只陪伴" in block           # natural 纪律不动
    assert "换个角度再推一次" not in block
