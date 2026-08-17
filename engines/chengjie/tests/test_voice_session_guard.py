"""voice_session_guard：peer 语种闸 + 语音后孤儿二稿静默窗。"""
from __future__ import annotations

import time

import pytest

from src.inbox.voice_session_guard import (
    clear_voice_marks_for_tests,
    last_inbound_ts,
    last_outbound_voice_ts,
    mark_voice_delivered,
    resolve_voice_session_cfg,
    should_quiet_after_voice,
    voice_peer_lang_conflict,
)


@pytest.fixture(autouse=True)
def _clean_marks():
    clear_voice_marks_for_tests()
    yield
    clear_voice_marks_for_tests()


def test_resolve_defaults():
    cfg = resolve_voice_session_cfg({})
    assert cfg["peer_lang_gate"] is True
    assert cfg["quiet_after_sec"] == 90.0
    assert resolve_voice_session_cfg({"quiet_after_sec": 0})["quiet_after_sec"] == 0.0


def test_peer_lang_blocks_cjk_to_english():
    # 事故金标：「你在干嘛呢？」进英语会话
    assert voice_peer_lang_conflict("你在干嘛呢？", "en") == "peer_lang_cjk"
    assert voice_peer_lang_conflict("Hey, what are you up to?", "en") == ""
    assert voice_peer_lang_conflict("你在干嘛呢？", "zh") == ""
    assert voice_peer_lang_conflict("你在干嘛呢？", "") == ""  # 未知不拦


def test_peer_lang_ignores_cjk_citation_in_english():
    # 实质性 CJK 才拦；个别汉字引用不误伤
    assert voice_peer_lang_conflict("I like the word 好", "en") == ""


def test_last_voice_and_inbound_ts():
    rows = [
        {"direction": "in", "ts": 100.0, "text": "hi"},
        {"direction": "out", "media_type": "voice", "ts": 110.0},
        {"direction": "out", "media_type": "text", "ts": 120.0, "text": "x"},
    ]
    assert last_outbound_voice_ts(rows) == 110.0
    assert last_inbound_ts(rows) == 100.0


def test_quiet_when_voice_and_no_new_inbound():
    rows = [
        {"direction": "in", "ts": 100.0},
        {"direction": "out", "media_type": "voice", "ts": 200.0},
    ]
    assert should_quiet_after_voice(
        recent_messages=rows, quiet_after_sec=90, now=250.0,
    ) == "voice_quiet"
    # 窗过期
    assert should_quiet_after_voice(
        recent_messages=rows, quiet_after_sec=90, now=400.0,
    ) == ""
    # 语音后又有入站 → 放行
    rows2 = rows + [{"direction": "in", "ts": 210.0, "text": "ok"}]
    assert should_quiet_after_voice(
        recent_messages=rows2, quiet_after_sec=90, now=250.0,
    ) == ""


def test_quiet_uses_process_mark_when_db_lag():
    mark_voice_delivered("wa:1:tom", now=1000.0)
    assert should_quiet_after_voice(
        conv_key="wa:1:tom", recent_messages=[],
        quiet_after_sec=90, now=1020.0,
    ) == "voice_quiet"


def test_quiet_disabled_when_sec_zero():
    rows = [{"direction": "out", "media_type": "voice", "ts": 200.0}]
    assert should_quiet_after_voice(
        recent_messages=rows, quiet_after_sec=0, now=210.0,
    ) == ""


@pytest.mark.asyncio
async def test_autosend_voice_peer_lang_blocks(monkeypatch):
    """外语客户 + 中文念稿 → autosend_voice 早退 False。"""
    from src.inbox import autosend_helpers as ah
    from types import SimpleNamespace

    cfg = {
        "inbox": {"l2_autosend": {"voice": {
            "enabled": True, "trigger": "always", "min_chars": 1,
        }}},
    }
    assistant = SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        inbox_store=SimpleNamespace(
            list_recent_messages=lambda *a, **k: [
                {"direction": "in", "ts": time.time(), "text": "hello",
                 "media_type": "text"},
            ],
            get_conv_meta=lambda *a, **k: {},
        ),
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
        _web_loop=None,
    )

    class _Orch:
        def owns_media(self, *a, **k):
            return True

    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda *a, **k: _Orch())
    monkeypatch.setattr(
        "src.inbox.outbound_translate.peer_language_hint",
        lambda *a, **k: "en")
    monkeypatch.setattr(
        "src.inbox.normalizer.conv_id",
        lambda *a, **k: "whatsapp:acct:tom")

    ok = await ah.autosend_voice(
        assistant, "whatsapp", "acct", "tom", "你在干嘛呢？")
    assert ok is False


@pytest.mark.asyncio
async def test_autosend_voice_quiet_blocks_second(monkeypatch):
    from src.inbox import autosend_helpers as ah
    from src.inbox.voice_session_guard import mark_voice_delivered
    from types import SimpleNamespace

    cfg = {
        "inbox": {"l2_autosend": {"voice": {
            "enabled": True, "trigger": "always", "min_chars": 1,
            "quiet_after_sec": 90,
        }}},
    }
    cid = "whatsapp:acct:tom"
    mark_voice_delivered(cid, now=time.time())
    assistant = SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        inbox_store=SimpleNamespace(
            list_recent_messages=lambda *a, **k: [
                {"direction": "in", "ts": time.time() - 120, "text": "hi"},
            ],
            get_conv_meta=lambda *a, **k: {},
        ),
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
        _web_loop=None,
    )

    class _Orch:
        def owns_media(self, *a, **k):
            return True

    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda *a, **k: _Orch())
    monkeypatch.setattr(
        "src.inbox.outbound_translate.peer_language_hint",
        lambda *a, **k: "en")
    monkeypatch.setattr(
        "src.inbox.normalizer.conv_id", lambda *a, **k: cid)

    ok = await ah.autosend_voice(
        assistant, "whatsapp", "acct", "tom", "Hey there friend")
    assert ok is False
