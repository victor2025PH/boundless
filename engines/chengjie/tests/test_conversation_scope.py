"""ConversationScope 键 SSOT 门禁：四平台隔离矩阵 + 存量格式锁 + 与生产键公式同源。"""
from __future__ import annotations

from src.utils.context_store import make_context_key
from src.utils.conversation_scope import ConversationScope, legacy_key_formats


def test_default_account_bare_key_compat():
    """default/空账号 → 裸键（单号老路径零回归）。"""
    s = ConversationScope(platform="telegram", account_id="default",
                          chat_key="5433982810")
    assert s.context_key() == "5433982810"
    assert s.memory_base_key() == "5433982810"
    assert s.spoken_scope() == ""


def test_companion_account_prefixed():
    s = ConversationScope(platform="telegram", account_id="8244899900",
                          chat_key="5433982810")
    assert s.context_key() == "8244899900:5433982810"
    assert s.memory_base_key() == "8244899900:5433982810"
    assert s.spoken_scope() == "8244899900"


def test_group_chat_gets_own_window():
    """群聊带 chat_id 维度：同 peer 群聊/私聊两窗互不污染（P1-2 语义）。"""
    private = ConversationScope(platform="telegram", account_id="a1",
                                chat_key="u1")
    group = ConversationScope(platform="telegram", account_id="a1",
                              chat_key="u1", chat_id="g99")
    assert private.context_key() != group.context_key()
    assert group.context_key() == "a1:g99:u1"


def test_isolation_matrix_four_platforms_two_accounts():
    """四平台 × 双号 × 同 peer：所有派生键按账号两两不同、同账号确定性稳定。"""
    peer = "shared_peer_007"
    for plat in ("telegram", "whatsapp", "line", "messenger"):
        a = ConversationScope(platform=plat, account_id="acctA", chat_key=peer)
        b = ConversationScope(platform=plat, account_id="acctB", chat_key=peer)
        assert a.context_key() != b.context_key()
        assert a.memory_base_key() != b.memory_base_key()
        assert a.cooldown_key() != b.cooldown_key()
        assert a.spoken_scope() != b.spoken_scope()
        assert a.conversation_id() != b.conversation_id()
        # 同账号重复构造 = 键确定性
        a2 = ConversationScope(platform=plat, account_id="acctA", chat_key=peer)
        assert a2.context_key() == a.context_key()


def test_from_conversation_id_roundtrip():
    s = ConversationScope.from_conversation_id("whatsapp:639270135480:639273815533")
    assert (s.platform, s.account_id, s.chat_key) == (
        "whatsapp", "639270135480", "639273815533")
    assert s.conversation_id() == "whatsapp:639270135480:639273815533"
    # chat_key 自带冒号（wa:acct:peer 之类）不被二次切碎
    s2 = ConversationScope.from_conversation_id("whatsapp:w1:wa:w1:Alice")
    assert s2.chat_key == "wa:w1:Alice"


def test_from_conv_row_fills_from_cid():
    s = ConversationScope.from_conv_row(
        {"conversation_id": "telegram:8244899900:5433982810"})
    assert s.account_id == "8244899900" and s.chat_key == "5433982810"


def test_memory_base_key_same_source_as_production_formula():
    """与生产写入侧公式逐字节同源：SkillManager._episodic_storage_key 的
    make_context_key 那一步（scope=user、无 CPI 时的全量输出）。"""

    class _Stub:  # 只带公式依赖的最小假体，绑真方法跑真代码
        _memory_cfg = {"scope": "user"}
        _cpi = None

    from src.skills.skill_manager import SkillManager
    for acct in ("", "default", "8244899900"):
        got = SkillManager._episodic_storage_key(
            _Stub(), "5433982810", "", "telegram", account_id=acct)
        want = ConversationScope(
            platform="telegram", account_id=acct, chat_key="5433982810",
        ).memory_base_key()
        assert got == want, (acct, got, want)


def test_legacy_format_table_locked():
    """存量键格式 ratchet：这些格式下有生产数据，改格式必须显式改本表+迁移。"""
    fmts = legacy_key_formats()
    assert fmts["a_line_companion"][1] == "8244899900:5433982810"
    assert "双重前缀" in fmts["protocol_autoreply"][0]
    assert "双重前缀" in fmts["whatsapp_rpa"][0]
    assert "P2-1 已接" in fmts["line_rpa"][0]
    # make_context_key 语义锁（本模块一切派生的地基）
    assert make_context_key("u", "") == "u"
    assert make_context_key("u", "default") == "u"
    assert make_context_key("u", "a") == "a:u"
