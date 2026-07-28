"""私聊屏蔽名单（telegram.no_reply_sender_usernames）—— 助手语义 + 三路径接线门禁。

背景：渠道中心「屏蔽名单」历史上只在群聊路径生效（telegram_client 群 handler 内联判定），
私聊路径完全不读 → UI 宣称「不自动回复」名不副实。修复＝抽单一助手 ``_username_blocked``，
群/私聊实时 handler 与轮询兜底三条入站路径共用；本文件钉住：

1. 助手语义与群路径历史内联实现完全一致（大小写 / 前导 @ / 空列表 / None username）；
2. 三条路径都真的在调助手（源码块静态断言，防「改一条漏两条」再漂移）。
"""
from __future__ import annotations

from pathlib import Path

from src.client.telegram_client import _username_blocked
from tests._source_block import source_block

_TC_PATH = Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py"


# ── 助手语义（与群路径历史实现逐条对齐）────────────────────────────────────────

def test_blocked_exact_match():
    assert _username_blocked(["gxp_notify_bot"], "gxp_notify_bot") is True


def test_blocked_case_insensitive_both_sides():
    # 名单项 lower + 待判 username lower：任意大小写组合都命中
    assert _username_blocked(["GXP_Notify_Bot"], "gxp_notify_bot") is True
    assert _username_blocked(["gxp_notify_bot"], "GXP_NOTIFY_BOT") is True
    assert _username_blocked(["GxP_NoTiFy_BoT"], "gXp_nOtIfY_bOt") is True


def test_blocked_config_entry_with_at_prefix():
    # 名单项去前导 @（渠道中心里 @name 与 name 等价）
    assert _username_blocked(["@gxp_notify_bot"], "gxp_notify_bot") is True
    assert _username_blocked(["@@weird"], "weird") is True  # lstrip('@') 剥掉全部前导 @


def test_blocked_config_entry_with_whitespace():
    # 名单项 strip 空白（UI 换行/空格分隔的残留）
    assert _username_blocked(["  gxp_notify_bot  "], "gxp_notify_bot") is True
    assert _username_blocked(["\t@Foo\n"], "foo") is True


def test_sender_username_whitespace_stripped():
    # 待判 username 侧也 strip + lower（与群路径 `(username or '').strip().lower()` 一致）
    assert _username_blocked(["foo"], "  Foo  ") is True


def test_sender_side_at_not_stripped():
    # 群路径历史语义：sender 侧从不剥 @（Telegram API 的 username 本身不带 @）。
    # 若传入带 @ 的待判值，不命中——语义必须与群路径逐字一致，不做「顺手增强」。
    assert _username_blocked(["foo"], "@foo") is False


def test_not_blocked_when_list_empty_or_none():
    assert _username_blocked([], "anyone") is False
    assert _username_blocked(None, "anyone") is False


def test_not_blocked_when_username_none_or_empty():
    # 无 username 的用户无从匹配 → 恒不命中（群路径 `if uname and ...` 同义）
    assert _username_blocked(["foo"], None) is False
    assert _username_blocked(["foo"], "") is False
    assert _username_blocked(["foo"], "   ") is False


def test_not_blocked_when_not_in_list():
    assert _username_blocked(["foo", "@Bar"], "baz") is False
    # 子串不算命中（整名匹配，非 contains）
    assert _username_blocked(["foobar"], "foo") is False


def test_multiple_entries_any_hit():
    lst = ["@Alice", "  BOB ", "carol"]
    assert _username_blocked(lst, "alice") is True
    assert _username_blocked(lst, "Bob") is True
    assert _username_blocked(lst, "CAROL") is True
    assert _username_blocked(lst, "dave") is False


# ── 接线静态断言（三条入站路径共用同一助手）───────────────────────────────────
#
# 用 tests._source_block 按块头现读文件（并发施工下行号漂移免疫），不 import 重模块。

def test_private_handler_calls_helper():
    block = source_block(_TC_PATH, "async def handle_private_message(")
    assert "_username_blocked(" in block, "私聊实时 handler 未接屏蔽名单助手"
    # 每消息动态读 config（渠道中心保存后热生效），与群路径同口径
    assert "no_reply_sender_usernames" in block


def test_group_handler_calls_same_helper():
    block = source_block(_TC_PATH, "async def handle_group_message(")
    assert "_username_blocked(" in block, "群 handler 应改为调用同一助手（防语义分叉）"
    assert "no_reply_sender_usernames" in block
    # 旧内联判定应已被助手取代（列表推导式归一化不再出现在群 handler 内）
    assert "u.strip().lower().lstrip" not in block


def test_poll_fallback_calls_same_helper():
    # 实时推送通道失效时轮询兜底是私聊唯一入口，必须同判，否则名单在降级模式下失效
    block = source_block(_TC_PATH, "async def _poll_inbound_once(")
    assert "_username_blocked(" in block, "轮询兜底路径未接屏蔽名单助手"
    assert "no_reply_sender_usernames" in block


def test_private_check_placed_after_dedup_claim():
    # 私聊检查必须在去重 claim 之后：先占住 mid，共用同一去重表的轮询兜底
    # 才不会把已屏蔽的消息当「新进站」再捞回来处理（否则名单被轮询绕穿）。
    block = source_block(_TC_PATH, "async def handle_private_message(")
    claim_pos = block.index("self._msg_dedup.claim(")
    blocked_pos = block.index("_username_blocked(")
    assert claim_pos < blocked_pos, "私聊屏蔽检查应位于 _msg_dedup.claim 之后"
