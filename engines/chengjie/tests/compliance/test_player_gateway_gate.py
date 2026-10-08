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


# ── 红线无条件（蛋博士 2026-10-08 拍板）：脱敏不得受 outbound_text_guard 跳过开关控制 ──

def _bare_sm(cfg=None):
    import logging
    from src.skills.skill_manager import SkillManager

    class _SM(SkillManager):
        logger = logging.getLogger("t")

    sm = _SM.__new__(_SM)
    sm.config = cfg
    return sm


_REPLY = "Sige po, balance mo ay ₱5,000 ngayon, deposit 2000 php kahapon."


def test_enforce_redaction_ignores_skip_switches(monkeypatch):
    """关掉（跳过）全部守卫层后仍脱敏：skip_guard 恒 True 也不影响执法入口。"""
    from src.ai import conv_route
    monkeypatch.setattr(conv_route, "skip_guard", lambda *a, **k: True)
    sm = _bare_sm()
    out = sm._enforce_player_redaction(_REPLY, {"_player_data_block": "x"}, path="b_line")
    assert "5,000" not in out and "2000" not in out and "***" in out


def test_enforce_redaction_when_gateway_enabled_without_block_this_turn():
    """前几轮注入过的余额可能还在历史里：实例开了 player_gateway 就一律打码。"""
    sm = _bare_sm({"player_gateway": {"enabled": True}})
    out = sm._enforce_player_redaction(_REPLY, {}, path="a_line")
    assert "5,000" not in out
    # 没开网关、本轮也没注入 → 普通业务文本不动
    sm2 = _bare_sm({})
    assert sm2._enforce_player_redaction(_REPLY, {}, path="a_line") == _REPLY


def test_enforce_redaction_leaves_trace_without_raw_text(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="src.integrations.wujie_player"):
        red, n = wp.enforce_outbound_redaction(_REPLY, {"_player_data_block": "x", "platform": "whatsapp"},
                                               None, path="b_line")
    assert n >= 2 and "5,000" not in red
    msgs = [r.getMessage() for r in caplog.records if "action=redact" in r.getMessage()]
    assert msgs and "5,000" not in msgs[0] and "path=b_line" in msgs[0]


def test_b_line_redaction_is_outside_outbound_text_guard_skip():
    """结构钉：B 线 generate_inbox_draft 里脱敏调用不嵌在 ``not _skipg(...)`` 条件下；A 线同样无条件。"""
    import ast
    import inspect
    import textwrap
    from src.skills.skill_manager import SkillManager

    def _calls_outside_skip(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        hits = []

        def walk(node, guarded):
            for child in ast.iter_child_nodes(node):
                g = guarded
                if isinstance(child, ast.If) and "_skipg" in ast.unparse(child.test):
                    g = True
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "_enforce_player_redaction"):
                    hits.append(g)
                walk(child, g)
        walk(tree, False)
        return hits

    b = _calls_outside_skip(SkillManager.generate_inbox_draft)
    assert b and not any(b), "B 线脱敏必须有调用且全部不在 _skipg 条件内"
    a = _calls_outside_skip(SkillManager._handle_message_guarded)
    assert a and not any(a)
