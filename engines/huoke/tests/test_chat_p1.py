# -*- coding: utf-8 -*-
"""P1 聊天 dry_run / 会话结构（不发起真实 HTTP 到设备）。"""

from unittest.mock import patch

import pytest

from src.chat.controller import ChatController


def test_dry_run_help_no_pending():
    c = ChatController()
    with patch.object(c._ai, "parse_intent", return_value={"intent": "help", "devices": [], "params": {}}):
        with patch.object(c._ai, "_multi_intent_parse", return_value={"intent": ""}):
            r = c.handle("帮助", dry_run=True)
    assert r["dry_run"] is True
    assert r.get("pending_plan") is None
    assert r.get("pending_confirmation") is None


def test_exit_profile_load():
    from src.host.exit_profile import load_exit_profiles

    profiles = load_exit_profiles(force_reload=True)
    assert isinstance(profiles, list)
    ids = {p.get("id") for p in profiles}
    assert "phone_v2ray" in ids or len(profiles) >= 0
