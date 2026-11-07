"""真机验收脚本的模拟测试。不连 ssh，不点真机，不对外发送。"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from src.inbox.store import InboxStore
from src.integrations.whatsapp_rpa.phone_acceptance import (
    EvidenceLog,
    SshAdbPhoneDevice,
    cold_texts,
    compare_prefill,
    lines_to_screen,
    make_http_decide,
    make_ppocr_reader,
    new_left_bubbles,
    png_size,
    redact_text,
    run_convert_pending,
    run_dry,
    run_first_touch_cap,
    run_pair_chat,
    run_rate_limit_check,
    run_stop_test,
    safe_log_fields,
    ssh_adb_argv,
    variant_phone,
)
from src.integrations.whatsapp_rpa.phone_send import composer_matches
from src.integrations.whatsapp_rpa.phone_identity import header_matches, header_needs_profile
from src.integrations.whatsapp_rpa.phone_ledger import PhoneLedger
from src.integrations.whatsapp_rpa.phone_loop import WaPhoneLoop
from src.integrations.whatsapp_rpa.phone_send import PhoneScreen

PHONE_A = "+14155552671"
PHONE_B = "+14155550000"


def _png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class LinkPhone:
    def __init__(self, name: str, own_phone: str, bus: dict):
        self.name = name
        self.own_phone = own_phone
        self.bus = bus
        self.taps = 0
        self.urls = []
        self.screen = PhoneScreen()
        self.force_composer = None
        self.rate = False

    async def open_url(self, url: str) -> None:
        self.urls.append(url)
        parsed = urlparse(url)
        digits = parsed.path.strip("/").split("/")[-1]
        text = unquote(parse_qs(parsed.query).get("text", [""])[0])
        self._bubble = text
        header = digits[:8] + "…"
        if self.rate:
            composer = (text or "你好") + "\n发送过于频繁，请稍后再试"
        elif self.force_composer is not None:
            composer = self.force_composer
        else:
            composer = text
        self.screen = PhoneScreen(header=header, composer=composer)

    async def screencap(self) -> bytes:
        return _png(100, 100)

    async def read_screen(self, png: bytes) -> PhoneScreen:
        return self.screen

    async def tap_send(self) -> None:
        self.taps += 1
        other = "b" if self.name == "a" else "a"
        self.bus.setdefault(other, []).append({
            "sender": self.own_phone, "phone": self.own_phone, "text": self._bubble,
        })
        self.screen = PhoneScreen(header=self.screen.header, outgoing=[self._bubble], composer="")

    async def press_back(self) -> None:
        return None

    async def poll_notifications(self):
        items = list(self.bus.get(self.name, []))
        self.bus[self.name] = []
        return items


def _loop(tmp_path: Path, device, account: str, decide_reply: str = "收到，继续"):
    store = InboxStore(tmp_path / f"inbox-{account}.db")
    ledger = PhoneLedger(tmp_path / f"ledger-{account}.db")

    async def decide(*, account_id, phone, last_inbound, persona_id=""):
        return {"ok": True, "reply": decide_reply}

    loop = WaPhoneLoop(
        account_id=account, inbox=store, device=device, decide_fn=decide,
        cfg={"enabled": True, "hourly_cap": 2, "daily_cap": 80},
        ledger=ledger, now_fn=lambda: 1_700_000_000.0,
    )
    return loop


REAL_HEADER = "凌晨5:46 100 +63 993 355 6... □ 今天"
REAL_PHONE = "+639933556049"


def test_truncated_header_matches_prefix_or_suffix_only():
    assert header_matches(REAL_HEADER, phone=REAL_PHONE)
    assert not header_needs_profile(REAL_HEADER, phone=REAL_PHONE)
    assert header_matches("+63 993 355 6049", phone=REAL_PHONE)
    assert header_matches("14155552…", phone=PHONE_A)
    assert not header_matches("141555…", phone=PHONE_A)
    assert header_needs_profile("141555…", phone=PHONE_A)
    assert header_needs_profile("+63 99…", phone=REAL_PHONE)
    assert not header_matches("…2671", phone=PHONE_A)
    assert not header_needs_profile("…2671", phone=PHONE_A)
    assert not header_matches("5552", phone=PHONE_A)
    assert not header_matches("415555", phone=PHONE_A)
    assert not header_matches("凌晨5:46 100", phone=REAL_PHONE)
    assert header_matches("Ann", phone=PHONE_A, sender="Ann")


def test_ssh_adb_never_uses_local_adb_or_server_commands():
    argv = ssh_adb_argv("hub176", "SERIAL_A", ["exec-out", "screencap", "-p"])
    assert argv[0] == "ssh"
    assert "hub176" in argv
    assert argv[-1].startswith("adb -s SERIAL_A")
    assert "kill-server" not in argv[-1]
    for blocked in (["kill-server"], ["start-server"], ["connect", "1.2.3.4:5555"]):
        with pytest.raises(ValueError):
            ssh_adb_argv("hub176", "SERIAL_A", blocked)


def test_prefill_requires_the_chinese_text_not_the_placeholder():
    assert compare_prefill("你好，这是验收", "你好，这是验收")["match"] is True
    placeholder = compare_prefill("你好，这是验收", "输入消息")
    assert placeholder["match"] is False
    assert placeholder["placeholder_only"] is True
    assert compare_prefill("你好，这是验收", "你好，这是验收\n输入消息")["match"] is True
    seed = "你好，我在做聊天链路验收"
    wrapped = "你好，我在做聊天链路\n验收\n-"
    assert composer_matches(wrapped, seed)
    assert compare_prefill(seed, wrapped)["match"] is True
    mixed = wrapped + "\n04:03 √ 你好呀，这么晚还醒着。04:08 发消息 回"
    assert composer_matches(mixed, seed)
    assert not composer_matches("你好，我在做聊天链路验收-", "你好，我在做聊天链路验收。")


def test_ppocr_boxes_split_header_composer_and_bubbles():
    png = _png(100, 200)
    assert png_size(png) == (100, 200)
    screen = lines_to_screen([
        {"text": "141555…", "box": [10, 4, 40, 16]},
        {"text": "在吗", "box": [4, 80, 30, 96]},
        {"text": "你好", "box": [60, 100, 90, 116]},
        {"text": "你好，这是验收", "box": [8, 180, 90, 196]},
    ], 100, 200)
    assert "141555" in screen.header
    assert screen.composer == "你好，这是验收"
    assert screen.last_incoming == "在吗"
    assert screen.outgoing == ["你好"]
    seen = {}

    def post(url, payload, timeout=20.0, headers=None):
        seen["url"] = url
        assert "image_b64" in payload
        assert headers is None or "Authorization" not in headers
        return {"ok": True, "lines": [
            {"text": "你好，这是验收", "box": [8, 180, 90, 196]},
        ]}

    reader = make_ppocr_reader("http://192.168.0.176:8766", post_json=post)
    parsed = reader(png)
    assert seen["url"] == "http://192.168.0.176:8766/v1/ocr"
    assert parsed.composer == "你好，这是验收"


def test_real_ocr_header_and_bubble_sides():
    """02→03 干跑的顶栏原文，加上会把时间和己方历史认错的框。"""
    width, height = 1080, 2400
    screen = lines_to_screen([
        {"text": "凌晨5:46", "box": [48, 28, 220, 84]},
        {"text": "100", "box": [900, 28, 1040, 84]},
        {"text": "+63 993 355 6...", "box": [160, 150, 780, 230]},
        {"text": "□", "box": [800, 160, 860, 220]},
        {"text": "今天", "box": [470, 250, 610, 320]},
        {"text": "17:02", "box": [760, 500, 900, 560]},
        {"text": "昨天", "box": [460, 600, 620, 660]},
        {"text": "在吗", "box": [36, 700, 280, 820]},
        {"text": "上一句验收", "box": [160, 880, 1000, 1020]},
        {"text": "你好，这是验收", "box": [80, 2100, 980, 2280]},
    ], width, height)
    assert screen.header == REAL_HEADER
    assert header_matches(screen.header, phone=REAL_PHONE)
    assert screen.composer == "你好，这是验收"
    assert screen.last_incoming == "在吗"
    assert screen.outgoing == ["上一句验收"]
    assert "17:02" not in screen.outgoing
    assert "昨天" not in screen.outgoing
    assert screen.last_incoming != "上一句验收"
    assert "17:02" in screen.notes
    assert "昨天" in screen.notes


def test_decide_log_does_not_keep_the_token():
    secret = "super-secret-token"
    assert secret not in redact_text(f"Authorization: Bearer {secret}", [secret])
    logged = safe_log_fields(
        {"event": "decide", "authorization": f"Bearer {secret}", "action": "draft"},
        [secret],
    )
    assert "authorization" not in logged
    assert secret not in json.dumps(logged)

    captured = {}

    def post(url, body, timeout=60.0, headers=None):
        captured["headers"] = dict(headers or {})
        captured["body"] = body
        return {"action": "draft", "text": "你好呀"}

    decide = make_http_decide("http://127.0.0.1:18799/api/replybus/decide", token=secret, post_json=post)

    async def _call():
        return await decide(account_id="seat-a", phone=PHONE_A, last_inbound="在吗")

    import asyncio
    result = asyncio.run(_call())
    assert result["reply"] == "你好呀"
    assert captured["headers"]["Authorization"] == f"Bearer {secret}"
    assert captured["body"]["message"]["platform"] == "whatsapp"
    assert secret not in json.dumps(safe_log_fields({"url": captured.get("url", "")}, [secret]))


@pytest.mark.asyncio
async def test_dry_run_checks_chinese_and_does_not_tap(tmp_path: Path):
    bus = {}
    device = LinkPhone("a", PHONE_A, bus)
    evidence = EvidenceLog(tmp_path / "evidence")
    loop = _loop(tmp_path, device, "seat-a")
    report = await run_dry(loop, device, PHONE_B, "你好，这是验收", evidence)
    assert report["ok"] is True
    assert report["status"] == "dry_run"
    assert device.taps == 0
    assert "wa.me/14155550000" in device.urls[0]
    saved = json.loads((tmp_path / "evidence" / "report.json").read_text(encoding="utf-8"))
    assert saved["match"] is True
    assert saved["expected"] == "你好，这是验收"
    assert any(path.name.endswith(".ocr.json") for path in (tmp_path / "evidence").iterdir())

    device.force_composer = "输入消息"
    device.open_timeout = 0
    device.open_interval = 0
    missed = await run_dry(loop, device, PHONE_B, "你好，这是验收", EvidenceLog(tmp_path / "bad"))
    assert missed["ok"] is False
    assert missed["placeholder_only"] is True
    assert device.taps == 0


@pytest.mark.asyncio
async def test_pair_chat_two_phones_exchange(tmp_path: Path):
    bus = {}
    device_a = LinkPhone("a", PHONE_A, bus)
    device_b = LinkPhone("b", PHONE_B, bus)
    store = InboxStore(tmp_path / "inbox.db")

    async def decide(*, account_id, phone, last_inbound, persona_id=""):
        return {"ok": True, "reply": "收到，" + last_inbound[:4]}

    def make(name, account, device):
        return WaPhoneLoop(
            account_id=account, inbox=store, device=device, decide_fn=decide,
            cfg={"enabled": True, "hourly_cap": 20, "daily_cap": 80},
            ledger=PhoneLedger(tmp_path / f"{name}.db"),
            now_fn=lambda: 1_700_000_000.0,
        )

    report = await run_pair_chat(
        make("a", "seat-a", device_a), make("b", "seat-b", device_b),
        PHONE_A, PHONE_B, 2, "你好，我在做链路验收", EvidenceLog(tmp_path / "pair"),
    )
    assert report["ok"] is True
    assert [row["status"] for row in report["turns"]] == ["sent", "sent"]
    assert device_a.taps == 1
    assert device_b.taps == 1
    assert report["turns"][1]["source"] == "notification"


@pytest.mark.asyncio
async def test_pair_chat_stops_when_the_first_send_fails(tmp_path: Path):
    bus = {}
    device_a = LinkPhone("a", PHONE_A, bus)
    device_b = LinkPhone("b", PHONE_B, bus)
    device_a.force_composer = "不是这句"
    device_a.open_timeout = 0
    store = InboxStore(tmp_path / "inbox.db")

    async def decide(*, account_id, phone, last_inbound, persona_id=""):
        raise AssertionError("首轮没发出去，不应再生成回复")

    def make(name, account, device):
        return WaPhoneLoop(
            account_id=account, inbox=store, device=device, decide_fn=decide,
            cfg={"enabled": True, "hourly_cap": 20, "daily_cap": 80},
            ledger=PhoneLedger(tmp_path / f"{name}.db"),
            now_fn=lambda: 1_700_000_000.0,
        )

    report = await run_pair_chat(
        make("a", "seat-a", device_a), make("b", "seat-b", device_b),
        PHONE_A, PHONE_B, 4, "你好，我在做聊天链路验收", EvidenceLog(tmp_path / "pair-stop"),
    )
    assert report["ok"] is False
    assert len(report["turns"]) == 1
    assert report["turns"][0]["status"] == "failed"
    assert report["turns"][0]["reason"] == "composer_mismatch"
    assert device_a.taps == 0
    assert device_b.taps == 0


def test_composer_bar_excludes_history_and_heard_ignores_old_left_bubbles():
    seed = "你好，我在做聊天链路验收"
    screen = lines_to_screen([
        {"text": "消息和通话已进行端到端加密", "box": [560, 1100, 1040, 1180]},
        {"text": "较方便?", "box": [40, 1400, 320, 1500]},
        {"text": "你好，我在做聊天链路验收", "box": [36, 1680, 760, 1820]},
        {"text": "04:03 √ 你好呀，这么晚还醒着。04:08 发消息 回", "box": [80, 1980, 1000, 2080]},
        {"text": "你好，我在做聊天链路", "box": [80, 2200, 980, 2280]},
        {"text": "验收", "box": [80, 2288, 240, 2348]},
        {"text": "-", "box": [250, 2300, 290, 2360]},
    ], 1080, 2400)
    assert "04:03" not in screen.composer
    assert "发消息" not in screen.composer
    assert composer_matches(screen.composer, seed)
    assert screen.outgoing == []
    assert "端到端加密" not in "\n".join(screen.outgoing)
    assert any("端到端加密" in line for line in screen.notes)
    assert "较方便?" in screen.incoming
    assert new_left_bubbles(screen.incoming, ["较方便?"]) == [seed]
    assert screen.last_incoming != "较方便?"


@pytest.mark.asyncio
async def test_cap_stop_convert_and_rate_limit(tmp_path: Path):
    bus = {}
    device = LinkPhone("a", PHONE_A, bus)
    loop = _loop(tmp_path, device, "seat-a")
    cap = await run_first_touch_cap(
        loop, PHONE_B, ["验收冷启动一"], 1, EvidenceLog(tmp_path / "cap"),
    )
    assert cap["ok"] is True
    assert [row["status"] for row in cap["results"]] == ["sent", "capped"]
    assert device.taps == 1
    assert variant_phone(PHONE_B, 0) != variant_phone(PHONE_B, 1)

    stopped = await run_stop_test(loop, PHONE_A, "不要再发", EvidenceLog(tmp_path / "stop"))
    assert stopped["ok"] is True
    assert device.taps == 1

    pending = await run_convert_pending(
        loop, PHONE_A, "我下单了", EvidenceLog(tmp_path / "deal"), confirm=False,
    )
    assert pending["ok"] is True
    assert pending["active_deals"] == 0

    device.screen = PhoneScreen(header=PHONE_A, composer="发送过于频繁，请稍后再试")
    rate = await run_rate_limit_check(device, EvidenceLog(tmp_path / "rate"))
    assert rate["status"] == "throttled"
    assert rate["tapped"] is False
    assert cold_texts(["一句"], 2)[0] == "一句"


class FramePhone:
    def __init__(self, frames):
        self.frames = list(frames)
        self.i = 0
        self.taps = 0
        self.urls = []
        self.t = 0.0
        self.open_timeout = 3.0
        self.open_interval = 1.0

    def clock(self):
        return self.t

    async def sleep_fn(self, delay):
        self.t += delay

    async def open_url(self, url: str) -> None:
        self.urls.append(url)

    async def screencap(self) -> bytes:
        return b"frame"

    async def read_screen(self, png: bytes) -> PhoneScreen:
        screen = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return screen

    async def tap_send(self) -> None:
        self.taps += 1

    async def press_back(self) -> None:
        return None


@pytest.mark.asyncio
async def test_wa_me_waits_out_contact_picker_and_search(tmp_path: Path):
    phone = "+639933556049"
    device = FramePhone([
        PhoneScreen(header="发送给 03", composer="O"),
        PhoneScreen(header="正在搜索…", composer=""),
        PhoneScreen(header=REAL_HEADER, composer="你好，这是验收"),
    ])
    report = await run_dry(None, device, phone, "你好，这是验收", EvidenceLog(tmp_path / "wait"))
    assert report["ok"] is True
    assert report["status"] == "dry_run"
    assert report["polls"] == 3
    assert device.taps == 0
    assert device.i == 3


@pytest.mark.asyncio
async def test_picker_timeout_is_not_a_text_mismatch(tmp_path: Path):
    device = FramePhone([
        PhoneScreen(header="发送给 03", composer="O"),
    ])
    device.open_timeout = 1.0
    report = await run_dry(None, device, "+639933556049", "你好，这是验收", EvidenceLog(tmp_path / "stuck"))
    assert report["ok"] is False
    assert report["reason"] == "contact_picker"
    assert report["reason"] != "composer_mismatch"
    assert device.taps == 0


@pytest.mark.asyncio
async def test_name_header_and_wrong_text_are_not_a_ready_chat(tmp_path: Path):
    named = FramePhone([PhoneScreen(header="Ann", composer="你好，这是验收")])
    named.open_timeout = 0
    named_report = await run_dry(
        None, named, "+639933556049", "你好，这是验收", EvidenceLog(tmp_path / "name"),
    )
    assert named_report["ok"] is False
    assert named_report["reason"] == "header_timeout"
    assert named.taps == 0

    wrong = FramePhone([PhoneScreen(header=REAL_HEADER, composer="不是这句")])
    wrong.open_timeout = 30
    wrong_report = await run_dry(
        None, wrong, "+639933556049", "你好，这是验收", EvidenceLog(tmp_path / "wrong"),
    )
    assert wrong_report["reason"] == "composer_mismatch"
    assert wrong_report["polls"] == 1
    assert wrong.t == 0.0
    assert wrong.taps == 0


class ProfilePhone(FramePhone):
    def __init__(self, profile_header: str):
        super().__init__([
            PhoneScreen(header="+63 99…", composer="你好，这是验收"),
        ])
        self.profile_header = profile_header
        self.header_taps = 0
        self.backs = 0
        self.phase = "chat"

    async def tap_header(self) -> None:
        self.header_taps += 1
        self.phase = "profile"

    async def press_back(self) -> None:
        self.backs += 1
        self.phase = "chat"

    async def read_screen(self, png: bytes) -> PhoneScreen:
        if self.phase == "profile":
            return PhoneScreen(header=self.profile_header)
        return self.frames[0]


@pytest.mark.asyncio
async def test_real_header_dry_run_matches_without_opening_profile(tmp_path: Path):
    device = FramePhone([
        PhoneScreen(header=REAL_HEADER, composer="你好，这是验收",
                    outgoing=["17:02"], last_incoming="上一句验收"),
    ])
    device.open_timeout = 0
    report = await run_dry(None, device, REAL_PHONE, "你好，这是验收", EvidenceLog(tmp_path / "real"))
    assert report["ok"] is True
    assert report["match"] is True
    assert report["status"] == "dry_run"
    assert report["polls"] == 1
    assert device.taps == 0
    assert not hasattr(device, "header_taps") or device.header_taps == 0


@pytest.mark.asyncio
async def test_short_prefix_opens_profile_and_checks_the_full_number(tmp_path: Path):
    device = ProfilePhone("+63 993 355 6049")
    report = await run_dry(None, device, REAL_PHONE, "你好，这是验收", EvidenceLog(tmp_path / "profile"))
    assert report["ok"] is True
    assert report["profile_confirmed"] is True
    assert device.header_taps == 1
    assert device.backs == 1
    assert device.taps == 0

    wrong = ProfilePhone("+63 111 111 1111")
    missed = await run_dry(None, wrong, REAL_PHONE, "你好，这是验收", EvidenceLog(tmp_path / "other"))
    assert missed["ok"] is False
    assert missed["reason"] == "header_mismatch"
    assert wrong.header_taps == 1
    assert wrong.taps == 0


def test_device_lock_is_exclusive_per_serial(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import DeviceBusy, DeviceLock

    first = DeviceLock(tmp_path, "SERIAL_A")
    first.acquire()
    second = DeviceLock(tmp_path, "SERIAL_A")
    with pytest.raises(DeviceBusy):
        second.acquire()
    other = DeviceLock(tmp_path, "SERIAL_B")
    other.acquire()
    other.release()
    first.release()
    second.acquire()
    second.release()

    stale = tmp_path / ".adb-SERIAL_A.lock"
    stale.write_text("99999999\n", encoding="utf-8")
    taken = DeviceLock(tmp_path, "SERIAL_A")
    taken.acquire()
    taken.release()

    from src.integrations.whatsapp_rpa.phone_acceptance import lock_serials
    busy = DeviceLock(tmp_path, "SERIAL_B")
    busy.acquire()
    with pytest.raises(DeviceBusy):
        lock_serials(tmp_path, ["SERIAL_A", "SERIAL_B"])
    freed = DeviceLock(tmp_path, "SERIAL_A")
    freed.acquire()
    freed.release()
    busy.release()


def _write_ai_only_config(directory: Path) -> Path:
    path = directory / "config.yaml"
    path.write_text(
        "ai:\n"
        "  provider: openai_compatible\n"
        "  base_url: http://127.0.0.1:9/v1\n"
        "  model: deepseek-flash\n"
        "skills:\n"
        "  enabled: []\n"
        "personas:\n"
        "  active: demo\n",
        encoding="utf-8",
    )
    (directory / "config.local.yaml").write_text(
        "ai:\n  api_key: sk-from-overlay\n",
        encoding="utf-8",
    )
    return path


def test_main_config_loader_still_requires_telegram(tmp_path: Path):
    from src.utils.config_manager import ConfigManager

    path = _write_ai_only_config(tmp_path)
    manager = ConfigManager(str(path))
    manager.config = {
        "ai": {"api_key": "sk-from-overlay"},
        "skills": {},
    }
    assert "telegram" not in manager.config
    assert manager._validate_config() is False


@pytest.mark.asyncio
async def test_acceptance_inits_ai_without_telegram(tmp_path: Path, monkeypatch):
    from scripts.wa_phone_loop_acceptance import build_inprocess_decide

    path = _write_ai_only_config(tmp_path)
    seen = {}

    class IdleAI:
        def __init__(self, config):
            seen["config"] = config
            self.client = None
            self._oa_client = None

        async def initialize(self):
            return False

    monkeypatch.setattr("src.ai.ai_client.AIClient", IdleAI)
    with pytest.raises(SystemExit, match="AI 客户端初始化失败"):
        await build_inprocess_decide(path, tmp_path / "data", object())
    loaded = seen["config"]
    assert "telegram" not in loaded.config
    assert loaded.get_ai_config()["api_key"] == "sk-from-overlay"
    assert loaded.get_ai_config()["model"] == "deepseek-flash"
    assert loaded.config["personas"]["active"] == "demo"

    class ReadyAI(IdleAI):
        async def initialize(self):
            self._oa_client = object()
            return True

    class Skill:
        def __init__(self, manager, ai):
            assert "telegram" not in manager.config
            self.ai = ai

        async def initialize(self):
            return True

    monkeypatch.setattr("src.ai.ai_client.AIClient", ReadyAI)
    monkeypatch.setattr("src.skills.skill_manager.SkillManager", Skill)
    decide = await build_inprocess_decide(path, tmp_path / "data", object())
    assert callable(decide)


def test_cli_dry_run_uses_injected_device(tmp_path: Path):
    from scripts.wa_phone_loop_acceptance import async_main, parser
    import asyncio
    device = LinkPhone("a", PHONE_A, {})
    args = parser().parse_args([
        "dry-run",
        "--data-dir", str(tmp_path / "data"),
        "--evidence-dir", str(tmp_path / "evidence"),
        "--adb-ssh-host", "hub176",
        "--serial", "SERIAL_A",
        "--ocr-url", "http://192.168.0.176:8766",
        "--account", "seat-a",
        "--phone", PHONE_B,
        "--text", "你好，这是验收",
    ])

    async def decide(**kwargs):
        raise AssertionError("dry-run 不应调用决定接口")

    report = asyncio.run(async_main(args, devices={"a": device}, decide_fn=decide))
    assert report["ok"] is True
    assert device.taps == 0
    text = Path("scripts/wa_phone_loop_acceptance.py").read_text(encoding="utf-8")
    assert "WhatsAppRpaService" not in text
    assert "TelegramClient" not in text


@pytest.mark.asyncio
async def test_ssh_device_runner_is_injected(tmp_path: Path):
    png = _png(32, 32)
    calls = []

    def runner(argv):
        calls.append(list(argv))
        remote = argv[-1]
        assert "kill-server" not in remote
        assert "start-server" not in remote
        if "screencap" in remote:
            return 0, png, ""
        if "wm" in remote and "size" in remote:
            return 0, b"Physical size: 720x1600\n", ""
        if "dumpsys" in remote:
            return 0, "NotificationRecord pkg=com.whatsapp\n android.title=String (Ann)\n android.text=String (在吗)\n".encode(), ""
        return 0, b"", ""

    device = SshAdbPhoneDevice("hub176", "SERIAL_A", lambda image: PhoneScreen(header="141555…"), runner=runner)
    shot = await device.screencap()
    assert shot.startswith(b"\x89PNG")
    assert calls[0][0] == "ssh"
    assert "hub176" in calls[0]
    notes = await device.poll_notifications()
    assert notes[0]["text"] == "在吗"
    await device.tap_send()
    assert any("input tap" in call[-1] for call in calls)
