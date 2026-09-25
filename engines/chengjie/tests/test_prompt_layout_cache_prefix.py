# -*- coding: utf-8 -*-
"""缓存友好布局门禁（2026-09-11）：每轮都变的深度人设块必须排在静态段之后、上下文块之前。

DeepSeek 前缀缓存（¥0.04/M vs 未命中 ¥2/M）按字节前缀命中：人设块 → 人称硬规则 → 快速设置 →
语言规则 都是「同一会话内不变」的段，深度人设块（随机瑕疵 / 生活线 / 时钟）若夹在中间，
缓存就在几千 token 处截断。模型读到的信息一字不少，只是顺序。
"""
from __future__ import annotations

import inspect

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {
        "domain": "conversion", "web_admin": {"site_name": "T"},
        # reply_style 必须落在 _STYLE_MAP（concise/warm/professional）。
        # 「亲切」不在表内时，【快速设置覆盖】只靠 ai_name；会话 c1 若被别的用例
        # 绑了 chat_binding / account_profile，名字会被压掉，整段消失（3.13 xdist）。
        "ai": {"ai_name": "苏婉", "reply_style": "warm"},
        "companion": {"deep_persona": {"enabled": True}},
    }

    def get_ai_config(self):
        return {}


def test_deep_persona_block_after_static_sections_before_context(monkeypatch):
    import src.companion.deep_persona as dp
    marker = "【深度人设-测试标记】今天心情不错"
    monkeypatch.setattr(dp, "build_deep_persona_block", lambda *a, **k: marker)
    client = AIClient(_Cfg())
    prompt = client._build_system_instruction(
        {"reply_lang": "en", "chat_id": "layout-cache-prefix-probe"})
    assert marker in prompt, "深度人设块丢了（推后 ≠ 丢弃）"
    i_marker = prompt.find(marker)
    i_quick = prompt.find("【快速设置覆盖】")
    i_lang = prompt.find("LANGUAGE RULE")
    i_ctx = prompt.find("上下文信息:")           # _build_context_prompt 的固定抬头
    assert i_quick > 0 and i_lang > 0 and i_ctx > 0
    assert i_quick < i_marker and i_lang < i_marker, "深度人设块应排在静态段之后"
    assert i_marker < i_ctx, "深度人设块应排在上下文块之前"


def test_source_order_deferred_block_appended_right_before_context():
    src = inspect.getsource(AIClient._build_system_instruction)
    i_def = src.find("_deferred_dp_block = _dp_block")
    i_app = src.find("parts.append(_deferred_dp_block)")
    i_ctx = src.find("context_prompt = self._build_context_prompt(context)")
    assert 0 < i_def < i_app < i_ctx
    # 老写法（紧跟人设块直接 append）不得回潮
    assert "parts.append(_dp_block)" not in src
