"""事实锁单测（2026-07-15「还没吃→刚吃了面包」自相矛盾事故修复）。

防重复/角度系统注入多样性压力时，prompt 必须同时带事实一致性硬约束。
"""
from __future__ import annotations

from types import SimpleNamespace


def _mk_ai(cfg):
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)   # 绕过 __init__ 重依赖，只测纯 prompt 构建
    c.config = SimpleNamespace(config=cfg)
    return c


def test_anti_repeat_hint_carries_fact_lock():
    """有 _anti_repeat_hint（角度轮换/复读重试/guardrail regen）→ 事实锁必在。"""
    p = _mk_ai({})._build_context_prompt({
        "last_message": "吃饭了吗",
        "last_reply": "还没吃呢，刚下课回来",
        "_anti_repeat_hint": "换一种完全不同的开头和语气来回答。",
    })
    assert "角度切换指令" in p
    assert "事实锁" in p
    assert "绝不能改变你已说过的事实" in p
    # 事实锁跟在角度指令同一块里（多样性压力与约束成对出现）
    assert p.index("事实锁") > p.index("角度切换指令")


def test_no_hint_no_fact_lock_noise():
    """无多样性压力 → 不注入事实锁（不给普通轮次加无谓 token）。"""
    p = _mk_ai({})._build_context_prompt({
        "last_message": "吃饭了吗",
        "last_reply": "还没吃呢",
    })
    assert "事实锁" not in p


def test_repeated_message_block_requires_fact_consistency():
    """用户真复读（不同 mid 同文本）话术里也带事实一致硬性要求。"""
    p = _mk_ai({})._build_context_prompt({
        "last_message": "吃饭了吗",
        "_is_repeated_message": True,
        "_prev_reply_for_repeat": "还没吃呢，刚下课回来",
    })
    assert "一模一样的消息" in p
    assert "事实必须与上次一致" in p
