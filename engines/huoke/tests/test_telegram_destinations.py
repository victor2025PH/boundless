# -*- coding: utf-8 -*-
"""telegram_destinations 单元测试。"""

import pytest

from src.host.telegram_destinations import (
    expand_telegram_notify_targets,
    has_user_telegram_destination,
    lines_to_recipients,
    parse_line,
)


def test_parse_line_skips_invite():
    tid, reason = parse_line("https://t.me/+AbCdEfGhIjK")
    assert tid is None and reason == "invite_link"


def test_lines_to_recipients_skips_invite_and_dedup():
    text = "111\nhttps://t.me/+Xx\n111\n@user"
    out = lines_to_recipients(text)
    assert out == ["111", "@user"]


def test_has_user_true_for_chat_or_recipients():
    assert has_user_telegram_destination({"chat_id": "1"})
    assert has_user_telegram_destination({"recipients": ["@a"]})
    assert not has_user_telegram_destination({"recipients": []})
    assert not has_user_telegram_destination({})


def test_expand_order_primary_then_recipients(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CC_CHAT_ID", "")
    tg = {"chat_id": "100", "recipients": ["200", "100"]}
    assert expand_telegram_notify_targets(tg) == ["100", "200"]


def test_normalize_notifications_payload_splits_recipients():
    from src.host.routers.notifications import _normalize_notifications_payload

    body = {
        "telegram": {
            "bot_token": "x",
            "chat_id": "1",
            "recipients": " @a \n b ",
        }
    }
    out = _normalize_notifications_payload(body)
    assert out["telegram"]["recipients"] == ["@a", "b"]
    assert out["telegram"]["invite_link_notes"] == ""
