# -*- coding: utf-8 -*-
"""官方 bot 发送通道（src/ops/bug_bot.py）门禁（实施82 P0）。

红线：
- 本模块**只许 sendMessage/getMe**——绝不出现 getUpdates/setWebhook/deleteWebhook
  （该 bot 的 webhook 挂着官网活跃消费链，抢 update＝打崩龙珠/订单/客服核销）；
- 自咬环守卫：官方 bot 的消息进观察管线必须被硬压制（bot 发的公示满是 bug 词，
  不压制会被自己登记成新工单）；判定失败 fail-open 为 False（宁漏压不误压真用户）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def bb(tmp_path, monkeypatch):
    from src.integrations import notify_webhooks_store as store
    from src.ops import bug_bot
    p = tmp_path / "notify_webhooks.json"
    p.write_text(json.dumps([{
        "name": "tg-ops", "format": "telegram", "enabled": True,
        "token": "123:TESTTOKEN", "target": "1", "events": ["bug_intake"],
    }]), encoding="utf-8")
    store.set_store_path(p)
    bug_bot.reset_cache_for_tests()
    yield bug_bot
    store.set_store_path(None)
    bug_bot.reset_cache_for_tests()


def test_token_from_store(bb):
    assert bb.bot_token() == "123:TESTTOKEN"


def test_bot_send_enabled_default_true(bb):
    assert bb.bot_send_enabled({}) is True
    assert bb.bot_send_enabled(None) is True
    assert bb.bot_send_enabled({"bug_intake": {"bot_send": False}}) is False


def test_send_group_html_payload_and_msgid(bb, monkeypatch):
    captured = {}

    def fake_call(method, body, **kw):
        captured["method"] = method
        captured["body"] = body
        return True, {"ok": True, "result": {"message_id": 777}}

    monkeypatch.setattr(bb, "_api_call", fake_call)
    ok, mid, note = bb.send_group_html("-1004345824259", "<b>hi</b>",
                                       reply_to_message_id=42)
    assert ok and mid == 777
    assert captured["method"] == "sendMessage"
    assert captured["body"]["chat_id"] == -1004345824259
    assert captured["body"]["parse_mode"] == "HTML"
    assert captured["body"]["reply_to_message_id"] == 42
    # 非法 chat_id 快败不打网络
    ok2, mid2, note2 = bb.send_group_html("webuser:x", "hi")
    assert not ok2 and note2 == "bad_chat_id"


def test_send_reply_to_degrades_once(bb, monkeypatch):
    """引用的原消息被删 → 去掉 reply_to 重发一次（引用是增强不是前提）。"""
    calls = []

    def fake_call(method, body, **kw):
        calls.append(dict(body))
        if "reply_to_message_id" in body:
            return False, {"ok": False,
                           "description": "Bad Request: reply message not found"}
        return True, {"ok": True, "result": {"message_id": 9}}

    monkeypatch.setattr(bb, "_api_call", fake_call)
    ok, mid, _ = bb.send_group_html("-1", "t", reply_to_message_id=5)
    assert ok and mid == 9 and len(calls) == 2
    assert "reply_to_message_id" not in calls[1]


def test_is_official_bot_uses_cache_and_fails_open(bb, monkeypatch):
    # 注入 getMe 成功 → 命中
    monkeypatch.setattr(
        bb, "_api_call",
        lambda m, b, **kw: (True, {"ok": True, "result": {"id": 8506426282}}))
    assert bb.is_official_bot("8506426282") is True
    assert bb.is_official_bot(8506426282) is True
    assert bb.is_official_bot("5433982810") is False
    # 非数字 sender 直接 False（webuser:* 等异构 id）
    assert bb.is_official_bot("webuser:x") is False
    # getMe 失败 → fail-open False（绝不误压真用户）
    bb.reset_cache_for_tests()
    monkeypatch.setattr(bb, "_api_call",
                        lambda m, b, **kw: (False, {"error": "down"}))
    assert bb.is_official_bot("8506426282") is False


def test_build_verify_keyboard_contract(bb):
    kb = bb.build_verify_keyboard(44, "5433982810")
    row = kb["inline_keyboard"][0]
    assert row[0]["callback_data"] == "btv:44:y:5433982810"
    assert row[1]["callback_data"] == "btv:44:n:5433982810"
    assert "修好了" in row[0]["text"] and "还是不行" in row[1]["text"]
    # callback_data ≤64 字节（Telegram 硬限）
    assert all(len(b["callback_data"].encode()) <= 64 for b in row)
    # 非数字 reporter（webuser 工单）/坏 ticket → 不挂键盘，文字口令兜底
    assert bb.build_verify_keyboard(44, "webuser:x") is None
    assert bb.build_verify_keyboard(0, "42") is None


def test_send_group_html_carries_buttons(bb, monkeypatch):
    captured = {}

    def fake_call(method, body, **kw):
        captured["body"] = body
        return True, {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(bb, "_api_call", fake_call)
    kb = bb.build_verify_keyboard(9, "42")
    ok, _, _ = bb.send_group_html("-1", "t", buttons=kb)
    assert ok and captured["body"]["reply_markup"] == kb
    # 不带键盘时绝不发空 reply_markup（会把用户既有键盘顶掉）
    bb.send_group_html("-1", "t")
    assert "reply_markup" not in captured["body"]


def test_module_never_consumes_updates():
    src = (ROOT / "src" / "ops" / "bug_bot.py").read_text(encoding="utf-8")
    for banned in ("getUpdates", "setWebhook", "deleteWebhook"):
        assert banned not in src, (
            f"bug_bot 出现 {banned}——该 bot 的 webhook 挂着官网活跃消费链，"
            "本模块只许出站 sendMessage/getMe")


def test_trigger_suppresses_official_bot(tmp_path, monkeypatch):
    """观察管线自咬环守卫：官方 bot 的消息（含 bug 词）必须硬压制且不登记工单。"""
    from src.ops import bug_bot, bug_intake
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(bug_intake, "_db_path",
                        lambda: tmp_path / "bug_intake.db")
    monkeypatch.setattr(bug_bot, "is_official_bot",
                        lambda sid: str(sid) == "8506426282")
    cfg = {"bug_intake": {"enabled": True, "groups": [-100123]}}
    # bot 发的周公示（满是 bug 词）→ False 硬压制，零工单
    v = bug_intake.trigger_verdict(
        cfg, -100123, "8506426282",
        "📋 已知问题：#44 line 收不到消息 · #26 生成语音失败", now=1000.0)
    assert v is False
    assert bug_intake.dump_stats()["official_bot_suppressed"] == 1
    assert bug_intake.list_tickets() == []
    # 真用户同文案 → 照常引燃（守卫只认 bot id，不认文案）
    v2 = bug_intake.trigger_verdict(
        cfg, -100123, "500", "line 收不到消息", now=1001.0)
    assert v2 is True
    bug_intake.reset_state_for_tests()