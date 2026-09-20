# -*- coding: utf-8 -*-
"""P22：坐席指令（agent_instruction）进 prompt 高权重块。

「采纳并拟稿」语义断链的根因是 smart-reply 不收 instruction；本门禁钉住
ai_client 消费口 + persona_reply / desktop 路由形参契约，防回归成闲聊生成。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from src.ai.ai_client import AIClient
from src.inbox import persona_reply as pr
from src.web.routes import unified_inbox_desktop_routes as desk


def _mk_ai(cfg=None):
    c = AIClient.__new__(AIClient)
    c.config = SimpleNamespace(config=cfg or {})
    return c


def test_agent_instruction_block_in_prompt():
    ai = _mk_ai({"domain": "conversion"})
    out = ai._build_context_prompt({
        "channel": "desktop",
        "intent": "casual",
        "_agent_instruction": "今日工作意图：摸痛点\n力度：陪伴日（只共情）",
    })
    assert "【坐席指令" in out
    assert "摸痛点" in out
    assert "硬推销" in out  # 块尾护栏


def test_agent_instruction_absent_when_empty():
    ai = _mk_ai({"domain": "conversion"})
    out = ai._build_context_prompt({
        "channel": "desktop",
        "intent": "casual",
        "_goal_block": "【营销目标】今日陪伴",
    })
    assert "【坐席指令" not in out
    assert "营销目标" in out


def test_persona_reply_accepts_agent_instruction_kw():
    sig = inspect.signature(pr.generate_persona_reply)
    assert "agent_instruction" in sig.parameters
    # P22.1：opener 产线也收指令（防前端漏切 mode 时静默丢意图）
    sig_op = inspect.signature(pr.generate_topic_opener)
    assert "agent_instruction" in sig_op.parameters


def test_desktop_smart_reply_wires_instruction():
    from pathlib import Path
    src = Path(desk.__file__).read_text(encoding="utf-8")
    assert 'body.get("instruction")' in src
    assert "agent_instruction=instruction" in src
    assert "instr=%s" in src  # 观测：有无坐席指令


def test_persona_reply_fallback_includes_instruction():
    """SkillManager 挂掉走 ai.chat 兜底时，指令仍须进 prompt（否则「采纳」变闲聊）。"""
    from pathlib import Path
    src = Path(pr.__file__).read_text(encoding="utf-8")
    assert "【坐席指令——本条必须完成】" in src
    assert "_inst_block" in src


def test_goal_view_exposes_created_by():
    from src.companion.goals.service import goal_view

    view = goal_view(
        {
            "goal_id": "g1",
            "template": "acquire_and_convert",
            "status": "active",
            "autonomy": "suggest",
            "created_by": "auto_create",
            "start_ts": 1.0,
            "deadline_ts": 1.0 + 14 * 86400,
            "progress": 0.1,
            "milestone_idx": 0,
        },
        None,
    )
    assert view.get("created_by") == "auto_create"
