"""WhatsApp 号码闭环：模拟设备，不连真机，不对外发送。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.reply_history import load_reply_history
from src.inbox.store import InboxStore
from src.integrations.whatsapp_rpa.phone_identity import (
    conversation_id,
    extract_phone,
    header_matches,
    normalize_e164,
    view_intent_args,
    wa_me_url,
)
from src.integrations.whatsapp_rpa.phone_ledger import PhoneLedger, resolve_caps
from src.integrations.whatsapp_rpa.phone_loop import (
    WaPhoneLoop,
    client_msg_id,
    phone_loop_enabled,
    register_phone_loop,
    route_registered_send,
    text_is_stop,
    unregister_phone_loop,
)
from src.integrations.whatsapp_rpa.phone_notify import parse_wa_notifications
from src.integrations.whatsapp_rpa.phone_send import (
    PhoneScreen,
    bubble_visible,
    composer_matches,
)
from src.integrations.whatsapp_rpa.service import WhatsAppRpaService

PHONE = "+14155552671"
ACCOUNT = "seat-a"


class FakeDevice:
    def __init__(self, screen: PhoneScreen, after: Optional[PhoneScreen] = None):
        self.screen = screen
        self.after = after
        self.urls: list[str] = []
        self.taps = 0
        self.backs = 0
        self.notifications: list = []

    async def open_url(self, url: str) -> None:
        self.urls.append(url)

    async def screencap(self) -> bytes:
        return b"png"

    async def tap_send(self) -> None:
        self.taps += 1
        if self.after is not None:
            self.screen = self.after

    async def read_screen(self, png: bytes) -> PhoneScreen:
        return self.screen

    async def press_back(self) -> None:
        self.backs += 1

    async def poll_notifications(self):
        return list(self.notifications)


def _loop(tmp_path: Path, device: FakeDevice, **cfg) -> WaPhoneLoop:
    store = InboxStore(tmp_path / "inbox.db")
    ledger = PhoneLedger(tmp_path / "ledger.db")
    calls = []

    async def decide(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "reply": cfg.pop("reply", "你好呀")}

    loop = WaPhoneLoop(
        account_id=ACCOUNT,
        inbox=store,
        device=device,
        decide_fn=decide,
        cfg={"enabled": True, **cfg},
        ledger=ledger,
        now_fn=lambda: 1_700_000_000.0,
    )
    loop.decide_calls = calls  # type: ignore[attr-defined]
    loop.inbox_path = store
    return loop


def _ready(text: str, header: str = PHONE) -> FakeDevice:
    return FakeDevice(
        PhoneScreen(header=header, composer=text),
        PhoneScreen(header=header, outgoing=[text]),
    )


def test_e164_key_and_wa_me_keeps_chinese():
    assert normalize_e164("你好") == ""
    assert normalize_e164("+1 (415) 555-2671") == PHONE
    assert conversation_id(ACCOUNT, PHONE) == f"whatsapp:{ACCOUNT}:{PHONE}"
    url = wa_me_url(PHONE, "你好呀")
    assert url.startswith("https://wa.me/14155552671?text=")
    assert "input" not in url
    assert "你好" in __import__("urllib.parse").parse.unquote(url)
    args = view_intent_args(url)
    assert "uiautomator" not in " ".join(args)
    assert "input" not in args


@pytest.mark.asyncio
async def test_decide_whatsapp_uses_inbox_history(tmp_path: Path, monkeypatch):
    from src.integrations.whatsapp_rpa.phone_loop import decide_whatsapp

    store = InboxStore(tmp_path / "inbox.db")
    cid = conversation_id(ACCOUNT, PHONE)
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="whatsapp", account_id=ACCOUNT,
        chat_key=PHONE, phone=PHONE,
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="in", text="第一句", ts=1, platform_msg_id="a",
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="in", text="第二句", ts=2, platform_msg_id="b",
    ))
    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "reply": "接着说"}

    monkeypatch.setattr("src.inbox.persona_reply.generate_persona_reply", _fake)
    app = SimpleNamespace(state=SimpleNamespace(inbox_store=store))
    await decide_whatsapp(
        app, account_id=ACCOUNT, phone=PHONE, last_inbound="第二句",
    )
    assert captured["platform"] == "whatsapp"
    assert captured["chat_key"] == PHONE
    assert captured["conversation_id"] == cid
    assert captured["history"] == [{"role": "user", "content": "第一句"}]


def test_history_drops_only_the_current_inbound(tmp_path: Path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = conversation_id(ACCOUNT, PHONE)
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="whatsapp", account_id=ACCOUNT,
        chat_key=PHONE, phone=PHONE,
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="in", text="第一句", ts=1,
        platform_msg_id="m1",
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="out", text="回第一句", ts=2,
        platform_msg_id="m2",
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="in", text="第二句", ts=3,
        platform_msg_id="m3",
    ))
    history = load_reply_history(store, cid, "第二句")
    assert history == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回第一句"},
    ]
    assert load_reply_history(None, cid, "第二句") == []


def test_caps_reject_zero_as_unlimited():
    assert resolve_caps({}) == {"hourly_cap": 20, "daily_cap": 80}
    assert resolve_caps({"hourly_cap": 0, "daily_cap": -3}) == {
        "hourly_cap": 20, "daily_cap": 80,
    }
    assert resolve_caps({"hourly_cap": 2, "daily_cap": 5}) == {
        "hourly_cap": 2, "daily_cap": 5,
    }


def test_phone_loop_defaults_stay_off():
    assert phone_loop_enabled({}) is False
    assert phone_loop_enabled({"phone_loop": {"enabled": False}}) is False
    bare = WhatsAppRpaService.__new__(WhatsAppRpaService)
    defaults = WhatsAppRpaService._defaults(bare)
    assert defaults["enabled"] is False
    assert defaults["daily_cap"] == 0


@pytest.mark.asyncio
async def test_reply_writes_inbox_and_sends_once(tmp_path: Path):
    device = _ready("你好呀")
    loop = _loop(tmp_path, device)
    result = await loop.handle_inbound(phone=PHONE, text="在吗", display_name="Ann")
    assert result["conversation_id"] == conversation_id(ACCOUNT, PHONE)
    assert result["send"]["status"] == "sent"
    assert device.taps == 1
    assert "text=" in device.urls[-1]
    rows = loop.inbox.list_recent_messages(result["conversation_id"])
    assert [row["direction"] for row in rows] == ["in", "out"]
    assert rows[1]["text"] == "你好呀"
    assert loop.decide_calls[0]["phone"] == PHONE
    src = Path("src/integrations/whatsapp_rpa/phone_loop.py").read_text(encoding="utf-8")
    assert ".process_message" not in src


@pytest.mark.asyncio
async def test_composer_mismatch_fails_without_tap_or_retry(tmp_path: Path):
    device = FakeDevice(PhoneScreen(header=PHONE, composer="不是这句"))
    loop = _loop(tmp_path, device, reply="你好呀")
    cid = client_msg_id("reply", PHONE, "你好呀", "在吗")
    first = await loop.send_prepared(
        phone=PHONE, text="你好呀", source="reply", client_msg_id=cid,
    )
    second = await loop.send_prepared(
        phone=PHONE, text="你好呀", source="reply", client_msg_id=cid,
    )
    assert first["status"] == "failed"
    assert first["reason"] == "composer_mismatch"
    assert second["reason"] == "duplicate"
    assert device.taps == 0
    assert composer_matches("你好呀", "你好呀")
    assert not composer_matches("你好呀。", "你好呀")


@pytest.mark.asyncio
async def test_missing_bubble_is_failed_and_not_retried(tmp_path: Path):
    device = FakeDevice(
        PhoneScreen(header=PHONE, composer="你好呀"),
        PhoneScreen(header=PHONE, outgoing=["另一句"]),
    )
    loop = _loop(tmp_path, device)
    cid = client_msg_id("reply", PHONE, "你好呀", "x")
    first = await loop.send_prepared(
        phone=PHONE, text="你好呀", source="reply", client_msg_id=cid,
    )
    await loop.send_prepared(
        phone=PHONE, text="你好呀", source="reply", client_msg_id=cid,
    )
    assert first["status"] == "failed"
    assert first["reason"] == "bubble_missing"
    assert device.taps == 1
    assert not bubble_visible(["另一句"], "你好呀")


@pytest.mark.asyncio
async def test_dry_run_is_not_sent(tmp_path: Path):
    device = _ready("你好呀")
    loop = _loop(tmp_path, device, dry_run=True)
    result = await loop.send_prepared(
        phone=PHONE, text="你好呀", source="reply",
        client_msg_id=client_msg_id("reply", PHONE, "你好呀", "dry"),
    )
    assert result["status"] == "dry_run"
    assert result["delivered"] is False
    assert device.taps == 0
    assert device.urls == []
    cid = conversation_id(ACCOUNT, PHONE)
    assert loop.inbox.list_recent_messages(cid) == []


@pytest.mark.asyncio
async def test_stop_persists_and_blocks_every_exit(tmp_path: Path):
    device = _ready("你好呀")
    loop = _loop(tmp_path, device)
    register_phone_loop(loop)
    try:
        stopped = await loop.handle_inbound(phone=PHONE, text="不要再发")
        assert stopped["stopped"] is True
        assert loop.decide_calls == []
        assert device.taps == 0
        again = await loop.handle_inbound(phone=PHONE, text="在吗")
        assert again["stopped"] is True
        assert loop.decide_calls == []
        care = await route_registered_send(ACCOUNT, PHONE, "最近还好吗", source="care")
        assert care["status"] == "stopped"
        touch = loop.enqueue_first_touch(PHONE, "嗨")
        assert touch["ok"] is True
        drained = await loop.drain_first_touch()
        assert drained[0]["status"] == "stopped"
        assert device.taps == 0
    finally:
        unregister_phone_loop(ACCOUNT)
    assert text_is_stop("有点累") is False
    assert text_is_stop("stop") is True


@pytest.mark.asyncio
async def test_hourly_cap_blocks_without_tapping(tmp_path: Path):
    device = _ready("甲")
    loop = _loop(tmp_path, device, hourly_cap=1, daily_cap=80, reply="甲")
    ok = await loop.send_prepared(
        phone=PHONE, text="甲", source="reply",
        client_msg_id=client_msg_id("reply", PHONE, "甲", "1"),
    )
    device.screen = PhoneScreen(header=PHONE, composer="乙")
    device.after = PhoneScreen(header=PHONE, outgoing=["乙"])
    blocked = await loop.send_prepared(
        phone=PHONE, text="乙", source="reply",
        client_msg_id=client_msg_id("reply", PHONE, "乙", "2"),
    )
    assert ok["status"] == "sent"
    assert blocked["status"] == "capped"
    assert blocked["retry"] is True
    assert device.taps == 1


@pytest.mark.asyncio
async def test_first_touch_once_from_queue_and_outreach(tmp_path: Path):
    opener = "嗨，我是小界"
    device = _ready(opener)
    loop = _loop(tmp_path, device, first_touch_enabled=True)
    assert loop.enqueue_first_touch(PHONE, opener)["ok"] is True
    first = await loop.drain_first_touch()
    second = await loop.drain_first_touch()
    assert first[0]["status"] == "sent"
    assert second == []
    assert device.taps == 1
    again = loop.enqueue_first_touch(PHONE, opener)
    assert again["reason"] == "already_touched"

    other = "+14155550000"
    cid = conversation_id(ACCOUNT, other)
    loop.inbox.record_outreach(
        cid, batch_id="plan", platform="whatsapp", account_id=ACCOUNT,
        status="planned", note="另一句招呼", ts=1_700_000_000.0,
    )
    device.screen = PhoneScreen(header=other, composer="另一句招呼")
    device.after = PhoneScreen(header=other, outgoing=["另一句招呼"])
    drained = await loop.drain_first_touch()
    assert drained[0]["status"] == "sent"
    assert drained[0]["phone"] == other
    logged = loop.inbox.list_outreach_recent(days=30, limit=20, now=1_700_000_100.0)
    assert any(row["status"] == "sent" and row["conversation_id"] == conversation_id(ACCOUNT, other)
               for row in logged)


@pytest.mark.asyncio
async def test_notification_header_must_match_before_ingest(tmp_path: Path):
    device = FakeDevice(PhoneScreen(header="别人", composer=""))
    loop = _loop(tmp_path, device)
    missed = await loop.ingest_notification({
        "sender": "Ann", "text": "在吗", "phone": PHONE,
    })
    assert missed["reason"] == "header_mismatch"
    assert device.taps == 0
    assert loop.inbox.list_conversations(platform="whatsapp", account_id=ACCOUNT) == []

    device.screen = PhoneScreen(header="Ann", composer="你好呀")
    device.after = PhoneScreen(header="Ann", outgoing=["你好呀"])
    hit = await loop.ingest_notification({
        "sender": "Ann", "text": "在吗", "phone": PHONE,
    })
    assert hit["send"]["status"] == "sent"
    assert header_matches("Ann", phone=PHONE, sender="Ann")
    assert not header_matches("Anna", phone=PHONE, sender="Ann")
    assert extract_phone("请加 14155552671") == PHONE


@pytest.mark.asyncio
async def test_unresolved_name_does_not_open_chat(tmp_path: Path):
    device = _ready("你好呀")
    loop = _loop(tmp_path, device)
    result = await loop.ingest_notification({"sender": "同名甲", "text": "在吗"})
    assert result["reason"] == "phone_unresolved"
    assert device.urls == []


@pytest.mark.asyncio
async def test_care_and_wake_use_the_same_sender(tmp_path: Path):
    device = _ready("最近还好吗")
    loop = _loop(tmp_path, device, wake_enabled=True, wake_after_sec=60, wake_text="最近还好吗")
    register_phone_loop(loop)
    try:
        care = await route_registered_send(ACCOUNT, PHONE, "最近还好吗", source="care")
        assert care["status"] == "sent"
        assert care["delivered"] is True
        cid = conversation_id(ACCOUNT, PHONE)
        loop.inbox.upsert_conversation(InboxConversation(
            conversation_id=cid, platform="whatsapp", account_id=ACCOUNT,
            chat_key=PHONE, phone=PHONE, display_name="Ann", last_ts=1,
        ))
        loop._now = lambda: 1_700_000_000.0 + 180  # type: ignore[method-assign]
        device.screen = PhoneScreen(header=PHONE, composer="最近还好吗")
        device.after = PhoneScreen(header=PHONE, outgoing=["最近还好吗"])
        woke = await loop.wake_silent()
        assert woke[0]["status"] == "sent"
        assert device.taps == 2
        assert all("wa.me/14155552671" in url for url in device.urls)
    finally:
        unregister_phone_loop(ACCOUNT)
    assert await route_registered_send(ACCOUNT, PHONE, "最近还好吗") is None


@pytest.mark.asyncio
async def test_conversion_manual_and_keyword_write_outreach(tmp_path: Path):
    device = _ready("收到")
    loop = _loop(tmp_path, device, reply_mode="off")
    inbound = await loop.handle_inbound(phone=PHONE, text="我下单了")
    assert inbound["deal"]["ok"] is True
    assert inbound["deal"]["stage"] == "deal"
    manual = loop.mark_deal(PHONE, source="manual", note="坐席标记", amount=12, by="sarah")
    assert manual["ok"] is True
    logged = loop.inbox.list_outreach_recent(days=30, limit=20, now=1_700_000_100.0)
    assert {row["status"] for row in logged} >= {"converted"}
    assert device.taps == 0


def test_notification_parser_reads_sender_without_dump():
    dump = """
NotificationRecord(0x1) pkg=com.whatsapp
  android.title=String (Ann)
  android.text=String (在吗)
"""
    assert parse_wa_notifications(dump) == [{"sender": "Ann", "text": "在吗"}]


def test_ocr_helper_fails_closed_without_png():
    from src.integrations.whatsapp_rpa.phone_send import ocr_phone_screen
    assert ocr_phone_screen(b"") == PhoneScreen()
    assert ocr_phone_screen(b"not a png") == PhoneScreen()


def test_module_does_not_use_dump_or_input_text():
    root = Path("src/integrations/whatsapp_rpa")
    for name in ("phone_loop.py", "phone_send.py", "phone_identity.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "uiautomator" not in text
        assert "input text" not in text
        assert ".process_message" not in text
