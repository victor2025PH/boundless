# -*- coding: utf-8 -*-
"""add_friend_and_greet 整合测试 — 方案 A profile DM fallback 路径.

mock add_friend_with_note + _send_msg_from_current_profile + send_greeting_after_add_friend
三个 method, 不依赖 atx-agent / 真机. 验整合契约:
  - 方案 A 成功 → dm_only_sent=True / greet_ok=True / via_profile_dm_fallback
  - 方案 A 失败 → 不阻断后续 greet_on_failure 流
  - DM fallback 抛异常 → 不 propagate, 安全兜底
  - greeting/note 来源优先级
  - from_current_profile=False / add_ok=True → 不触发 DM fallback
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fb(monkeypatch):
    """跳过 __init__, 挂上整合测试用的 mock dependencies."""
    from src.app_automation import facebook as fb_mod
    inst = fb_mod.FacebookAutomation.__new__(fb_mod.FacebookAutomation)
    # add_friend_with_note: MagicMock — 每个 test 单独控制返值
    inst.add_friend_with_note = MagicMock(return_value=False)
    # _send_msg_from_current_profile: MagicMock — 每个 test 单独控制
    inst._send_msg_from_current_profile = MagicMock(
        return_value=(False, "no_message_button"))
    # send_greeting_after_add_friend
    inst.send_greeting_after_add_friend = MagicMock(return_value=False)
    inst._last_greet_skip_reason = ""
    # _did / _u2 — 给 device-resolution stub
    inst._did = MagicMock(return_value="DEV")
    inst._u2 = MagicMock(return_value=MagicMock(name="mock_u2_device"))
    return inst


# ---- A. 方案 A 成功路径 ----

def test_dm_fallback_success_sets_all_flags_and_returns_early(fb):
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.return_value = (True, "sent")

    out = fb.add_friend_and_greet(
        profile_name="斉藤正和",
        greeting="你好",
        device_id="DEV",
        from_current_profile=True,
    )

    assert out["add_friend_ok"] is False
    assert out["dm_only_sent"] is True
    assert out["greet_ok"] is True
    assert out["greet_skipped_reason"] == "via_profile_dm_fallback"
    # return early — send_greeting_after_add_friend 不应被调
    fb.send_greeting_after_add_friend.assert_not_called()
    # _send_msg_from_current_profile 被调一次, 用 greeting
    fb._send_msg_from_current_profile.assert_called_once()
    args, _ = fb._send_msg_from_current_profile.call_args
    assert args[2] == "你好"  # (d, did, message)


# ---- B. 方案 A 失败 + 默认 greet_on_failure=False ----

def test_dm_fallback_fails_then_skip_with_add_friend_failed(fb):
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.return_value = (
        False, "no_message_button")

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="hi",
        from_current_profile=True,
        # greet_on_failure 默认 False
    )

    assert out["dm_only_sent"] is False
    assert out["greet_ok"] is False
    assert out["greet_skipped_reason"] == "add_friend_failed"
    # send_greeting_after_add_friend 不应被调 (greet_on_failure=False 短路)
    fb.send_greeting_after_add_friend.assert_not_called()


# ---- C. DM fallback 抛异常 不 propagate ----

def test_dm_fallback_exception_swallowed_and_continues_normally(fb):
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.side_effect = RuntimeError(
        "atx-agent died")

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="hi",
        from_current_profile=True,
    )

    # 异常被吞, 继续到 add_friend_failed 短路
    assert out["dm_only_sent"] is False
    assert out["greet_skipped_reason"] == "add_friend_failed"


# ---- D. greeting 空 + note 非空 → DM 用 note ----

def test_dm_uses_note_when_greeting_empty(fb):
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.return_value = (True, "sent")

    fb.add_friend_and_greet(
        profile_name="X",
        greeting="",         # 空
        note="加个好友",     # 非空
        from_current_profile=True,
    )

    fb._send_msg_from_current_profile.assert_called_once()
    args, _ = fb._send_msg_from_current_profile.call_args
    assert args[2] == "加个好友"


# ---- E. greeting + note 都空 → 不触发 DM fallback ----

def test_no_dm_fallback_when_both_greeting_and_note_empty(fb):
    fb.add_friend_with_note.return_value = False

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="",
        note="",
        from_current_profile=True,
    )

    fb._send_msg_from_current_profile.assert_not_called()
    assert out["dm_only_sent"] is False
    assert out["greet_skipped_reason"] == "add_friend_failed"


# ---- F. from_current_profile=False → 不触发 DM fallback ----

def test_no_dm_fallback_when_from_current_profile_false(fb):
    fb.add_friend_with_note.return_value = False

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="hi",
        from_current_profile=False,
    )

    fb._send_msg_from_current_profile.assert_not_called()
    assert out["dm_only_sent"] is False
    assert out["greet_skipped_reason"] == "add_friend_failed"


# ---- G. add_ok=True → 不触发 DM fallback (走正常 greet) ----

def test_no_dm_fallback_when_add_friend_succeeded(fb):
    fb.add_friend_with_note.return_value = True
    fb.send_greeting_after_add_friend.return_value = True

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="hi",
        from_current_profile=True,
    )

    # add_ok=True → 走正常 greet, 不走方案 A
    fb._send_msg_from_current_profile.assert_not_called()
    fb.send_greeting_after_add_friend.assert_called_once()
    assert out["add_friend_ok"] is True
    assert out["dm_only_sent"] is False
    assert out["greet_ok"] is True


# ---- H. greeting 优先 over note 当两者都非空 ----

def test_greeting_takes_priority_over_note_for_dm_message(fb):
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.return_value = (True, "sent")

    fb.add_friend_and_greet(
        profile_name="X",
        greeting="GREET_TEXT",
        note="NOTE_TEXT",
        from_current_profile=True,
    )

    args, _ = fb._send_msg_from_current_profile.call_args
    assert args[2] == "GREET_TEXT"  # greeting 优先


# ---- I. greet_on_failure=True + DM fallback 失败 → 仍尝试正常 greet ----

def test_greet_on_failure_true_continues_to_normal_greet_after_dm_miss(fb):
    """边界: greet_on_failure=True 时, DM fallback 失败后仍跑正常 greet."""
    fb.add_friend_with_note.return_value = False
    fb._send_msg_from_current_profile.return_value = (
        False, "no_message_button")
    fb.send_greeting_after_add_friend.return_value = True

    out = fb.add_friend_and_greet(
        profile_name="X",
        greeting="hi",
        from_current_profile=True,
        greet_on_failure=True,  # 显式开
    )

    # DM fallback 被试过
    fb._send_msg_from_current_profile.assert_called_once()
    # 失败后没 return early, 继续到正常 greet
    fb.send_greeting_after_add_friend.assert_called_once()
    assert out["greet_ok"] is True
    assert out["dm_only_sent"] is False
