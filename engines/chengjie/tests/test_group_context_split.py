# -*- coding: utf-8 -*-
"""P1-2 群聊/私聊上下文分窗门禁。

三条不变量：
1. 判据安全性——**不能**用 ``chat_id != user_id`` 判群（protocol 链 user_id 为
   复合键 ``platform:acct:chat``、chat_id 为裸键，字符串恒不等；误判=全部协议
   私聊断窗）。只信显式 ``is_group`` / ``chat_type`` 群信号。
2. 键格式与 ``ConversationScope.context_key`` 同构（SSOT 对齐门禁）。
3. 群窗/私窗互不污染；episodic 记忆键刻意不带群维度（用户事实跨窗共享）。
"""

import types

import pytest

from src.skills.skill_manager import SkillManager
from src.utils.context_store import ContextStore, make_context_key
from src.utils.conversation_scope import ConversationScope

_scope = SkillManager._context_chat_scope


# ── 1. 判据安全性 ──────────────────────────────────────────────────────────

def test_no_signal_never_splits():
    """无显式群信号一律不分窗——即使 chat_id 与 user_id 字符串不等。"""
    # protocol_autoreply 实景：user_id 复合键、chat_id 裸键（恒不等）
    assert _scope(
        {"chat_id": "639273815533"},
        "whatsapp:639270135480:639273815533") == ""
    # TG 私聊：chat_id == user_id
    assert _scope({"chat_id": 5433982810}, "5433982810") == ""
    # 空上下文
    assert _scope(None, "5433982810") == ""
    assert _scope({}, "5433982810") == ""
    # chat_type=private 显式私聊
    assert _scope(
        {"chat_type": "private", "chat_id": "abc"}, "5433982810") == ""


def test_explicit_group_signal_splits():
    assert _scope(
        {"is_group": True, "chat_id": -1008881234}, "5433982810",
    ) == "-1008881234"
    assert _scope(
        {"chat_type": "supergroup", "chat_id": -1008881234}, "5433982810",
    ) == "-1008881234"
    assert _scope(
        {"chat_type": "channel", "chat_id": -1009}, "5433982810") == "-1009"


def test_group_signal_defensive_fallbacks():
    """群信号但 chat_id 缺失/等于 user → 退化不分（绝不造坏键）。"""
    assert _scope({"is_group": True}, "5433982810") == ""
    assert _scope({"is_group": True, "chat_id": ""}, "5433982810") == ""
    assert _scope({"is_group": True, "chat_id": 5433982810}, "5433982810") == ""


# ── 2/3. 键格式对齐 + 窗口隔离 ─────────────────────────────────────────────

def _sm_stub(tmp_path):
    sm = types.SimpleNamespace()
    sm._context_store = ContextStore(tmp_path / "ctx.db")
    sm._get_user_context = types.MethodType(
        SkillManager._get_user_context, sm)
    return sm


def test_key_matches_conversation_scope(tmp_path):
    """群窗键与 ConversationScope.context_key 同构（SSOT 锁定）。"""
    sm = _sm_stub(tmp_path)
    cs = ConversationScope(
        platform="telegram", account_id="8244899900",
        chat_key="5433982810", chat_id="-1008881234")
    ctx = sm._get_user_context(
        "5433982810", account_id="8244899900", chat_scope="-1008881234")
    assert ctx["_context_store_key"] == cs.context_key()
    assert ctx["_context_store_key"] == make_context_key(
        "-1008881234:5433982810", "8244899900")


def test_group_and_private_windows_isolated(tmp_path):
    """同一用户同一账号：群聊窗与私聊窗各自独立，互不见字段。"""
    sm = _sm_stub(tmp_path)
    priv = sm._get_user_context("5433982810", account_id="8244899900")
    priv["last_message"] = "私聊里说的悄悄话"
    grp = sm._get_user_context(
        "5433982810", account_id="8244899900", chat_scope="-1008881234")
    # 新窗口拿到的是 store 默认空值，绝不是私窗内容
    assert grp.get("last_message", "") != "私聊里说的悄悄话"
    grp["last_message"] = "群里的发言"
    # 回读各自窗口，互不污染
    assert sm._get_user_context(
        "5433982810", account_id="8244899900")["last_message"] == "私聊里说的悄悄话"
    assert sm._get_user_context(
        "5433982810", account_id="8244899900", chat_scope="-1008881234",
    )["last_message"] == "群里的发言"
    # 逻辑 user_id 两窗一致（供 prompt/记忆业务）
    assert priv["user_id"] == grp["user_id"] == "5433982810"


def test_private_key_format_unchanged(tmp_path):
    """私聊键格式一个字节不变（向后兼容：存量数据零迁移）。"""
    sm = _sm_stub(tmp_path)
    bare = sm._get_user_context("5433982810")
    assert bare["_context_store_key"] == "5433982810"
    acct = sm._get_user_context("5433982810", account_id="8244899900")
    assert acct["_context_store_key"] == "8244899900:5433982810"


def test_memory_key_has_no_group_dimension():
    """episodic 记忆键不带群维度：用户事实跨窗共享（ConversationScope 口径）。"""
    cs_grp = ConversationScope(
        platform="telegram", account_id="8244899900",
        chat_key="5433982810", chat_id="-1008881234")
    cs_priv = ConversationScope(
        platform="telegram", account_id="8244899900",
        chat_key="5433982810")
    assert cs_grp.memory_base_key() == cs_priv.memory_base_key()
    assert cs_grp.context_key() != cs_priv.context_key()


def test_check_cooldown_accepts_chat_scope(tmp_path):
    """_check_cooldown 与主路径同窗读取（形参契约钉住，防漂移）。"""
    import inspect
    sig = inspect.signature(SkillManager._check_cooldown)
    assert "chat_scope" in sig.parameters


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
