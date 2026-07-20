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
