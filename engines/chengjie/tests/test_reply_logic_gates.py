"""回复逻辑闸门纯函数单测（UI「Telegram → 回复逻辑」4 项失效配置接线）。

只测 src/client/reply_logic_gates.py 的纯函数——不 import telegram_client，
避免 pyrogram 依赖。覆盖：
- cooldown_remaining：缺省/0/负值不限制、未到期返回剩余秒数、已到期归 0
- consecutive_limit_reached：缺省不限制、达标/未达标、静默超 30 分钟自动复位
- should_ignore_edited：缺省 True、显式 False、无 edit_date 恒 False
"""
from __future__ import annotations

import pytest

from src.client.reply_logic_gates import (
    DEFAULT_STREAK_RESET_AFTER,
    consecutive_limit_reached,
    cooldown_remaining,
    group_allowlist_blocked,
    normalize_chat_type,
    reply_logic_key,
    should_ignore_edited,
)


# ── cooldown_remaining ───────────────────────────────────────────────────────
def test_cooldown_missing_key_means_unlimited():
    assert cooldown_remaining({}, last_reply_ts=1000.0, now=1001.0) == 0.0


def test_cooldown_zero_means_unlimited():
    cfg = {"cooldown_seconds": 0}
    assert cooldown_remaining(cfg, last_reply_ts=1000.0, now=1000.5) == 0.0


def test_cooldown_negative_means_unlimited():
    cfg = {"cooldown_seconds": -5}
    assert cooldown_remaining(cfg, last_reply_ts=1000.0, now=1000.5) == 0.0


def test_cooldown_never_replied_not_limited():
    cfg = {"cooldown_seconds": 60}
    assert cooldown_remaining(cfg, last_reply_ts=None, now=1000.0) == 0.0


def test_cooldown_active_returns_remaining_seconds():
    cfg = {"cooldown_seconds": 60}
    # 上次回复 20 秒前 → 还剩 40 秒
    assert cooldown_remaining(cfg, last_reply_ts=1000.0, now=1020.0) == pytest.approx(40.0)


def test_cooldown_expired_returns_zero():
    cfg = {"cooldown_seconds": 60}
    assert cooldown_remaining(cfg, last_reply_ts=1000.0, now=1061.0) == 0.0


def test_cooldown_tolerates_string_config_value():
    cfg = {"cooldown_seconds": "30"}
    assert cooldown_remaining(cfg, last_reply_ts=1000.0, now=1010.0) == pytest.approx(20.0)


# ── consecutive_limit_reached ────────────────────────────────────────────────
def test_consecutive_missing_key_means_unlimited():
    hit, eff = consecutive_limit_reached({}, count=99, last_reply_ts=1000.0, now=1001.0)
    assert hit is False
    assert eff == 99  # 不限制但生效计数照常返回


def test_consecutive_zero_means_unlimited():
    cfg = {"max_consecutive_replies": 0}
    hit, _ = consecutive_limit_reached(cfg, count=50, last_reply_ts=1000.0, now=1001.0)
    assert hit is False


def test_consecutive_below_limit_allows():
    cfg = {"max_consecutive_replies": 5}
    hit, eff = consecutive_limit_reached(cfg, count=4, last_reply_ts=1000.0, now=1001.0)
    assert hit is False
    assert eff == 4


def test_consecutive_at_limit_blocks():
    cfg = {"max_consecutive_replies": 5}
    hit, eff = consecutive_limit_reached(cfg, count=5, last_reply_ts=1000.0, now=1001.0)
    assert hit is True
    assert eff == 5


def test_consecutive_resets_after_silence():
    """静默超过 reset_after（默认 30 分钟）→ 计数复位，重新可回。"""
    cfg = {"max_consecutive_replies": 5}
    silent_now = 1000.0 + DEFAULT_STREAK_RESET_AFTER + 1
    hit, eff = consecutive_limit_reached(cfg, count=5, last_reply_ts=1000.0, now=silent_now)
    assert hit is False
    assert eff == 0  # 生效计数归零 → 调用方可落地


def test_consecutive_within_reset_window_keeps_count():
    cfg = {"max_consecutive_replies": 5}
    within_now = 1000.0 + DEFAULT_STREAK_RESET_AFTER - 1
    hit, eff = consecutive_limit_reached(cfg, count=5, last_reply_ts=1000.0, now=within_now)
    assert hit is True
    assert eff == 5


def test_consecutive_never_replied_counts_as_zero():
    cfg = {"max_consecutive_replies": 1}
    hit, eff = consecutive_limit_reached(cfg, count=3, last_reply_ts=None, now=1000.0)
    assert hit is False
    assert eff == 0


def test_consecutive_custom_reset_after():
    cfg = {"max_consecutive_replies": 2}
    hit, eff = consecutive_limit_reached(
        cfg, count=2, last_reply_ts=1000.0, now=1011.0, reset_after=10.0)
    assert hit is False
    assert eff == 0


# ── should_ignore_edited ─────────────────────────────────────────────────────
def test_ignore_edited_defaults_to_true():
    assert should_ignore_edited({}, edit_date=1234567890) is True


def test_ignore_edited_explicit_false_forwards():
    cfg = {"ignore_edited": False}
    assert should_ignore_edited(cfg, edit_date=1234567890) is False


def test_ignore_edited_no_edit_date_never_ignores():
    assert should_ignore_edited({}, edit_date=None) is False
    assert should_ignore_edited({"ignore_edited": True}, edit_date=None) is False


def test_ignore_edited_explicit_true():
    cfg = {"ignore_edited": True}
    assert should_ignore_edited(cfg, edit_date=1234567890) is True


def test_ignore_edited_none_cfg_safe():
    assert should_ignore_edited(None, edit_date=1234567890) is True


# ── group_allowlist_blocked（P3-1 群聊灰度白名单）──────────────────────────────
def test_group_allowlist_missing_means_unrestricted():
    """缺省/空名单 = 不限制（历史行为一字不变）。"""
    assert group_allowlist_blocked({}, -1003142518418) is False
    assert group_allowlist_blocked(None, -1003142518418) is False
    assert group_allowlist_blocked({"allowlist_chat_ids": []}, -100) is False


def test_group_allowlist_bad_type_fails_open():
    """名单类型坏（str/dict/数字）→ 视为不限制，绝不误杀全部群。"""
    assert group_allowlist_blocked({"allowlist_chat_ids": "oops"}, -1) is False
    assert group_allowlist_blocked({"allowlist_chat_ids": 123}, -1) is False
    assert group_allowlist_blocked({"allowlist_chat_ids": [""]}, -1) is False


def test_group_allowlist_blocks_unlisted_group():
    cfg = {"allowlist_chat_ids": [-1003142518418]}
    assert group_allowlist_blocked(cfg, -1003088334335) is True


def test_group_allowlist_allows_listed_group_int_or_str():
    """int/str 混填均可匹配（YAML 手填两种形态都合法）。"""
    cfg = {"allowlist_chat_ids": [-1003142518418, "-1003431196068"]}
    assert group_allowlist_blocked(cfg, -1003142518418) is False
    assert group_allowlist_blocked(cfg, "-1003142518418") is False
    assert group_allowlist_blocked(cfg, -1003431196068) is False
    assert group_allowlist_blocked(cfg, " -1003431196068 ") is False


# ── normalize_chat_type（pyrogram 枚举 → 规范小写名）───────────────────────────
def test_normalize_chat_type_pyrogram_enum():
    """pyrogram ChatType 枚举必须取 .name——str() 出 "ChatType.SUPERGROUP"，
    直接 lower 永远匹配不上，群消息会误入私聊上下文窗（2026-07-25 实锤）。"""
    import enum

    class ChatType(enum.Enum):  # 形态与 pyrogram.enums.ChatType 同构
        PRIVATE = "private"
        GROUP = "group"
        SUPERGROUP = "supergroup"
        CHANNEL = "channel"

    assert normalize_chat_type(ChatType.SUPERGROUP) == "supergroup"
    assert normalize_chat_type(ChatType.GROUP) == "group"
    assert normalize_chat_type(ChatType.PRIVATE) == "private"
    # 反例钉死回归：str(枚举) 的旧写法产物绝不该是归一化输出
    assert normalize_chat_type(ChatType.SUPERGROUP) != "chattype.supergroup"


def test_normalize_chat_type_plain_string_and_empty():
    """纯字符串（旧版本/测试桩）原样规范化；None/空安全返 ""。"""
    assert normalize_chat_type("SUPERGROUP") == "supergroup"
    assert normalize_chat_type(" group ") == "group"
    assert normalize_chat_type(None) == ""
    assert normalize_chat_type("") == ""


def test_normalize_chat_type_value_only_duck_type():
    """只有 .value 的鸭子类型不得归一成 `<object at 0x…>` 那种垃圾值。

    收件箱侧 tg_chat_dict 与本函数共用归一（频道/群之分靠它，负数 chat_id 启发式
    把频道也算群），桩对象与旧 pyrogram 分支都只带 value。
    """
    class _T:
        def __init__(self, value):
            self.value = value

    assert normalize_chat_type(_T("channel")) == "channel"
    assert normalize_chat_type(_T("supergroup")) == "supergroup"
    assert "object at" not in normalize_chat_type(_T("group"))


# ── reply_logic_key（闸门读 × 记账写 同键契约，2026-08-03 死闸门修复）────────
# 历史 bug：闸门做「双号隔离」时把读键改成 {account}:{chat}:{user}，记账侧仍写
# 旧键 {chat}:{user} → 读写永不相交 → last_reply_ts 恒 None → UI「回复逻辑」的
# 冷却与最大连续回复**静默失效**（SpamBot 80 秒 8 轮空转事故里两道闸都没响）。
def test_reply_logic_key_format():
    assert reply_logic_key("acct", 123, 456) == "acct:123:456"
    assert reply_logic_key("default", "8921664288", "178220800") == (
        "default:8921664288:178220800")


def test_recorder_writes_the_key_the_gate_reads():
    """回归钉：sender 记账落的键必须能被闸门读回（端到端语义）。"""
    import logging
    from types import SimpleNamespace

    from src.client.sender import TelegramSenderMixin

    fake = SimpleNamespace(
        _auto_reply_ts={},
        _auto_reply_streak={},
        account_id="acct1",
        logger=logging.getLogger("test_reply_logic"),
    )
    TelegramSenderMixin._record_auto_reply(fake, 123, 456)
    key = reply_logic_key("acct1", 123, 456)
    assert key in fake._auto_reply_ts, (
        "记账键与闸门读键不一致——冷却/连发上限会静默失效")
    assert fake._auto_reply_streak.get(key) == 1
    # 记账后的时间戳必须真的能让冷却闸拦住下一条
    cfg = {"cooldown_seconds": 60}
    ts = fake._auto_reply_ts[key]
    assert cooldown_remaining(cfg, ts, ts + 1.0) > 0
    # 连发计数随后续记账递增（复位语义共用 effective_streak）
    TelegramSenderMixin._record_auto_reply(fake, 123, 456)
    assert fake._auto_reply_streak.get(key) == 2


def test_recorder_missing_account_id_still_consistent():
    """无 account_id 属性的宿主（测试桩/旧形态）回落 'default' 分桶，不炸不丢。"""
    import logging
    from types import SimpleNamespace

    from src.client.sender import TelegramSenderMixin

    fake = SimpleNamespace(
        _auto_reply_ts={},
        _auto_reply_streak={},
        logger=logging.getLogger("test_reply_logic"),
    )
    TelegramSenderMixin._record_auto_reply(fake, 1, 2)
    assert reply_logic_key("default", 1, 2) in fake._auto_reply_ts


def test_gate_source_uses_same_key_format():
    """静态契约：telegram_client 闸门侧的读键构造必须与 reply_logic_key 同格式。

    不 import telegram_client（避免 pyrogram 依赖），扫源码钉住：要么保持
    ``f"{self.account_id}:{chat_id}:{user_id}"`` 字面量，要么已重构为直接调用
    ``reply_logic_key(``——两种形态都视为契约成立；两者都消失即红（说明有人
    改了闸门键格式，记账侧必须同步，否则闸门再次变死）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "client"
           / "telegram_client.py").read_text(encoding="utf-8")
    assert (
        'f"{self.account_id}:{chat_id}:{user_id}"' in src
        or "reply_logic_key(" in src
    ), "telegram_client 闸门读键格式已漂移，须与 reply_logic_key 对齐"
