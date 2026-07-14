"""Phase4：TG B 线入站 emoji/sticker 语义 + WA reaction 轻量跟进门禁。"""
from __future__ import annotations

import time
import types
from pathlib import Path

from src.companion.reaction_followup import (
    build_followup_text,
    is_positive_reaction,
    parse_reaction_followup_cfg,
    schedule_reaction_followup,
    should_schedule_reaction_followup,
)
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb
from src.integrations.protocol_bridge import ingest_incoming
from src.integrations.shared.deferred_outbox import DeferredOutboxStore
from src.integrations.tg_inbound_text import (
    annotate_inbound_emoji,
    demojize_one,
    sticker_text_from_message,
)


class _Sticker:
    def __init__(self, emoji: str = "") -> None:
        self.emoji = emoji


class _FakeMsg:
    def __init__(self, *, text: str = "", sticker=None) -> None:
        self.text = text
        self.caption = None
        self.sticker = sticker
        self.chat = types.SimpleNamespace(id=12345)
        self.date = types.SimpleNamespace(timestamp=lambda: 100.0)
        self.id = 99
        self.outgoing = False


def test_demojize_one_laugh():
    out = demojize_one("😂")
    assert out and out != "😂"
    assert "笑" in out or "joy" in out.lower() or len(out) > 1


def test_annotate_pure_emoji():
    t = annotate_inbound_emoji("😂😂")
    assert t.startswith("[表情]")


def test_annotate_mixed_emoji():
    t = annotate_inbound_emoji("好的👍")
    assert "好的👍" in t
    assert "表情" in t


def test_sticker_text_from_message():
    msg = _FakeMsg(sticker=_Sticker("😂"))
    t = sticker_text_from_message(msg)
    assert t.startswith("[表情]")


def test_tg_message_payload_sticker_enriched():
    msg = _FakeMsg(sticker=_Sticker("👍"))
    payload = pb.tg_message_payload(msg, "acc1", media_type="sticker")
    assert payload is not None
    assert payload["text"].startswith("[表情]")


def test_tg_message_payload_text_emoji_enriched():
    msg = _FakeMsg(text="嗨😊")
    payload = pb.tg_message_payload(msg, "acc1")
    assert payload is not None
    assert "嗨" in payload["text"]
    assert "表情" in payload["text"]


def test_is_positive_reaction():
    pos = frozenset({"❤️", "👍", "😂"})
    assert is_positive_reaction("❤️", positive_set=pos)
    assert is_positive_reaction("👍🏻", positive_set=pos)
    assert not is_positive_reaction("👎", positive_set=pos)
    assert not is_positive_reaction("", positive_set=pos)


def test_should_schedule_skips_self_and_inbound():
    cfg = parse_reaction_followup_cfg({"reaction_followup": {"enabled": True}})
    assert should_schedule_reaction_followup(
        sender="me", emoji="❤️", direction="out", platform="whatsapp",
        chat_type="", cfg=cfg, cooldown_ts=0, now=1000.0,
    ) == "self_reaction"
    assert should_schedule_reaction_followup(
        sender="639111", emoji="❤️", direction="in", platform="whatsapp",
        chat_type="", cfg=cfg, cooldown_ts=0, now=1000.0,
    ) == "not_our_message"


def test_build_followup_text_deterministic():
    a = build_followup_text("❤️", lang="zh", seed="wa:1:123")
    b = build_followup_text("❤️", lang="zh", seed="wa:1:123")
    assert a == b
    assert a


def test_schedule_reaction_followup_enqueues(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="whatsapp", account_id="wa1", chat_key="639111",
        text="hello from us", ts=50, msg_id="MID1", direction="out",
    )
    outbox = DeferredOutboxStore(tmp_path / "deferred.db")
    dispatcher = types.SimpleNamespace(_store=outbox)
    config = {
        "companion": {
            "reaction_followup": {"enabled": True, "platforms": ["whatsapp"]},
        },
    }
    row_id = schedule_reaction_followup(
        store=store,
        deferred_dispatcher=dispatcher,
        config=config,
        config_dir=tmp_path,
        platform="whatsapp",
        account_id="wa1",
        chat_key="639111",
        target_id="MID1",
        emoji="❤️",
        sender="639111",
        now=1000.0,
    )
    assert row_id > 0
    due = outbox.drain_due(now=2000.0, limit=5)
    assert len(due) == 1
    assert "reaction_followup" in due[0]["reason"]
    assert cid == "whatsapp:wa1:639111"


def test_schedule_reaction_followup_cooldown(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ingest_incoming(
        store, platform="whatsapp", account_id="wa1", chat_key="639222",
        text="hi", ts=50, msg_id="M2", direction="out",
    )
    outbox = DeferredOutboxStore(tmp_path / "deferred.db")
    dispatcher = types.SimpleNamespace(_store=outbox)
    config = {
        "companion": {
            "reaction_followup": {
                "enabled": True, "cooldown_hours": 6, "platforms": ["whatsapp"],
            },
        },
    }
    kw = dict(
        store=store, deferred_dispatcher=dispatcher, config=config,
        config_dir=tmp_path, platform="whatsapp", account_id="wa1",
        chat_key="639222", target_id="M2", emoji="👍", sender="639222",
    )
    assert schedule_reaction_followup(**kw, now=1000.0) > 0
    assert schedule_reaction_followup(**kw, now=2000.0) == 0
