# -*- coding: utf-8 -*-
"""智安 P0-3（2026-10-08）：余额回述闸（主线 wujie_player）。

红线：「博彩召回回述真人余额」——只用单测 / 假网关验证，绝不真查真发。
验收：TG / Messenger 上「uid 12345678 balance?」不调网关、回复无金额；只有 WA 本人号在
开关打开时才查；请求 bind=False、不带对话原文；注入块与出站回复的金额一律 ***。
"""
from __future__ import annotations

import json

import pytest

from src.integrations import wujie_player as wp

_ON = {"enabled": True, "url": "http://gw.invalid", "key": "k", "visible_facts": True}


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def _boom(*a, **kw):
        calls.append((a, kw))
        raise AssertionError("网关不应被调用")

    monkeypatch.setattr(wp, "fetch_lookup", _boom)
    return calls


@pytest.mark.parametrize("ctx", [
    {"platform": "telegram", "chat_id": "987654321"},
    {"platform": "messenger", "chat_id": "psid-1"},
    {"platform": "whatsapp", "chat_id": "120363@g.us"},
    {"platform": "whatsapp", "chat_id": "2342342342342@lid"},
    {"platform": "whatsapp", "chat_id": "not-a-number@s.whatsapp.net"},
    {},
])
def test_unverified_identity_never_calls_gateway(ctx, no_network):
    c = dict(ctx)
    wp.inject_player_block(c, "uid 12345678 balance ko? 09171234567", {"player_gateway": _ON})
    assert "_player_data_block" not in c
    assert no_network == []


def test_default_config_never_calls_gateway(no_network):
    c = {"platform": "whatsapp", "chat_id": "639171234567@s.whatsapp.net"}
    for cfg in ({}, {"player_gateway": {"enabled": True, "url": "http://x", "key": "k"}},
                {"player_gateway": dict(_ON, enabled=False)}):
        wp.inject_player_block(c, "balance?", cfg)
        assert "_player_data_block" not in c
    assert no_network == []


def test_request_body_is_bind_false_phone_only(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"chatx_text": "Balance: ₱12,345.50 | Deposit today 1000 | VIP Gold"}).encode()

    def _urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(wp.urllib.request, "urlopen", _urlopen)
    c = {"platform": "whatsapp", "chat_id": "639171234567:3@s.whatsapp.net"}
    wp.inject_player_block(c, "uid 99999999 balance?", {"player_gateway": _ON})
    assert captured["body"] == {"phone": "639171234567", "bind": False}
    assert "99999999" not in json.dumps(captured["body"])
    block = c["_player_data_block"]
    assert "12,345" not in block and "1000" not in block and "VIP Gold" in block
    assert block.count("***") >= 2


@pytest.mark.parametrize("text,leak", [
    ("Your balance is ₱12,345.50 po", "12,345"),
    ("Balance mo: 5000", "5000"),
    ("Deposit 1000 at withdraw 500 kahapon", "1000"),
    ("may 2,000 pesos ka sa wallet", "2,000"),
    ("打码量还差 3000，余额 88 元", "3000"),
    ("You have 1500 balance left", "1500"),
    ("turnover required: 25,000 PHP", "25,000"),
])
def test_redact_financials(text, leak):
    out, n = wp.redact_financials(text)
    assert leak not in out and n >= 1 and "***" in out


@pytest.mark.parametrize("text", ["See you at 3pm!", "VIP level 3", "Room 204 tayo", "kain na tayo"])
def test_redact_leaves_non_financial(text):
    assert wp.redact_financials(text) == (text, 0)


def test_outbound_guard_redacts_when_player_block_present():
    """SkillManager._apply_outbound_text_guard：本轮有玩家网关块 → 出站金额脱敏；无块 → 不动。"""
    import logging
    from src.skills.skill_manager import SkillManager

    class _SM(SkillManager):
        logger = logging.getLogger("t")

    sm = _SM.__new__(_SM)
    sm.config = None
    out = SkillManager._apply_outbound_text_guard(
        sm, "Sige po, balance mo ay ₱5,000 ngayon.", user_context={"_player_data_block": "x"})
    assert "5,000" not in out and "***" in out
    out2 = SkillManager._apply_outbound_text_guard(
        sm, "Sige po, balance mo ay ₱5,000 ngayon.", user_context={})
    assert "5,000" in out2
