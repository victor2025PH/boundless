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


def test_new_inbound_after_voice_wins_over_a_restaled_mirror_row():
    """电脑微信重扫把旧语音占位当新出站行入库，不许因此吞掉真来信。

    2026-09-20 实锤：12:58 发出的两条语音，屏上那颗「语音5″秒」占位直到
    13:21:28 才被扫进来，于是库里出现一条 ingested_at=13:21:28 的出站语音行；
    客户 13:21 的新来信同批入库。旧实现取「翻库」与「进程内登记」的 max，
    被污染的 13:21:28 赢了 13:20:47 的真实投递时刻 → in_ts 不大于 voice_ts
    → 13:21:45 已经写好的英文草稿在 13:22:09 被判成孤儿二稿，客户什么也没收到。

    进程内登记是一手事实（本进程真的发出去了），翻库只在没登记时兜底。
    """
    from src.inbox.voice_session_guard import clear_voice_marks_for_tests

    clear_voice_marks_for_tests()
    cid = "wechat:wechat-pc:wx:id:peer"
    delivered = 1000.0
    rows = [
        # 12:58 真发的语音（ack 镜像，时间准）
        {"direction": "out", "media_type": "voice", "ts": 400.0, "ingested_at": 400.0},
        # 同两条语音的屏上占位，重扫时才入库 —— 展示时间旧、入库时间新
        {"direction": "out", "media_type": "voice", "ts": 402.0, "ingested_at": 1041.0},
        # 客户的新来信：展示时间来自时间条（更早），入库时间才是真相
        {"direction": "in", "ts": 405.0, "ingested_at": 1041.0, "text": "还在吗"},
        # 13:20:47 真发的那条
        {"direction": "out", "media_type": "voice", "ts": delivered,
         "ingested_at": delivered, "text": "I am still here."},
    ]
    mark_voice_delivered(cid, now=delivered)
    assert should_quiet_after_voice(
        conv_key=cid, recent_messages=rows, quiet_after_sec=90, now=delivered + 82,
    ) == "", "客户在语音之后说了新话，这一轮必须发出去"

    # 反向：客户没再说话时守卫照旧生效，别把 2026-08-04 的孤儿二稿闸修没了
    clear_voice_marks_for_tests()
    mark_voice_delivered(cid, now=delivered)
    silent = [r for r in rows if float(r.get("ingested_at") or 0) <= delivered]
    assert should_quiet_after_voice(
        conv_key=cid, recent_messages=silent, quiet_after_sec=90, now=delivered + 10,
    ) == "voice_quiet"
    clear_voice_marks_for_tests()


def test_ingested_at_beats_a_stale_display_timestamp():
    """``ts`` 是展示时间，电脑微信取自气泡上方的时间条，可能比真实到达早几分钟。"""
    from src.inbox.voice_session_guard import clear_voice_marks_for_tests

    clear_voice_marks_for_tests()
    # 展示时间说来信在语音之前，入库时间说在之后 —— 以入库时间为准
    rows = [
        {"direction": "out", "media_type": "voice", "ts": 200.0, "ingested_at": 200.0},
        {"direction": "in", "ts": 150.0, "ingested_at": 230.0, "text": "在吗"},
    ]
    assert should_quiet_after_voice(
        recent_messages=rows, quiet_after_sec=90, now=250.0,
    ) == ""
    # 没有 ingested_at 的行（非本平台/老数据）照旧读 ts，向后兼容
    assert last_inbound_ts([{"direction": "in", "ts": 150.0}]) == 150.0
