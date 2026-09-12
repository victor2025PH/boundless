"""ASR 会话语种先验（ASR P0，2026-09-12）：``src/inbox/asr_lang_hint`` 纯函数 +
AutoDraft「消息行已有转写 → 免重转」+ 协议入站 lang_hint 透传契约。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.inbox.asr_lang_hint import (
    ASR_HINT_LANGS,
    conversation_asr_lang_hint,
    hint_from_history,
)


def _row(direction, text, media_type="", ts=0):
    return {"direction": direction, "text": text, "media_type": media_type, "ts": ts}


def test_hint_from_history_majority_of_text_rows():
    rows = [
        _row("in", "你好呀今天怎么样", ts=1),
        _row("out", "Hello!", ts=2),                       # 出站不算
        _row("in", "我明天有空", ts=3),
        _row("in", "[语音]", "voice", ts=4),                # 占位不算
        _row("in", "섬멸에서 섬미꼬야", "voice", ts=5),      # 语音行的转写不算（正是要复核的对象）
        _row("in", "[图片内容] 一只猫", ts=6),              # 系统识图文本不算
    ]
    assert hint_from_history(rows) == "zh"


def test_hint_requires_two_rows_and_majority():
    assert hint_from_history([_row("in", "你好呀今天怎么样")]) is None          # 单条太薄
    mixed = [_row("in", "你好呀今天怎么样", ts=1), _row("in", "hello there my friend", ts=2),
             _row("in", "我明天有空", ts=3), _row("in", "how are you doing today", ts=4)]
    assert hint_from_history(mixed) is None                                     # 50/50 无多数
    assert hint_from_history([]) is None and hint_from_history(None) is None


def test_hint_only_whisper_codes_and_family_fold():
    rows = [_row("in", "こんにちは、元気ですか", ts=1), _row("in", "今日はいい天気ですね", ts=2)]
    assert hint_from_history(rows) == "ja"
    assert "yue" in ASR_HINT_LANGS and "zh" in ASR_HINT_LANGS


def test_conversation_hint_prefers_explicit_outbound_lang():
    store = MagicMock()
    store.get_outbound_lang_if_set.return_value = "zh-TW"
    store.list_recent_messages.return_value = [
        _row("in", "hello there my friend", ts=1), _row("in", "how are you", ts=2)]
    assert conversation_asr_lang_hint(store, "c1") == "zh"      # 显式设置优先 + 家族折叠
    store.get_outbound_lang_if_set.return_value = "auto"        # auto=跟客户 → 看历史
    assert conversation_asr_lang_hint(store, "c1") == "en"
    store.get_outbound_lang_if_set.side_effect = RuntimeError("no col")
    assert conversation_asr_lang_hint(store, "c1") == "en"      # 取显式失败不影响历史路径
    assert conversation_asr_lang_hint(None, "c1") is None
    assert conversation_asr_lang_hint(store, "") is None


# ── AutoDraft：消息行已是转写 → 不再打 ASR ─────────────────────────────────
def test_autodraft_reuses_transcript_already_in_message_row():
    from tests.test_autodraft_media_backfill import _Store, _assistant
    from src.inbox.autodraft_helpers import enrich_auto_draft

    rows = [
        _row("in", "你好", ts=99) | {"media_ref": "", "message_id": "m0"},
        # 协议入站已把转写写进文本（不是 [语音] 占位）
        _row("in", "我吃过了呀 你吃了吗", "voice", ts=100) | {
            "media_ref": "/static/protocol_media/whatsapp/v.ogg", "message_id": "m1"},
    ]
    store = _Store(rows)
    draft_svc = MagicMock()
    draft_svc.get_draft.return_value = {}
    draft_svc.enrich_draft.return_value = True
    app = MagicMock()
    tc = MagicMock()
    vtr = MagicMock()
    vtr.transcribe_voice_message = AsyncMock(return_value="不该被调到")
    tc.voice_transcriber = vtr
    app.state.telegram_client = tc
    captured = {}

    async def _fake_gen(**kw):
        captured.update(kw)
        return {"ok": True, "reply": "好", "reply_lang": "zh"}

    conv = {"conversation_id": "whatsapp:acc:peer", "platform": "whatsapp",
            "chat_key": "peer", "account_id": "acc"}
    with patch("src.inbox.persona_reply.generate_persona_reply", _fake_gen), \
         patch("src.integrations.protocol_bridge.static_media_ref_to_path",
               return_value="C:/tmp/fake.ogg"), \
         patch("src.inbox.media_enrich.enrich_inbound_media_text",
               AsyncMock(return_value=("", ""))):
        asyncio.run(enrich_auto_draft(
            _assistant(None), draft_svc, app, store, conv, "hi", "d1", "review"))
    assert vtr.transcribe_voice_message.await_count == 0, "消息行已有转写，不得再打 ASR"
    assert captured["last_inbound"] == "我吃过了呀 你吃了吗"
    assert captured["media_desc"] == "我吃过了呀 你吃了吗"
    assert captured["media_type"] == "voice"


def test_autodraft_still_transcribes_bare_placeholder_and_passes_hint():
    from tests.test_autodraft_media_backfill import _Store, _assistant
    from src.inbox.autodraft_helpers import enrich_auto_draft

    rows = [
        _row("in", "你好呀今天怎么样", ts=98) | {"media_ref": "", "message_id": "m0"},
        _row("in", "我明天有空", ts=99) | {"media_ref": "", "message_id": "m0b"},
        _row("in", "", "voice", ts=100) | {
            "media_ref": "/static/protocol_media/whatsapp/v.ogg", "message_id": "m1"},
    ]
    store = _Store(rows)
    draft_svc = MagicMock()
    draft_svc.get_draft.return_value = {}
    draft_svc.enrich_draft.return_value = True
    app = MagicMock()
    tc = MagicMock()
    vtr = MagicMock()
    vtr.transcribe_voice_message = AsyncMock(return_value="明天有空吗")
    tc.voice_transcriber = vtr
    app.state.telegram_client = tc
    captured = {}

    async def _fake_gen(**kw):
        captured.update(kw)
        return {"ok": True, "reply": "好", "reply_lang": "zh"}

    conv = {"conversation_id": "whatsapp:acc:peer", "platform": "whatsapp",
            "chat_key": "peer", "account_id": "acc"}
    with patch("src.inbox.persona_reply.generate_persona_reply", _fake_gen), \
         patch("src.integrations.protocol_bridge.static_media_ref_to_path",
               return_value="C:/tmp/fake.ogg"), \
         patch("src.inbox.media_enrich.enrich_inbound_media_text",
               AsyncMock(return_value=("", ""))):
        asyncio.run(enrich_auto_draft(
            _assistant(None), draft_svc, app, store, conv, "hi", "d1", "review"))
    assert vtr.transcribe_voice_message.await_count == 1
    _args, kwargs = vtr.transcribe_voice_message.await_args
    assert kwargs.get("lang_hint") == "zh"      # 两条中文文字消息 → 先验 zh
    assert captured["last_inbound"] == "明天有空吗"


# ── 协议入站：lang_hint 透传 + 旧签名回退 ──────────────────────────────────
def test_ingest_guarded_passes_hint_and_falls_back_on_old_signature():
    from src.web.routes import unified_inbox_account_routes as r

    class _New:
        def __init__(self):
            self.calls = []

        async def transcribe_voice_message(self, path, lang, *, lang_hint=None):
            self.calls.append((path, lang, lang_hint))
            return "ok"

    class _Old:
        def __init__(self):
            self.calls = []

        async def transcribe_voice_message(self, path, lang):
            self.calls.append((path, lang))
            return "old"

    n, o = _New(), _Old()
    assert asyncio.run(r._transcribe_ingest_guarded(n, "p.ogg", "auto", None, lang_hint="zh")) == "ok"
    assert n.calls == [("p.ogg", "auto", "zh")]
    assert asyncio.run(r._transcribe_ingest_guarded(o, "p.ogg", "auto", None, lang_hint="zh")) == "old"
    assert o.calls == [("p.ogg", "auto")]
    assert asyncio.run(r._transcribe_ingest_guarded(n, "p.ogg", "auto", None)) == "ok"
    assert n.calls[-1] == ("p.ogg", "auto", None)
