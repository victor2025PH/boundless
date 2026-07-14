# -*- coding: utf-8 -*-
"""
闲聊 / 说明模式 — 不创建任务、不调设备，仅基于 OpenClaw 使用常识回答。
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

log = logging.getLogger(__name__)

_PING_QUICK = re.compile(
    r"^(在吗|在么|在嘛|在不在|还在吗|还在不|有人吗|有人不|在没|在不)[!！？?。.…\s]*$",
    re.IGNORECASE,
)

_SYSTEM = """你是 OpenClaw 手机群控助手的「说明模式」。
用户在使用控制台，你的回答要求：
1) 用简洁中文，2～6 句；2) 只描述产品能力与操作建议，不要编造实时设备数量或任务状态；
3) 若需要查在线设备、漏斗、日报等，明确提示用户在输入框发送例如「哪些手机在线」「今日日报」「任务为什么失败」等查询句；
4) 若用户想执行任务，提示使用「养号」「关注」「停止所有任务」等指令式说法；
5) 不要输出 JSON、不要假装已执行任何操作。"""


def generate_converse_reply(
    user_message: str,
    history: Optional[List[dict]] = None,
) -> str:
    msg = (user_message or "").strip()
    if _PING_QUICK.match(msg):
        return (
            "在的，我在这里。\n"
            "查在线设备、漏斗、日报可以说「哪些手机在线」「今日日报」；"
            "下任务请用口令，例如「01号养号30分钟」「全部停止」。"
        )
    try:
        from src.ai.llm_client import get_llm_client
    except Exception:
        return _fallback_reply(user_message)
    client = get_llm_client()
    if not getattr(client.config, "api_key", ""):
        return _fallback_reply(user_message)
    hist = history or []
    msgs = [{"role": "system", "content": _SYSTEM}]
    for h in hist[-4:]:
        role = h.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = (h.get("content") or "").strip()
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": msg})
    try:
        text = client.chat_messages(msgs, temperature=0.4, max_tokens=400, use_cache=False)
        text = (text or "").strip()
        if len(text) < 8:
            return _fallback_reply(user_message)
        return text
    except Exception as e:
        log.warning("[converse] LLM error: %s", e)
        return _fallback_reply(user_message)


def _fallback_reply(msg: str) -> str:
    return (
        "你好！我是 OpenClaw 助手。\n"
        "查在线设备、漏斗、日报或任务失败原因，请直接用自然语言问，例如「主控有几台手机在线」「今日日报」「任务为什么失败」。\n"
        "要执行养号、关注、停止任务等，请用指令式说法，例如「01号养号30分钟」「全部停止」。\n"
        f"（当前无法连接说明模型，已使用固定回复。你刚才说：{msg[:80]}）"
    )
