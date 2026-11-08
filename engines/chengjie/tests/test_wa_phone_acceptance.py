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
    RecordingDevice,
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
        header = "+" + digits[:8] + "…"
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
    assert not header_matches("14155552…", phone=PHONE_A)
    assert header_needs_profile("14155552…", phone=PHONE_A)
    assert not header_matches("141555…", phone=PHONE_A)
    assert header_needs_profile("141555…", phone=PHONE_A)
    assert header_needs_profile("小明", phone=PHONE_A)
    assert not header_matches("小明", phone=PHONE_A)
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
    for blocked in (["kill-server"], ["start-server"], ["connect", "1.2.3.4:5555"], ["install", "app.apk"]):
        with pytest.raises(ValueError):
            ssh_adb_argv("hub176", "SERIAL_A", blocked)


def _pair_chat_ui_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]">
    <node resource-id="com.whatsapp:id/conversation_contact_name" text="+63 993 355 6049" bounds="[180,96][760,168]" />
    <node resource-id="com.whatsapp:id/back" text="&lt;" bounds="[12,88][108,184]" />
    <node text="消息和通话已进行端到端加密，只有此对话中的成员可以查看" bounds="[180,420][900,520]" />
    <node resource-id="com.whatsapp:id/message_text" text="较方便?" bounds="[36,1400][340,1520]" />
    <node resource-id="com.whatsapp:id/message_text" text="上一句" bounds="[700,1580][1024,1700]" />
    <node resource-id="com.whatsapp:id/message_text" text="你好，早上好，在忙吗" bounds="[36,1760][780,1900]" />
    <node resource-id="com.whatsapp:id/input_attach_button" content-desc="D" bounds="[980,2204][1064,2300]" />
    <node resource-id="com.whatsapp:id/entry" text="你好，早上好，在忙吗" bounds="[140,2196][920,2324]" />
  </node>
</hierarchy>
"""


def test_ui_tree_reads_entry_text_and_splits_bubbles_by_bounds():
    from src.integrations.whatsapp_rpa.phone_acceptance import screen_from_ui_xml

    sentence = "你好，早上好，在忙吗"
    screen = screen_from_ui_xml(_pair_chat_ui_xml().encode("utf-8"))
    assert screen is not None
    assert screen.source == "uiautomator"
    assert screen.composer == sentence
    assert composer_matches(screen.composer, sentence)
    assert "D" not in screen.composer
    assert header_matches(screen.header, phone=REAL_PHONE)
    assert screen.outgoing == ["上一句"]
    assert "<" not in screen.outgoing
    assert all("加密" not in line for line in screen.outgoing)
    assert screen.incoming == ["较方便?", sentence]
    assert screen_from_ui_xml(b"<hierarchy><node text='no entry'/></hierarchy>") is None


@pytest.mark.asyncio
async def test_device_prefers_ui_dump_and_saves_ocr_boxes_on_fallback(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import SshAdbPhoneDevice, screen_from_ui_xml

    png = _png(32, 32)
    xml = _pair_chat_ui_xml().encode("utf-8")
    calls = []

    def runner(argv):
        remote = argv[-1]
        calls.append(remote)
        assert "kill-server" not in remote
        assert "start-server" not in remote
        assert " connect" not in f" {remote}"
        if "uiautomator" in remote and "dump" in remote:
            assert remote.startswith("adb -s SERIAL_A shell ")
            return 0, b"UI hierchary dumped to: /sdcard/wa_ui.xml\n", ""
        if "cat" in remote and "wa_ui.xml" in remote:
            return 0, xml, ""
        if "rm" in remote and "wa_ui.xml" in remote:
            return 0, b"", ""
        if "screencap" in remote:
            return 0, png, ""
        return 0, b"", ""

    ocr_calls = {"n": 0}

    def ocr(_image):
        ocr_calls["n"] += 1
        return PhoneScreen(composer="不应该走到 OCR")

    device = SshAdbPhoneDevice("hub176", "SERIAL_A", ocr, runner=runner)
    screen = await device.read_screen(png)
    assert screen.composer == "你好，早上好，在忙吗"
    assert screen.source == "uiautomator"
    assert ocr_calls["n"] == 0
    assert any("shell uiautomator dump /sdcard/wa_ui.xml" in call for call in calls)
    assert any("shell cat /sdcard/wa_ui.xml" in call for call in calls)
    assert screen_from_ui_xml(xml).composer == screen.composer

    def fail_dump(argv):
        remote = argv[-1]
        if "uiautomator" in remote:
            return 1, b"", "dumped failed"
        return 0, b"", ""

    boxes = [{"text": "你好，早上好，在忙吗", "box": [140, 2200, 780, 2310]},
             {"text": "D", "box": [990, 2216, 1048, 2294]}]

    def ocr_with_boxes(_image):
        parsed = lines_to_screen(boxes, 1080, 2400)
        parsed.ocr_lines = boxes
        parsed.source = "ocr"
        return parsed

    fallback = SshAdbPhoneDevice("hub176", "SERIAL_A", ocr_with_boxes, runner=fail_dump)
    wrapped = RecordingDevice(fallback, EvidenceLog(tmp_path / "ocr"), "a")
    saved = await wrapped.read_screen(png)
    assert saved.source == "ocr"
    assert saved.composer == "你好，早上好，在忙吗"
    stored = json.loads(next((tmp_path / "ocr").glob("*.ocr.json")).read_text(encoding="utf-8"))
    assert stored["source"] == "ocr"
    assert stored["ocr_lines"] == boxes
    assert "D" not in stored["composer"]


@pytest.mark.asyncio
async def test_killed_ui_dump_is_tried_once_then_ocr(tmp_path: Path):
    png = _png(32, 32)
    calls = []

    def runner(argv):
        remote = argv[-1]
        calls.append(remote)
        if "uiautomator" in remote:
            return 137, b"Killed\n", "Killed"
        if "keyevent" in remote and "111" in remote:
            return 0, b"", ""
        if "wm" in remote and "size" in remote:
            return 0, b"Physical size: 1080x2400\n", ""
        if "input" in remote and "tap" in remote:
            return 0, b"", ""
        return 0, b"", ""

    def ocr(_image):
        screen = lines_to_screen([
            {"text": "你好，早上好，在忙吗", "box": [111, 1428, 474, 1472]},
            {"text": "D", "box": [507, 1535, 544, 1572]},
        ], 1080, 2400)
        screen.ocr_lines = [
            {"text": "你好，早上好，在忙吗", "box": [111, 1428, 474, 1472]},
            {"text": "D", "box": [507, 1535, 544, 1572]},
        ]
        screen.source = "ocr"
        return screen

    device = SshAdbPhoneDevice("hub176", "SERIAL_A", ocr, runner=runner)
    first = await device.read_screen(png)
    second = await device.read_screen(png)
    assert first.composer == "你好，早上好，在忙吗"
    assert second.composer == first.composer
    assert first.ui_dump == "killed"
    assert sum(1 for call in calls if "uiautomator" in call) == 1
    await device.tap_send()
    key_at = max(i for i, call in enumerate(calls) if "keyevent" in call and "111" in call)
    size_at = min(i for i, call in enumerate(calls) if i > key_at and "wm" in call and "size" in call)
    tap_at = min(i for i, call in enumerate(calls) if i > size_at and "input" in call and "tap" in call)
    assert key_at < size_at < tap_at


def test_ocr_box_is_scaled_into_device_pixels_before_tap():
    """ceb233 之后那下点空了：OCR 框在约 720×1600，手机是 1080×2400。

    正文 [111,1432,471,1469]。把这套坐标直接拿去 input tap 会点在 y=1450，
    打不中大约 0.907W、0.904H 的绿色发送钮。
    """
    from src.integrations.whatsapp_rpa.phone_acceptance import locate_send_tap

    sentence = "你好，早上好，在忙吗"
    box = [111, 1432, 471, 1469]
    screen = PhoneScreen(composer=sentence, ocr_lines=[{"text": sentence, "box": box}])
    screen.ocr_size = [720, 1600]
    point = locate_send_tap(b"", (1080, 2400), screen)
    cy = (1432 + 1469) / 2.0
    assert point["tap_source"] == "composer"
    assert point["tap"] == [round(0.907 * 1080), round(cy * 2400 / 1600)]
    assert point["tap"][1] != round(cy)
    assert point["device_size"] == [1080, 2400]
    assert point["ocr_size"] == [720, 1600]
    assert abs(point["tap"][0] - 980) <= 1
    assert abs(point["tap"][1] - 2176) <= 1


def test_green_send_button_is_mapped_from_the_screenshot():
    """461×1024 的预览里绿钮在 (418,925)，对应 720×1600 截图里的 (653,1445)。"""
    import io
    from PIL import Image, ImageDraw
    from src.integrations.whatsapp_rpa.phone_acceptance import find_green_send_center, locate_send_tap

    shot_w, shot_h = 720, 1600
    view_w, view_h = 461, 1024
    shot_x = 418 * shot_w / view_w
    shot_y = 925 * shot_h / view_h
    image = Image.new("RGB", (shot_w, shot_h), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    radius = 22
    draw.ellipse(
        (shot_x - radius, shot_y - radius, shot_x + radius, shot_y + radius),
        fill=(37, 211, 102),
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png = buf.getvalue()
    center = find_green_send_center(png)
    assert center is not None
    assert abs(center[0] - shot_x) < 2
    assert abs(center[1] - shot_y) < 2
    point = locate_send_tap(png, (1080, 2400), None)
    assert point["tap_source"] == "green"
    assert point["shot_size"] == [720, 1600]
    assert point["device_size"] == [1080, 2400]
    assert abs(point["tap"][0] - round(shot_x * 1080 / 720)) <= 2
    assert abs(point["tap"][1] - round(shot_y * 2400 / 1600)) <= 2
    assert abs(point["tap"][0] - 980) <= 3
    assert abs(point["tap"][1] - 2168) <= 3


def _paint_bubbles() -> bytes:
    import io
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (720, 1500), (236, 229, 221))
    draw = ImageDraw.Draw(image)
    draw.rectangle((170, 710, 560, 810), fill=(0xD9, 0xFD, 0xD3))
    draw.rectangle((30, 1360, 430, 1460), fill=(255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_wrapped_reply_lines_merge_upward_when_the_left_edge_matches():
    """03 的三行回复被折成五行，行距大约 50px，超过旧的 1.5 倍行高。"""
    lines = [
        {"text": "早呀，这个点起得挺早", "box": [112, 1301, 470, 1335]},
        {"text": "嘛", "box": [111, 1350, 150, 1389]},
        {"text": "我在呢，还没吃早饭，先喝口水缓一缓", "box": [111, 1403, 451, 1441]},
        {"text": "你周末也这么早起", "box": [112, 1453, 367, 1494]},
        {"text": "？", "box": [111, 1506, 417, 1543]},
        {"text": "D", "box": [507, 1535, 544, 1572]},
    ]
    screen = lines_to_screen(lines, 720, 1600)
    expected = "早呀，这个点起得挺早嘛\n我在呢，还没吃早饭，先喝口水缓一缓\n你周末也这么早起？"
    assert "D" not in screen.composer
    assert composer_matches(screen.composer, expected)
    assert composer_matches("早呀， 这个点起得挺早嘛", "早呀，这个点起得挺早嘛")


def test_own_bubble_is_green_even_when_its_center_is_left_of_center():
    """02 发出的气泡 [197,739,519,776]，中心 x=358，在 720 宽的左半边。

    底色是 #D9FDD3，不能当成对方。03 收到的是白底 [58,1388,377,1422]。
    """
    sentence = "你好，早上好，在忙吗"
    own = [197, 739, 519, 776]
    peer = [58, 1388, 377, 1422]
    png = _paint_bubbles()
    screen = lines_to_screen([
        {"text": sentence, "box": own},
        {"text": "在吗", "box": peer},
        {"text": "输入消息", "box": [120, 1450, 400, 1490]},
    ], 720, 1500, png=png)
    assert screen.outgoing == [sentence]
    assert screen.incoming == ["在吗"]
    assert (own[0] + own[2]) / 2 < 720 / 2

    pale = lines_to_screen([
        {"text": sentence, "box": own},
        {"text": "在吗", "box": peer},
        {"text": "输入消息", "box": [120, 1450, 400, 1490]},
    ], 720, 1500)
    assert pale.outgoing == [sentence]
    assert pale.incoming == ["在吗"]
    assert own[0] >= 720 * 0.12
    assert peer[0] < 720 * 0.12


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
    assert composer_matches("你好，我在做聊天链路验收-", "你好，我在做聊天链路验收。")
    assert not composer_matches("今天天气真好我们去散步", "你好，我在做聊天链路验收。")


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
    assert screen.header == "+63 993 355 6..."
    assert header_matches(screen.header, phone=REAL_PHONE)
    assert header_matches(REAL_HEADER, phone=REAL_PHONE)
    assert screen.composer == "你好，这是验收"
    assert screen.last_incoming == "在吗"
    assert screen.outgoing == ["上一句验收"]
    assert "17:02" not in screen.outgoing
    assert "昨天" not in screen.outgoing
    assert screen.last_incoming != "上一句验收"
    assert "17:02" in screen.notes
    assert "昨天" in screen.notes


def test_header_uses_only_the_plus_box_and_stops_at_the_ellipsis():
    """03 顶栏把电量、时间和右侧的「2」拼进号码，前缀才和 639543629194 冲突。"""
    from src.integrations.whatsapp_rpa.phone_identity import header_number_candidates

    blob = "6:51… 100 +63 954362 9... 2 在线 消息和通话已进行端到端加密…"
    phone = "+639543629194"
    assert header_number_candidates(blob) == [("639543629", True)]
    assert header_matches(blob, phone=phone)
    assert "6395436292" not in [digits for digits, _flag in header_number_candidates(blob)]
    width, height = 720, 1600
    screen = lines_to_screen([
        {"text": "6:51", "box": [24, 8, 90, 36]},
        {"text": "100", "box": [620, 8, 690, 36]},
        {"text": "+63 954 362 9...", "box": [198, 86, 435, 121]},
        {"text": "2", "box": [579, 111, 586, 122]},
        {"text": "在线", "box": [198, 128, 250, 156]},
        {"text": "消息和通话已进行端到端加密", "box": [40, 180, 680, 230]},
    ], width, height)
    assert screen.header == "+63 954 362 9..."
    assert header_matches(screen.header, phone=phone)
    assert "在线" not in screen.header
    assert "端到端" not in screen.header
    named = lines_to_screen([
        {"text": "小明", "box": [198, 86, 320, 121]},
    ], width, height)
    assert named.header == "小明"
    assert header_needs_profile(named.header, phone=phone)


@pytest.mark.asyncio
async def test_existing_draft_is_sent_or_cleared_before_prefill():
    from urllib.parse import parse_qs, unquote, urlparse

    from src.integrations.whatsapp_rpa.phone_send import deliver_via_wa_me

    sentence = "早呀，这个点起得挺早嘛"

    class SameDraft:
        def __init__(self):
            self.taps = 0
            self.urls = []
            self.cleared = False
            self.screen = PhoneScreen(header=PHONE_A, composer=sentence)

        async def open_url(self, url: str) -> None:
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return self.screen

        async def clear_composer(self) -> None:
            self.cleared = True

        async def tap_send(self) -> None:
            self.taps += 1
            self.screen = PhoneScreen(header=PHONE_A, composer="", outgoing=[sentence])

        async def press_back(self) -> None:
            return None

    same = SameDraft()
    sent = await deliver_via_wa_me(same, PHONE_A, sentence)
    assert sent.status == "sent"
    assert same.urls == []
    assert same.cleared is False
    assert same.taps == 1

    class StaleDraft:
        def __init__(self):
            self.taps = 0
            self.urls = []
            self.cleared = False
            self.screen = PhoneScreen(header=PHONE_A, composer="不忙呢，刚起来没多久")

        async def open_url(self, url: str) -> None:
            self.urls.append(url)
            text = unquote(parse_qs(urlparse(url).query).get("text", [""])[0])
            prior = "" if self.cleared else self.screen.composer
            self.screen = PhoneScreen(header=PHONE_A, composer=prior + text)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return self.screen

        async def clear_composer(self) -> None:
            self.cleared = True
            self.screen = PhoneScreen(header=PHONE_A, composer="")

        async def tap_send(self) -> None:
            self.taps += 1
            shown = self.screen.composer
            self.screen = PhoneScreen(header=PHONE_A, composer="", outgoing=[shown])

        async def press_back(self) -> None:
            return None

    stale = StaleDraft()
    sent = await deliver_via_wa_me(stale, PHONE_A, sentence)
    assert stale.cleared is True
    assert sent.status == "sent"
    assert stale.screen.outgoing == [sentence]


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
async def test_visible_seed_is_marked_sent_and_not_sent_again(tmp_path: Path):
    sentence = "你好，早上好，在忙吗"
    bus = {}

    class SeenPhone(LinkPhone):
        async def open_url(self, url: str) -> None:
            self.urls.append(url)
            self.screen = PhoneScreen(
                header=PHONE_B, composer="", outgoing=[sentence],
            )

    device_a = SeenPhone("a", PHONE_A, bus)
    device_b = LinkPhone("b", PHONE_B, bus)
    store = InboxStore(tmp_path / "inbox.db")
    ledger = PhoneLedger(tmp_path / "ledger.db")
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
    key = sent_content_key("acceptance", PHONE_B, sentence)
    ledger.reserve(
        client_msg_id="old-fail", phone=PHONE_B, account_id="seat-a",
        source="acceptance", content_key=key,
    )
    ledger.finish("old-fail", "failed", error="bubble_missing")

    def make(name, account, device, book):
        return WaPhoneLoop(
            account_id=account, inbox=store, device=device,
            decide_fn=_no_decide, cfg={"enabled": True},
            ledger=book, now_fn=lambda: 1_700_000_000.0, run_id="run-next",
        )

    async def _no_decide(**kwargs):
        return {"ok": True, "reply": "收到"}

    report = await run_pair_chat(
        make("a", "seat-a", device_a, ledger),
        make("b", "seat-b", device_b, PhoneLedger(tmp_path / "b.db")),
        PHONE_A, PHONE_B, 1, sentence, EvidenceLog(tmp_path / "seen"),
    )
    assert report["turns"][0]["status"] == "sent"
    assert report["turns"][0]["reason"] == "already_visible"
    assert device_a.taps == 0
    assert ledger.get_send("old-fail")["status"] == "sent"
    assert ledger.has_sent_content(key) is True


@pytest.mark.asyncio
async def test_empty_text_is_reread_and_the_next_turn_still_runs(tmp_path: Path):
    sentence = "你好，早上好，在忙吗"

    class Quiet:
        def __init__(self):
            self.reads = 0
            self.taps = 0
            self.urls = []

        async def open_url(self, url: str) -> None:
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            self.reads += 1
            return PhoneScreen(header=PHONE_A, composer="")

        async def poll_notifications(self):
            return []

        async def tap_send(self) -> None:
            self.taps += 1

        async def press_back(self) -> None:
            return None

    device_a = LinkPhone("a", PHONE_A, {})
    device_b = Quiet()
    store = InboxStore(tmp_path / "inbox.db")

    async def decide(**kwargs):
        return {"ok": True, "reply": "收到"}

    def make(account, device, name):
        return WaPhoneLoop(
            account_id=account, inbox=store, device=device, decide_fn=decide,
            cfg={"enabled": True, "hourly_cap": 20, "daily_cap": 80},
            ledger=PhoneLedger(tmp_path / f"{name}.db"),
            now_fn=lambda: 1_700_000_000.0, run_id=name,
        )

    report = await run_pair_chat(
        make("seat-a", device_a, "a"), make("seat-b", device_b, "b"),
        PHONE_A, PHONE_B, 3, sentence, EvidenceLog(tmp_path / "empty"),
    )
    assert [row.get("status") for row in report["turns"]][0] == "sent"
    assert report["turns"][1]["reason"] == "empty_text"
    assert len(report["turns"]) == 3
    assert device_b.reads >= 2


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


def test_ceb233_composer_drops_the_suggestion_icon_below_the_draft():
    """ceb233 真机 OCR。正文 [111,1428,474,1472]，其下「D」[507,1535,544,1572]。

    「D」在正文下方约 60px、水平居中，是键盘建议条或表情条，不是输入栏里的字。
    折行如果还和正文垂直连着，仍然并进来。
    """
    sentence = "你好，早上好，在忙吗"
    screen = lines_to_screen([
        {"text": sentence, "box": [111, 1428, 474, 1472]},
        {"text": "D", "box": [507, 1535, 544, 1572]},
    ], 1080, 2400)
    assert screen.composer == sentence
    assert composer_matches(screen.composer, sentence)
    assert "D" not in screen.composer

    wrapped = lines_to_screen([
        {"text": "你好，早上好，", "box": [111, 1428, 474, 1472]},
        {"text": "在忙吗", "box": [111, 1484, 280, 1528]},
        {"text": "D", "box": [507, 1600, 544, 1637]},
    ], 1080, 2400)
    assert composer_matches(wrapped.composer, sentence)
    assert "D" not in wrapped.composer


def test_pair0608_read2_drops_the_camera_icon_not_letters_in_the_text():
    """005-a-read2.ocr.json：输入框是「你好，早上好，在忙吗」再加一行「D」。

    「D」落在右侧 15% 的图标槽，框窄，并且和正文隔开。附件或相机被认成字母。
    同一句里如果字就在正文框中，或者拆出来的字贴着正文，都要留下。
    """
    sentence = "你好，早上好，在忙吗"
    screen = lines_to_screen([
        {"text": sentence, "box": [140, 2200, 780, 2310]},
        {"text": "D", "box": [990, 2216, 1048, 2294]},
    ], 1080, 2400)
    assert screen.composer == sentence
    assert composer_matches(screen.composer, sentence)
    assert "D" not in screen.composer

    glued = lines_to_screen([
        {"text": sentence + "D", "box": [140, 2200, 860, 2310]},
    ], 1080, 2400)
    assert glued.composer == sentence + "D"

    adjacent = lines_to_screen([
        {"text": sentence, "box": [140, 2200, 940, 2310]},
        {"text": "D", "box": [948, 2216, 1004, 2294]},
    ], 1080, 2400)
    assert "D" in adjacent.composer


def test_wrapped_bubbles_drop_the_clock_and_heard_keeps_every_new_one():
    """07:07 / 07:08 的长消息被折成两行，尾部还有时间和已读勾。不能只留最后一行。"""
    from src.integrations.whatsapp_rpa.phone_send import screen_is_sliding, strip_bubble_tail

    own = "早嘛，周日这个点醒的，是自然醒还是被什么事叫起来的呀。"
    peer = "早上好呀，不忙，刚起来没多久。你这问候可够早的。"
    screen = lines_to_screen([
        {"text": "+639543629194", "box": [198, 86, 520, 130]},
        {"text": "旧的一句", "box": [36, 700, 220, 748]},
        {"text": "早上好呀，不忙，刚起来没多久。", "box": [36, 820, 400, 868]},
        {"text": "你这问候可够早的。", "box": [36, 880, 280, 928]},
        {"text": "7:07 ✓✓", "box": [300, 930, 420, 968]},
        {"text": "另一条新消息", "box": [36, 1040, 280, 1088]},
        {"text": "早嘛，周日这个点醒的，是自然醒还是", "box": [200, 1160, 640, 1208]},
        {"text": "被什么事叫起来的呀。7:08 ✓✓", "box": [200, 1220, 620, 1268]},
        {"text": "输入消息", "box": [111, 1500, 400, 1548]},
    ], 720, 1600)
    assert screen.outgoing == [own]
    assert composer_matches(screen.outgoing[0], own)
    assert peer in screen.incoming
    assert "7:07" not in "\n".join(screen.incoming)
    assert "7:08" not in screen.outgoing[0]
    assert strip_bubble_tail("被什么事叫起来的呀。7:08 ✓✓") == "被什么事叫起来的呀。"
    fresh = new_left_bubbles(screen.incoming, ["旧的一句"])
    assert fresh == [peer, "另一条新消息"]
    assert "\n".join(fresh).startswith(peer)

    sliding = lines_to_screen([
        {"text": "不忙呢，刚起来没多久", "box": [371, 1470, 680, 1540]},
        {"text": "e", "box": [308, 1492, 338, 1529]},
    ], 720, 1600)
    assert sliding.composer == "不忙呢，刚起来没多久"
    assert "e" not in sliding.composer
    assert screen_is_sliding(sliding)


@pytest.mark.asyncio
async def test_read_peer_text_returns_every_new_bubble(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import read_peer_text

    peer = "早上好呀，不忙，刚起来没多久。你这问候可够早的。"
    screen = PhoneScreen(
        header=PHONE_A,
        incoming=["旧的一句", peer, "另一条新消息"],
        bubbles=[
            {"side": "in", "text": "旧的一句", "top": 700, "bottom": 748},
            {"side": "out", "text": "早嘛，周日能睡到这会儿才起，挺舒服的吧。", "top": 800, "bottom": 860},
            {"side": "in", "text": peer, "top": 900, "bottom": 980},
            {"side": "in", "text": "o", "top": 990, "bottom": 1010},
            {"side": "in", "text": "另一条新消息", "top": 1040, "bottom": 1090},
        ],
    )

    class Quiet:
        async def open_url(self, url: str) -> None:
            return None

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return screen

        async def poll_notifications(self):
            return []

    heard = await read_peer_text(
        Quiet(), PHONE_A, EvidenceLog(tmp_path / "heard"), baseline=["旧的一句"],
    )
    assert heard["ok"] is True
    assert heard["text"] == peer + "\n另一条新消息"
    assert "o" not in heard["text"].split("\n")
    assert "旧的一句" not in heard["text"]


def test_timestamp_and_wallpaper_gap_split_two_bubbles():
    """贴得很近的两句，中间是时间戳或聊天背景，就不能并成一条。"""
    import io
    from PIL import Image

    image = Image.new("RGB", (720, 1600), (236, 229, 221))
    for top, bottom in ((400, 448), (456, 504)):
        for y in range(top, bottom):
            for x in range(30, 420):
                image.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png = buf.getvalue()
    first = "早嘛，周日能睡到这会儿才起，挺舒服的吧。"
    second = "对了，还没问你，平时怎么称呼你比较方便呀？"
    screen = lines_to_screen([
        {"text": first, "box": [36, 400, 400, 448]},
        {"text": second, "box": [36, 456, 400, 504]},
        {"text": "输入消息", "box": [111, 1500, 400, 1548]},
    ], 720, 1600, png)
    assert screen.incoming == [first, second]
    marked = lines_to_screen([
        {"text": first, "box": [36, 400, 400, 448]},
        {"text": "7:28 ✓✓", "box": [300, 450, 420, 478]},
        {"text": second, "box": [36, 470, 400, 518]},
        {"text": "输入消息", "box": [111, 1500, 400, 1548]},
    ], 720, 1600)
    assert marked.incoming == [first, second]


def test_placeholder_and_bottom_bubble_count_as_sent():
    from src.integrations.whatsapp_rpa.phone_send import send_confirmed

    sentence = "对了，还没问你，平时怎么称呼你比较方便呀？"
    screen = lines_to_screen([
        {"text": "对了，还没问你，平时怎么称呼", "box": [135, 1201, 643, 1232]},
        {"text": "你比较方便呀？", "box": [132, 1243, 280, 1281]},
        {"text": "发消息", "box": [111, 1500, 280, 1540]},
    ], 720, 1600)
    assert "发消息" in screen.composer
    assert send_confirmed(screen, sentence) is True
    assert send_confirmed(
        PhoneScreen(composer="发消息", outgoing=[sentence[:8] + "完全另一句"]),
        sentence,
    ) is False


@pytest.mark.asyncio
async def test_same_inbound_cannot_send_twice_and_stale_draft_is_finished(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import settle_stale_draft

    device = LinkPhone("a", PHONE_A, {})
    store = InboxStore(tmp_path / "inbox.db")
    ledger = PhoneLedger(tmp_path / "ledger.db")

    async def decide(**kwargs):
        return {"ok": True, "reply": "早嘛，周日能睡到这会儿才起，挺舒服的吧。"}

    loop = WaPhoneLoop(
        account_id="seat-a", inbox=store, device=device, decide_fn=decide,
        cfg={"enabled": True}, ledger=ledger,
        now_fn=lambda: 1_700_000_000.0, run_id="pair",
    )
    first = await loop.handle_inbound(phone=PHONE_B, text="在吗", display_name="03")
    second = await loop.handle_inbound(phone=PHONE_B, text="在吗", display_name="03")
    assert first["send"]["status"] == "sent"
    assert second["reason"] == "already_replied"
    assert second["send"]["tapped"] is False
    assert device.taps == 1

    draft = "嗯，被饿醒的，肚子先抗议了。你起这么早是有事还是也睡不着呀。"
    ledger.reserve(
        client_msg_id="draft-1", phone=PHONE_B, account_id="seat-a",
        source="reply", content_key="pending-draft", body=draft,
    )
    ledger.finish("draft-1", "failed", error="bubble_missing")

    class DraftPhone(LinkPhone):
        def __init__(self):
            super().__init__("b", PHONE_B, {})
            self.cleared = False
            self.screen = PhoneScreen(header=PHONE_A, composer=draft)

        async def clear_composer(self) -> None:
            self.cleared = True
            self.screen = PhoneScreen(header=PHONE_A, composer="")

        async def read_screen(self, png):
            return self.screen

        async def tap_send(self) -> None:
            self.taps += 1
            shown = self.screen.composer
            self.screen = PhoneScreen(header=PHONE_A, composer="", outgoing=[shown])

    other = DraftPhone()
    other_loop = WaPhoneLoop(
        account_id="seat-b", inbox=store, device=other, decide_fn=decide,
        cfg={"enabled": True}, ledger=PhoneLedger(tmp_path / "b.db"),
        now_fn=lambda: 1_700_000_000.0, run_id="b",
    )
    other_loop.ledger.reserve(
        client_msg_id="draft-b", phone=PHONE_A, account_id="seat-b",
        source="reply", content_key="k", body=draft,
    )
    other_loop.ledger.finish("draft-b", "failed", error="bubble_missing")
    settled = await settle_stale_draft(other_loop, PHONE_A)
    assert settled["status"] == "sent"
    assert other.taps == 1

    other.screen = PhoneScreen(header=PHONE_A, composer="这是一句不在台账里的草稿")
    other.taps = 0
    cleared = await settle_stale_draft(other_loop, PHONE_A)
    assert cleared["status"] == "cleared"
    assert other.cleared is True
    assert other.taps == 0


def test_scroll_to_latest_swipes_then_taps_the_down_button():
    calls = []

    def runner(argv):
        calls.append(argv[-1])
        if "wm size" in argv[-1]:
            return 0, b"Physical size: 1080x2400\n", ""
        return 0, b"", ""

    device = SshAdbPhoneDevice("hub176", "SERIAL", lambda _png: PhoneScreen(), runner=runner)
    import asyncio
    asyncio.run(device.scroll_to_latest())
    blob = "\n".join(calls)
    assert "input swipe" in blob
    assert "input tap" in blob

    typed = []

    def type_runner(argv):
        typed.append(argv[-1])
        if "wm size" in argv[-1]:
            return 0, b"Physical size: 1080x2400\n", ""
        return 0, b"", ""

    phone = SshAdbPhoneDevice("hub176", "SERIAL", lambda _png: PhoneScreen(), runner=type_runner)
    asyncio.run(phone.prefill_text("叫我顾嘉就行"))
    assert not any("clipboard" in line for line in typed)
    typed.clear()
    asyncio.run(phone.prefill_text("hello"))
    assert any("input text hello" in line for line in typed)


@pytest.mark.asyncio
async def test_halfwidth_question_mark_matches_without_waiting():
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    expected = "叫我顾嘉就行，你平时朋友都怎么称呼你呀？"
    shown = expected[:-1] + "?"

    class Ready:
        def __init__(self):
            self.t = 0.0
            self.open_timeout = 60.0
            self.reads = 0

        def clock(self):
            return self.t

        async def sleep_fn(self, delay):
            self.t += delay

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            self.reads += 1
            return PhoneScreen(header="+639543629194", composer=shown)

    device = Ready()
    ready = await wait_for_chat(device, "+639543629194", expected=expected)
    assert ready["ready"] is True
    assert device.t == 0
    assert device.reads == 1


@pytest.mark.asyncio
async def test_deadline_stops_the_wait_and_writes_a_final_shape(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    class Late:
        def __init__(self):
            self.deadline_at = 5.0

        def clock(self):
            return 5.0

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return PhoneScreen(header="发送给", composer="发消息")

    ready = await wait_for_chat(Late(), "+639543629194", expected="你好")
    assert ready["reason"] == "max_seconds"
    log = EvidenceLog(tmp_path)
    log.note(turns=[{"turn": 0, "status": "sent"}], history=[{"label": "04开场", "status": "sent"}], timings=[{"wait_ms": 3}])
    final = log.finish({
        "command": "pair-chat", "ok": False, "reason": "max_seconds",
        "turns": log.turns, "history": log.history, "timings": log.timings,
    })
    assert final["partial"] is False
    assert final["reason"] == "max_seconds"
    assert final["turns"][0]["status"] == "sent"
    assert final["history"][0]["label"] == "04开场"


def test_own_persona_name_stays_and_peer_name_is_stripped():
    from src.utils.persona_guard import find_wrong_self_name, sanitize_self_name

    hard, _soft = find_wrong_self_name("叫我小界就好", ["小界"], ["顾嘉"])
    assert hard == []
    hard, _soft = find_wrong_self_name("叫我顾嘉就行", ["小界"], ["顾嘉"])
    assert any("顾嘉" in hit for hit in hard)
    _cleaned, hits, _soft = sanitize_self_name("叫我顾嘉就行，你呢？", ["小界"], ["顾嘉"])
    assert any("顾嘉" in hit for hit in hits)


def test_first_bubble_counts_as_sent_without_green_color():
    from src.integrations.whatsapp_rpa.phone_acceptance import quote_close_point
    from src.integrations.whatsapp_rpa.phone_send import send_confirmed

    opener = "早上好呀，今天天气怎么样"
    screen = PhoneScreen(composer="发消息", incoming=[opener], outgoing=[])
    assert send_confirmed(screen, opener) is True
    point = quote_close_point(
        [{"text": "X", "box": [640, 1480, 670, 1510]}], 720, 1600,
    )
    assert point == (655, 1495)
    assert quote_close_point(
        [{"text": "X", "box": [20, 40, 40, 60]}], 720, 1600,
    ) is None


def test_echoed_name_is_cleared_and_seen_text_is_marked_sent(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import (
        _echoes_peer_intro, absorb_visible_texts, ledger_history,
    )
    from src.integrations.whatsapp_rpa.phone_loop import persona_separation_note, sent_content_key

    note = persona_separation_note("阿宁", "顾嘉")
    assert "你是阿宁" in note
    assert "对方是顾嘉" in note
    assert "不要复述对方的自我介绍" in note

    phone = "+639933556049"
    text = "对了，还没问你，平时怎么称呼你比较方便呀？"
    ledger = PhoneLedger(tmp_path / "a.db")
    ledger.reserve(
        client_msg_id="q", phone=phone, account_id="seat-a",
        source="reply", body=text, content_key="q",
    )
    ledger.finish("q", "failed", error="bubble_missing")
    absorb_visible_texts(ledger, phone, ["对了，还没问你，平时怎么称呼你比较方便呀?"])
    labels = {row["label"]: row["status"] for row in ledger_history(ledger)}
    assert labels["07:35"] == "sent"
    assert ledger.has_sent_content(sent_content_key("reply", phone, text))
    ledger.note_inbound(phone, "叫我顾嘉就行，你呢，怎么称呼比较方便？")
    assert _echoes_peer_intro(
        ledger, phone, "叫我顾嘉就行，你平时朋友都怎么称呼你呀？",
    )


def test_report_is_written_before_the_run_finishes(tmp_path: Path):
    log = EvidenceLog(tmp_path)
    log.save("a-read1", b"png", PhoneScreen(header="+639543629194", composer="发消息"))
    early = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert early["partial"] is True
    assert early["steps"][0]["name"] == "a-read1"
    assert early["turns"] == [] and early["history"] == [] and early["timings"] == []
    log.note(turns=[{"turn": 0}], history=[{"label": "04开场"}], timings=[{"wait_ms": 1}])
    log.save("a-read2", b"png", PhoneScreen(composer="发消息"))
    kept = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert kept["turns"] == [{"turn": 0}]
    assert kept["history"][0]["label"] == "04开场"
    assert kept["timings"][0]["wait_ms"] == 1
    final = log.finish({"command": "pair-chat", "ok": True, "turns": []})
    assert final["partial"] is False
    assert "report.json" in final["report_path"]


@pytest.mark.asyncio
async def test_unstable_frames_stop_after_three_tries():
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    class Jitter:
        def __init__(self):
            self.n = 0
            self.t = 0.0
            self.open_timeout = 30.0

        def clock(self):
            return self.t

        async def sleep_fn(self, delay):
            self.t += delay

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            self.n += 1
            return PhoneScreen(
                header="+639543629194", composer="早呀，刚醒，还没吃上早饭呢。",
                ocr_lines=[{"text": f"box-{self.n}", "box": [1, 1, 20, 20]}],
                shot_size=[720, 1600], composer_left=40,
            )

    device = Jitter()
    ready = await wait_for_chat(device, "+639543629194", expected="早呀，刚醒，还没吃上早饭呢。")
    assert ready["ready"] is True
    assert ready["timings"]["stability_tries"] == 3
    assert ready["timings"]["wait_ms"] < 5000
    assert device.n <= 8


@pytest.mark.asyncio
async def test_placeholder_switches_once_and_does_not_wait_out_the_timeout():
    from src.integrations.whatsapp_rpa.phone_send import deliver_via_wa_me

    sentence = "早呀，那正好，我这边也还没吃呢，饿着肚子陪你聊会儿。"

    class Stuck:
        def __init__(self):
            self.urls = []
            self.t = 0.0
            self.open_timeout = 40.0
            self.taps = 0
            self.keyboard = False

        def clock(self):
            return self.t

        async def sleep_fn(self, delay):
            self.t += delay

        async def open_url(self, url: str) -> None:
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return PhoneScreen(header="+639543629194", composer="发消息")

        async def tap_send(self) -> None:
            self.taps += 1

        async def press_back(self) -> None:
            return None

        async def unicode_keyboard_available(self) -> bool:
            return self.keyboard

    device = Stuck()
    missed = await deliver_via_wa_me(device, "+639543629194", sentence)
    assert missed.status == "failed"
    assert missed.reason == "composer_not_ready"
    assert missed.tapped is False
    assert device.taps == 0
    assert len(device.urls) == 1
    assert "wa.me/" in device.urls[0] and "text=" in device.urls[0]
    assert device.t < 5

    class Keyboard(Stuck):
        def __init__(self):
            super().__init__()
            self.keyboard = True
            self.typed = []
            self.filled = False

        async def prefill_unicode(self, text: str) -> None:
            self.typed.append(text)
            self.filled = True

        async def read_screen(self, png):
            if self.filled and self.taps:
                return PhoneScreen(
                    header="+639543629194", composer="发消息", outgoing=[sentence],
                )
            if self.filled:
                return PhoneScreen(header="+639543629194", composer=sentence)
            return PhoneScreen(header="+639543629194", composer="发消息")

        async def tap_send(self) -> None:
            self.taps += 1

    typed = Keyboard()
    sent = await deliver_via_wa_me(typed, "+639543629194", sentence)
    assert typed.typed == [sentence]
    assert sent.timings.get("retried") is True
    assert sent.status == "sent"
    assert typed.t < 8


@pytest.mark.asyncio
async def test_pending_second_segment_is_sent_without_a_new_inbound(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import ledger_history, settle_stale_draft

    draft = "叫我顾嘉就行，你呢，怎么称呼比较方便？"
    phone = "+639543629194"

    class Box:
        def __init__(self):
            self.taps = 0
            self.urls = []
            self.screen = PhoneScreen(header=phone, composer=draft)

        async def open_url(self, url: str) -> None:
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return self.screen

        async def tap_send(self) -> None:
            self.taps += 1
            self.screen = PhoneScreen(header=phone, composer="发消息", outgoing=[draft])

        async def press_back(self) -> None:
            return None

        async def clear_composer(self) -> None:
            self.screen = PhoneScreen(header=phone, composer="")

    device = Box()
    ledger = PhoneLedger(tmp_path / "b.db")
    ledger.note_inbound(phone, "在吗")
    ledger.mark_replied(phone, "在吗")
    ledger.reserve(
        client_msg_id="part-2", phone=phone, account_id="seat-b",
        source="reply", body=draft, content_key="part-2",
    )
    ledger.finish("part-2", "failed", error="composer_not_ready")
    store = InboxStore(tmp_path / "inbox.db")

    async def decide(**kwargs):
        return {"ok": True, "reply": "不该再生成"}

    loop = WaPhoneLoop(
        account_id="seat-b", inbox=store, device=device, decide_fn=decide,
        cfg={"enabled": True}, ledger=ledger,
        now_fn=lambda: 1_700_000_000.0, run_id="b",
    )
    settled = await settle_stale_draft(loop, phone)
    assert settled["status"] == "sent"
    assert device.taps == 1
    assert device.urls == []

    device.screen = PhoneScreen(header=phone, composer="过时的另一句")
    device.taps = 0
    cleared = await settle_stale_draft(loop, phone)
    assert cleared["status"] == "cleared"
    assert device.taps == 0
    assert device.screen.composer == ""

    ledger.reserve(
        client_msg_id="morning", phone=phone, account_id="seat-b",
        source="reply", body="早呀，刚醒，还没吃上早饭呢。", content_key="m",
    )
    ledger.finish("morning", "sent")
    labels = {row["label"]: row["status"] for row in ledger_history(ledger)}
    assert labels["07:54"] == "sent"
    assert labels["07:28"] == "absent"
    assert labels["开场"] == "absent"


@pytest.mark.asyncio
async def test_sliding_frame_waits_until_the_boxes_stay_still():
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    phone = "+639543629194"
    sentence = "不忙呢，刚起来没多久"
    sliding = lines_to_screen([
        {"text": phone, "box": [198, 86, 520, 130]},
        {"text": sentence, "box": [371, 1470, 680, 1540]},
        {"text": "e", "box": [308, 1492, 338, 1529]},
    ], 720, 1600)
    stable = lines_to_screen([
        {"text": phone, "box": [198, 86, 520, 130]},
        {"text": sentence, "box": [111, 1470, 420, 1540]},
    ], 720, 1600)

    class Frames:
        def __init__(self):
            self.reads = 0
            self.t = 0.0
            self.open_timeout = 5.0

        def clock(self):
            return self.t

        async def sleep_fn(self, delay):
            self.t += delay

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            self.reads += 1
            if self.reads == 1:
                return sliding
            return stable

    device = Frames()
    ready = await wait_for_chat(device, phone, expected=sentence)
    assert ready["ready"] is True
    assert "e" not in ready["composer"]
    assert device.reads >= 3


@pytest.mark.asyncio
async def test_visible_reply_is_marked_sent_without_tapping_again(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key

    phone = "+639543629194"
    sentence = "早嘛，周日这个点醒的，是自然醒还是被什么事叫起来的呀。"
    shown = lines_to_screen([
        {"text": phone, "box": [198, 86, 520, 130]},
        {"text": "早嘛，周日这个点醒的，是自然醒还是", "box": [200, 1160, 640, 1208]},
        {"text": "被什么事叫起来的呀。7:08 ✓✓", "box": [200, 1220, 620, 1268]},
        {"text": "输入消息", "box": [111, 1500, 400, 1548]},
    ], 720, 1600)

    class Stay:
        def __init__(self):
            self.urls = []
            self.taps = 0

        async def open_url(self, url: str) -> None:
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return shown

        async def tap_send(self) -> None:
            self.taps += 1

        async def press_back(self) -> None:
            return None

    device = Stay()
    ledger = PhoneLedger(tmp_path / "ledger.db")
    key = sent_content_key("reply", phone, sentence)
    ledger.reserve(
        client_msg_id="old-missing", phone=phone, account_id="seat-a",
        source="reply", content_key=key,
    )
    ledger.finish("old-missing", "failed", error="bubble_missing")
    store = InboxStore(tmp_path / "inbox.db")

    async def decide(**kwargs):
        return {"ok": True, "reply": sentence}

    loop = WaPhoneLoop(
        account_id="seat-a", inbox=store, device=device, decide_fn=decide,
        cfg={"enabled": True}, ledger=ledger,
        now_fn=lambda: 1_700_000_000.0, run_id="again",
    )
    sent = await loop.send_prepared(
        phone=phone, text=sentence, source="reply",
        client_msg_id="new-try",
    )
    assert sent["status"] == "sent"
    assert sent["reason"] == "already_visible"
    assert sent["tapped"] is False
    assert device.taps == 0
    assert device.urls == []
    assert ledger.get_send("old-missing")["status"] == "sent"
    assert ledger.has_sent_content(key) is True


@pytest.mark.asyncio
async def test_profile_check_presses_back_again_when_the_first_back_stays(tmp_path: Path):
    class Sticky(ProfilePhone):
        def __init__(self):
            super().__init__("+63 993 355 6049")
            self.backs = 0

        async def press_back(self) -> None:
            self.backs += 1
            if self.backs >= 2:
                self.phase = "chat"

    device = Sticky()
    report = await run_dry(None, device, REAL_PHONE, "你好，这是验收", EvidenceLog(tmp_path / "sticky"))
    assert report["profile_confirmed"] is True
    assert device.backs == 2
    assert device.phase == "chat"


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


@pytest.mark.asyncio
async def test_inprocess_decide_accepts_the_persona_names(tmp_path: Path, monkeypatch):
    """pair-chat 会把 self_name 传进进程内 decide。签名对不上会在真机上直接崩溃。"""
    from scripts.wa_phone_loop_acceptance import bind_persona_decide, build_inprocess_decide

    path = _write_ai_only_config(tmp_path)
    captured = {}

    class ReadyAI:
        def __init__(self, config):
            self._oa_client = None

        async def initialize(self):
            self._oa_client = object()
            return True

    class Skill:
        def __init__(self, manager, ai):
            self.ai = ai

        async def initialize(self):
            return True

    async def fake_decide(app, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "reply": "在的"}

    monkeypatch.setattr("src.ai.ai_client.AIClient", ReadyAI)
    monkeypatch.setattr("src.skills.skill_manager.SkillManager", Skill)
    monkeypatch.setattr(
        "src.integrations.whatsapp_rpa.phone_loop.decide_whatsapp", fake_decide,
    )
    decide = await build_inprocess_decide(path, tmp_path / "data", object())
    wrapped = bind_persona_decide(decide, "xiaojie", "小界", "顾嘉")
    result = await wrapped(
        account_id="seat-b", phone=PHONE_A, last_inbound="叫我顾嘉", persona_id="other",
    )
    assert result["reply"] == "在的"
    assert captured["self_name"] == "小界"
    assert captured["peer_names"] == ["顾嘉"]
    assert captured["persona_id"] == "xiaojie"
    assert "小界" in captured["agent_instruction"]
    assert "顾嘉" in captured["agent_instruction"]


def test_keyboard_keys_stay_out_of_the_composer():
    screen = lines_to_screen([
        {"text": "+639543629194", "box": [80, 70, 420, 120]},
        {"text": "发消息", "box": [40, 1280, 220, 1330]},
        {"text": "?123", "box": [18, 1421, 95, 1461]},
        {"text": "空格", "box": [280, 1500, 460, 1560]},
        {"text": "中/英", "box": [20, 1500, 110, 1560]},
    ], 720, 1600)
    assert "发消息" in screen.composer
    assert "?123" not in screen.composer
    assert "空格" not in screen.composer
    assert "中/英" not in screen.composer
    assert "?123" not in "".join(screen.outgoing)


def test_real_norm_ocr_counts_as_chat_and_keeps_the_header():
    """6e3f_norm：顶栏在 y≈86–121，发消息在屏幕中下部。高屏也不能把顶栏当状态栏丢掉。"""
    from src.integrations.whatsapp_rpa.phone_acceptance import in_chat

    phone = "+639543629194"
    seats = {
        "02": [107, 1423, 222, 1471],
        "03": [106, 1487, 220, 1527],
        "04": [106, 1421, 223, 1472],
    }
    for height in (1600, 2400):
        for seat, box in seats.items():
            screen = lines_to_screen([
                {"text": phone, "box": [96, 86, 430, 121]},
                {"text": "发消息", "box": box},
            ], 1080, height)
            verdict = in_chat(screen, phone)
            assert verdict["ok"] is True, (seat, height, verdict, screen.header)
            assert verdict["header_plus"] is True
            assert verdict["header_matches_peer"] is True
            assert verdict["input_bar"] is True
            assert verdict["placeholder"] is True
            assert verdict["boxes"]["header"] == [96, 86, 430, 121]
            assert verdict["boxes"]["composer"] == box
            assert phone in screen.header
    stripped = PhoneScreen(
        header="",
        composer="发消息",
        ocr_lines=[
            {"text": phone, "box": [96, 86, 430, 121]},
            {"text": "发消息", "box": [107, 1423, 222, 1471]},
        ],
        shot_size=[1080, 2400],
    )
    assert in_chat(stripped, phone)["ok"] is True


def test_english_message_placeholder_is_in_chat():
    from src.integrations.whatsapp_rpa.phone_acceptance import in_chat
    from src.integrations.whatsapp_rpa.phone_send import composer_is_placeholder

    phone = "+639543629194"
    screen = lines_to_screen([
        {"text": phone, "box": [96, 86, 430, 121]},
        {"text": "Message", "box": [105, 1424, 265, 1476]},
    ], 1080, 1600)
    verdict = in_chat(screen, phone)
    assert composer_is_placeholder("Message") is True
    assert composer_is_placeholder("Mensaje") is True
    assert composer_is_placeholder("Type a message") is True
    assert verdict["ok"] is True
    assert verdict["placeholder"] is True
    assert verdict["boxes"]["composer"] == [105, 1424, 265, 1476]


def test_quote_block_above_a_tall_composer_is_tapped_at_its_corner():
    from src.integrations.whatsapp_rpa.phone_acceptance import input_bar_top, quote_close_point

    lines = [
        {"text": "你", "box": [52, 339, 87, 372]},
        {"text": "早上好呀今天想出去走走", "box": [111, 475, 470, 506]},
        {"text": "顺便把昨天的事说完", "box": [111, 520, 430, 552]},
    ]
    screen = lines_to_screen(
        [{"text": "+639543629194", "box": [96, 86, 430, 121]}, *lines],
        720, 1600,
    )
    assert "早上好" in screen.composer
    assert "顺便" in screen.composer
    top = input_bar_top(screen)
    assert top == 475
    point = quote_close_point(lines, 720, 1600, input_top=top)
    assert point == (662, 351)


def test_sent_history_is_not_searched_again(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import failed_history_texts
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key

    sentence = "对了，还没问你，平时怎么称呼你比较方便呀？"
    ledger = PhoneLedger(tmp_path / "ledger.db")
    key = sent_content_key("reply", PHONE_B, sentence)
    ledger.reserve(
        client_msg_id="old", phone=PHONE_B, account_id="a",
        source="reply", content_key=key, body=sentence,
    )
    ledger.finish("old", "failed", error="bubble_missing")
    ledger.mark_content_sent(key)
    assert failed_history_texts(ledger) == []


@pytest.mark.asyncio
async def test_peer_name_is_not_sent(tmp_path: Path):
    from scripts.wa_phone_loop_acceptance import bind_persona_decide
    from src.integrations.whatsapp_rpa.phone_loop import claims_peer_identity, persona_separation_note

    assert "对方叫顾嘉，你叫小界" in persona_separation_note("小界", "顾嘉")
    assert claims_peer_identity("叫我顾嘉就行", ["顾嘉"]) is True
    assert claims_peer_identity("叫我小界", ["顾嘉"]) is False
    assert claims_peer_identity("那你就喊我顾嘉，或者小界也行", ["顾嘉", "嘉嘉"]) is True
    assert claims_peer_identity("喊我小界，或者顾嘉也行", ["顾嘉"]) is True
    assert claims_peer_identity("那你就喊我小界吧", ["顾嘉"]) is False
    calls = []

    async def decide(**kwargs):
        calls.append(kwargs.get("agent_instruction") or "")
        if len(calls) == 1:
            return {"ok": True, "reply": "我是顾嘉，你呢"}
        return {"ok": True, "reply": "我是小界"}

    wrapped = bind_persona_decide(decide, "xiaojie", "小界", "顾嘉")
    result = await wrapped(
        account_id="seat", phone=PHONE_A, last_inbound="叫我嘉嘉", persona_id="",
    )
    assert result["reply"] == "我是小界"
    assert len(calls) == 2

    async def still_wrong(**kwargs):
        return {"ok": True, "reply": "叫我顾嘉"}

    skipped = await bind_persona_decide(still_wrong, "xiaojie", "小界", "顾嘉")(
        account_id="seat", phone=PHONE_A, last_inbound="你好", persona_id="",
    )
    assert skipped["ok"] is False
    assert skipped["reason"] == "peer_name"


def test_max_seconds_can_be_set_up_to_1800():
    from scripts.wa_phone_loop_acceptance import _clamp_max_seconds, parser

    assert _clamp_max_seconds(540) == 540
    assert _clamp_max_seconds(1800) == 1800
    assert _clamp_max_seconds(4000) == 1800
    args = parser().parse_args([
        "pair-chat",
        "--data-dir", "d", "--evidence-dir", "e",
        "--a-serial", "A", "--a-phone", "+1",
        "--b-serial", "B", "--b-phone", "+2",
        "--a-persona", "p1", "--b-persona", "p2",
        "--a-name", "小界", "--b-name", "顾嘉",
        "--max-seconds", "1800",
    ])
    assert args.a_name == "小界"
    assert args.b_name == "顾嘉"
    assert args.max_seconds == 1800


def test_b_read6_header_is_the_same_in_chat_decision():
    """b-read6 的顶栏和发消息必须让 in_chat 成立。空顶栏的界面树不能代替这次截图。"""
    from src.integrations.whatsapp_rpa.phone_acceptance import _dump_has_chat_header, in_chat

    phone = "+639543629194"
    screen = lines_to_screen([
        {"text": "+63 954 362 9..", "box": [198, 86, 435, 122]},
        {"text": "发消息", "box": [107, 1486, 222, 1529]},
    ], 1080, 1600)
    verdict = in_chat(screen, phone)
    assert verdict["ok"] is True
    assert verdict["header_matches_peer"] is True
    assert verdict["boxes"]["header"] == [198, 86, 435, 122]
    assert verdict["boxes"]["composer"] == [107, 1486, 222, 1529]
    assert _dump_has_chat_header(PhoneScreen(header="", composer="发消息")) is False
    assert _dump_has_chat_header(screen) is True


def test_delivered_bubble_counts_when_placeholder_and_text_match():
    from src.integrations.whatsapp_rpa.phone_send import (
        composer_still_has_draft, delivery_seen, send_confirmed, voice_permission_popup,
    )

    sentence = "这个点起床刚刚好，我这边也才醒。"
    screen = PhoneScreen(
        composer="发消息",
        outgoing=["更早的一句"],
        ocr_lines=[
            {"text": sentence, "box": [180, 1320, 640, 1388]},
            {"text": "发消息", "box": [107, 1486, 222, 1529]},
        ],
        shot_size=[720, 1600],
    )
    seen = delivery_seen(screen, sentence)
    assert seen["placeholder"] is True
    assert seen["new_bubble"] is True
    assert seen["text_match"] is True
    assert send_confirmed(screen, sentence) is True
    assert composer_still_has_draft(screen, sentence) is False
    popup = PhoneScreen(
        composer="",
        ocr_lines=[{"text": "要录制语音消息，请允许", "box": [80, 700, 640, 780]}],
        shot_size=[720, 1600],
    )
    assert voice_permission_popup(popup) is True


def test_garbage_heard_is_not_reliable():
    from src.integrations.whatsapp_rpa.phone_acceptance import _heard_reliable

    assert _heard_reliable("（ā更新") is False
    assert _heard_reliable("这个点起床刚刚好") is True


@pytest.mark.asyncio
async def test_back_from_keyboard_returns_with_wa_me_if_settings_open(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import prepare_phone

    phone = "+639543629194"

    class Seat:
        def __init__(self):
            self.phase = "keys"
            self.opens = 0
            self.backs = 0

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            if self.phase == "keys":
                return PhoneScreen(
                    header=phone, composer="发消息",
                    ocr_lines=[
                        {"text": phone, "box": [96, 86, 430, 121]},
                        {"text": "发消息", "box": [107, 900, 222, 960]},
                        {"text": "词", "box": [27, 1314, 88, 1365]},
                    ],
                    shot_size=[720, 1600],
                )
            if self.phase == "settings":
                return PhoneScreen(
                    header="语音输入", composer="",
                    ocr_lines=[{"text": "语音输入", "box": [42, 284, 276, 352]}],
                    shot_size=[720, 1600],
                )
            return PhoneScreen(
                header=phone, composer="发消息",
                ocr_lines=[
                    {"text": phone, "box": [96, 86, 430, 121]},
                    {"text": "发消息", "box": [107, 1486, 222, 1529]},
                ],
                shot_size=[720, 1600],
            )

        async def press_back(self):
            self.backs += 1
            self.phase = "settings"

        async def open_url(self, url):
            self.opens += 1
            assert "text=" not in url
            self.phase = "chat"

        async def press_home(self):
            raise AssertionError("回到会话只用 wa.me，不再回桌面")

    seat = Seat()
    loop = WaPhoneLoop(
        account_id="a", inbox=InboxStore(tmp_path / "inbox.db"), device=seat,
        decide_fn=None, cfg={"enabled": True},
        ledger=PhoneLedger(tmp_path / "ledger.db"), now_fn=lambda: 1.0,
    )
    result = await prepare_phone(loop, phone)
    assert seat.backs == 1
    assert seat.opens == 1
    assert result["chat"] is True
    assert result["composer"] == "发消息"


def test_keycap_below_1200_is_not_the_composer():
    screen = lines_to_screen([
        {"text": "+639543629194", "box": [96, 86, 430, 121]},
        {"text": "一段无关的长草稿还挂着引用", "box": [40, 860, 560, 920]},
        {"text": "词", "box": [27, 1314, 88, 1365]},
    ], 720, 1600)
    assert "词" not in screen.composer
    assert "长草稿" in screen.composer


def test_no_quote_means_no_close_tap_and_a_menu_is_not_a_quote():
    from src.integrations.whatsapp_rpa.phone_acceptance import menu_is_open, quote_close_point

    lines = [
        {"text": "+639543629194", "box": [96, 86, 430, 121]},
        {"text": "昨天那件事我想了想", "box": [40, 900, 480, 960]},
        {"text": "发消息", "box": [107, 1486, 222, 1529]},
    ]
    assert quote_close_point(lines, 1080, 1600, input_top=1486) is None
    menu = PhoneScreen(
        header="+639543629194", composer="发消息",
        ocr_lines=[{"text": "查看联系人", "box": [40, 200, 280, 260]}],
    )
    assert menu_is_open(menu) is True


@pytest.mark.asyncio
async def test_sent_history_does_not_touch_the_phone(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import reconcile_failed_history
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key

    sentence = "对了，还没问你，平时怎么称呼你比较方便呀？"
    ledger = PhoneLedger(tmp_path / "ledger.db")
    key = sent_content_key("reply", PHONE_B, sentence)
    ledger.reserve(
        client_msg_id="old", phone=PHONE_B, account_id="a",
        source="reply", content_key=key, body=sentence,
    )
    ledger.finish("old", "failed", error="bubble_missing")
    ledger.mark_content_sent(key)

    class Quiet:
        def __init__(self):
            self.reads = 0

        async def screencap(self):
            self.reads += 1
            return b"png"

        async def read_screen(self, png):
            self.reads += 1
            return PhoneScreen()

    device = Quiet()
    loop = WaPhoneLoop(
        account_id="a", inbox=InboxStore(tmp_path / "inbox.db"), device=device,
        decide_fn=None, cfg={"enabled": True}, ledger=ledger, now_fn=lambda: 1.0,
    )
    other = WaPhoneLoop(
        account_id="b", inbox=InboxStore(tmp_path / "inbox.db"), device=Quiet(),
        decide_fn=None, cfg={"enabled": True},
        ledger=PhoneLedger(tmp_path / "other.db"), now_fn=lambda: 1.0,
    )
    found = await reconcile_failed_history(loop, other, PHONE_A, PHONE_B)
    assert found == []
    assert device.reads == 0


@pytest.mark.asyncio
async def test_borrowed_name_draft_is_cleared_not_sent(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import prepare_phone

    phone = "+639543629194"

    class Seat:
        def __init__(self):
            self.clears = 0
            self.taps = 0
            self.phase = "draft"

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            if self.phase == "draft":
                return PhoneScreen(
                    header=phone, composer="叫我顾嘉就行",
                    ocr_lines=[
                        {"text": phone, "box": [96, 86, 430, 121]},
                        {"text": "叫我顾嘉就行", "box": [40, 1400, 360, 1460]},
                        {"text": "●", "box": [640, 1400, 690, 1460]},
                    ],
                    shot_size=[720, 1600],
                )
            return PhoneScreen(
                header=phone, composer="发消息",
                ocr_lines=[
                    {"text": phone, "box": [96, 86, 430, 121]},
                    {"text": "发消息", "box": [107, 1486, 222, 1529]},
                ],
                shot_size=[720, 1600],
            )

        async def clear_composer(self):
            self.clears += 1
            self.phase = "clean"

        async def tap_xy(self, x, y):
            self.taps += 1

        async def press_back(self):
            raise AssertionError("没有键盘，不应按返回")

    seat = Seat()
    loop = WaPhoneLoop(
        account_id="a", inbox=InboxStore(tmp_path / "inbox.db"), device=seat,
        decide_fn=None, cfg={"enabled": True},
        ledger=PhoneLedger(tmp_path / "ledger.db"), now_fn=lambda: 1.0,
    )
    loop.peer_names = ["顾嘉"]
    result = await prepare_phone(loop, phone)
    assert seat.clears == 1
    assert seat.taps == 0
    assert result["composer"] == "发消息"
    assert result["cleared"] is True


def test_keycap_w_is_not_the_composer():
    screen = lines_to_screen([
        {"text": "+639543629194", "box": [96, 86, 430, 121]},
        {"text": "一段无关的长草稿", "box": [40, 860, 520, 930]},
        {"text": "W", "box": [86, 1119, 139, 1161]},
        {"text": "E", "box": [150, 1119, 200, 1161]},
        {"text": "R", "box": [214, 1119, 264, 1161]},
    ], 720, 1600)
    assert "W" not in screen.composer
    assert "长草稿" in screen.composer


@pytest.mark.asyncio
async def test_keyboard_is_dismissed_once_before_clear(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import prepare_phone

    phone = "+639543629194"
    chat = PhoneScreen(
        header=phone, composer="发消息",
        ocr_lines=[
            {"text": phone, "box": [96, 86, 430, 121]},
            {"text": "发消息", "box": [107, 1423, 222, 1471]},
        ],
        shot_size=[1080, 2400],
    )
    dirty = PhoneScreen(
        header=phone, composer="一段无关的长草稿",
        ocr_lines=[
            {"text": phone, "box": [96, 86, 430, 121]},
            {"text": "一段无关的长草稿", "box": [40, 860, 520, 930]},
            {"text": "●", "box": [640, 860, 690, 920]},
            {"text": "W", "box": [86, 1119, 139, 1161]},
            {"text": "E", "box": [150, 1119, 200, 1161]},
        ],
        shot_size=[720, 1600],
    )

    class Seat:
        def __init__(self):
            self.phase = "keys"
            self.homes = 0
            self.opens = 0
            self.escapes = 0
            self.clears = 0
            self.backs = 0

        async def press_home(self):
            self.homes += 1

        async def open_url(self, url):
            self.opens += 1
            assert "text=" not in url

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            if self.phase == "keys":
                return dirty
            if self.phase == "draft":
                return PhoneScreen(
                    header=phone, composer="一段无关的长草稿",
                    ocr_lines=[
                        {"text": phone, "box": [96, 86, 430, 121]},
                        {"text": "一段无关的长草稿", "box": [40, 1400, 520, 1460]},
                        {"text": "●", "box": [640, 1400, 690, 1460]},
                    ],
                    shot_size=[720, 1600],
                )
            return chat

        async def dismiss_keyboard(self):
            self.escapes += 1

        async def clear_composer(self):
            self.clears += 1
            self.phase = "clean"

        async def press_back(self):
            self.backs += 1
            self.phase = "draft"

    seat = Seat()
    loop = WaPhoneLoop(
        account_id="seat", inbox=InboxStore(tmp_path / "inbox.db"), device=seat,
        decide_fn=None, cfg={"enabled": True},
        ledger=PhoneLedger(tmp_path / "ledger.db"), now_fn=lambda: 1.0,
    )
    result = await prepare_phone(loop, phone)
    assert seat.homes == 0
    assert seat.opens == 0
    assert seat.escapes == 0
    assert seat.backs == 1
    assert seat.clears == 1
    assert result["in_chat"]["placeholder"] is True
    assert result["in_chat"]["header_matches_peer"] is True
    assert result["in_chat"]["boxes"]["composer"] == [107, 1423, 222, 1471]


def test_raised_keyboard_keeps_the_real_composer_and_drops_keycaps():
    """键盘弹起后输入栏在 y≈930，「C」在 y≈1340，不能当成草稿。"""
    from src.integrations.whatsapp_rpa.phone_acceptance import chat_page_ready, quote_close_point

    screen = lines_to_screen([
        {"text": "+639543629194", "box": [80, 70, 420, 120]},
        {"text": "发消息", "box": [106, 911, 225, 963]},
        {"text": "C", "box": [270, 1326, 309, 1361]},
        {"text": "V", "box": [320, 1326, 359, 1361]},
        {"text": "B", "box": [370, 1326, 409, 1361]},
        {"text": "N", "box": [420, 1326, 459, 1361]},
        {"text": "M", "box": [470, 1326, 509, 1361]},
    ], 720, 1600)
    assert screen.composer.strip() == "发消息"
    assert "C" not in screen.composer
    assert "C" not in "".join(screen.outgoing + screen.incoming)
    desktop = PhoneScreen(
        header="全部笔记", composer="GMO",
        ocr_lines=[
            {"text": "全部笔记", "box": [40, 200, 280, 260]},
            {"text": "GMO", "box": [80, 900, 180, 960]},
        ],
        shot_size=[720, 1600],
    )
    settings = PhoneScreen(
        header="隐私", composer="",
        ocr_lines=[{"text": "隐私", "box": [40, 300, 180, 360]}],
        shot_size=[720, 1600],
    )
    assert chat_page_ready(desktop, "+639543629194") is False
    assert chat_page_ready(settings, "+639543629194") is False
    point = quote_close_point(
        [
            {"text": "发消息", "box": [106, 911, 225, 963]},
            {"text": "X", "box": [640, 820, 670, 852]},
        ],
        720, 1600, input_top=911,
    )
    assert point == (655, 836)


def test_split_bubble_matches_the_failed_sentence():
    from src.integrations.whatsapp_rpa.phone_acceptance import screen_matches_target

    sentence = "对了，还没问你，平时怎么称呼你比较方便呀？"
    screen = PhoneScreen(
        header=PHONE_B,
        composer="发消息",
        ocr_lines=[
            {"text": "对了，还没问你，平时怎么称呼你比较方便", "box": [135, 822, 645, 856]},
            {"text": "呀？", "box": [132, 868, 279, 903]},
        ],
    )
    assert screen_matches_target(screen, sentence) is True


@pytest.mark.asyncio
async def test_failed_history_is_found_within_three_screens(tmp_path: Path):
    from src.integrations.whatsapp_rpa.phone_acceptance import reconcile_failed_history
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key

    sentence = "对了，还没问你，平时怎么称呼你比较方便呀？"
    visible = PhoneScreen(
        header=PHONE_B,
        composer="发消息",
        ocr_lines=[
            {"text": "对了，还没问你，平时怎么称呼你比较方便", "box": [135, 822, 645, 856]},
            {"text": "呀？", "box": [132, 868, 279, 903]},
        ],
    )
    blank = PhoneScreen(header=PHONE_B, composer="发消息")

    class Pager:
        def __init__(self, pages):
            self.pages = list(pages)
            self.index = 0
            self.scrolls = 0
            self.taps = 0

        async def open_url(self, url):
            return None

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            return self.pages[min(self.index, len(self.pages) - 1)]

        async def scroll_older(self):
            self.scrolls += 1
            self.index += 1

        async def scroll_to_latest(self):
            return None

        async def press_back(self):
            return None

        async def tap_send(self):
            self.taps += 1

    store = InboxStore(tmp_path / "inbox.db")
    ledger = PhoneLedger(tmp_path / "ledger.db")
    key = sent_content_key("reply", PHONE_B, sentence)
    slot = ledger.reserve(
        client_msg_id="old-0735", phone=PHONE_B, account_id="seat-a",
        source="reply", content_key=key, body=sentence,
    )
    assert slot["reserved"] is True
    ledger.finish("old-0735", "failed", error="bubble_missing")
    device = Pager([blank, visible, blank])
    loop_a = WaPhoneLoop(
        account_id="seat-a", inbox=store, device=device, decide_fn=None,
        cfg={"enabled": True}, ledger=ledger, now_fn=lambda: 1.0,
    )
    loop_b = WaPhoneLoop(
        account_id="seat-b", inbox=store, device=Pager([blank]), decide_fn=None,
        cfg={"enabled": True}, ledger=PhoneLedger(tmp_path / "b.db"), now_fn=lambda: 1.0,
    )
    found = await reconcile_failed_history(loop_a, loop_b, PHONE_A, PHONE_B)
    assert sentence in found
    assert device.scrolls == 1
    assert device.scrolls <= 2
    assert device.taps == 0
    assert ledger.has_sent_content(key) is True


@pytest.mark.asyncio
async def test_normalize_ui_backs_out_of_the_menu_and_confirms_placeholder(tmp_path: Path):
    from scripts.wa_phone_loop_acceptance import async_main, parser

    class Phone:
        def __init__(self):
            self.phase = "menu"
            self.backs = 0
            self.homes = 0
            self.clears = 0
            self.taps = 0
            self.urls = []
            self.quote = True

        async def press_home(self):
            self.homes += 1

        async def open_url(self, url):
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            if self.phase == "menu":
                return PhoneScreen(
                    header="", composer="编辑",
                    ocr_lines=[{"text": "编辑", "box": [138, 1406, 216, 1447]}],
                    shot_size=[720, 1600],
                )
            if self.phase == "draft":
                lines = [
                    {"text": PHONE_A, "box": [80, 80, 400, 130]},
                    {"text": "无关的长草稿还挂着引用", "box": [40, 1360, 520, 1410]},
                    {"text": "●", "box": [640, 1360, 690, 1420]},
                    {"text": "?123", "box": [18, 1421, 95, 1461]},
                ]
                if self.quote:
                    lines.append({"text": "X", "box": [640, 1480, 670, 1510]})
                return PhoneScreen(
                    header=PHONE_A, composer="无关的长草稿还挂着引用",
                    ocr_lines=lines, shot_size=[720, 1600],
                )
            return PhoneScreen(
                header=PHONE_A, composer="发消息",
                ocr_lines=[
                    {"text": PHONE_A, "box": [80, 80, 400, 130]},
                    {"text": "发消息", "box": [40, 1500, 220, 1550]},
                ],
                shot_size=[720, 1600],
            )

        async def press_back(self):
            self.backs += 1
            self.phase = "draft"

        async def dismiss_keyboard(self):
            return None

        async def tap_xy(self, x, y):
            self.quote = False

        async def clear_composer(self):
            self.clears += 1
            self.phase = "cleared"

        async def tap_send(self):
            self.taps += 1

    phone = Phone()
    args = parser().parse_args([
        "normalize-ui",
        "--data-dir", str(tmp_path / "data"),
        "--evidence-dir", str(tmp_path / "evidence"),
        "--adb-ssh-host", "hub176",
        "--serial", "SERIAL_A",
        "--phone", PHONE_A,
    ])
    report = await async_main(args, devices={"a": phone})
    assert phone.homes == 1
    assert phone.backs == 2
    assert phone.clears == 1
    assert phone.taps == 0
    assert phone.urls and "wa.me/" in phone.urls[0] and "text=" not in phone.urls[0]
    assert report["cleared"] is True
    assert report["composer"] == "发消息"
    assert report["tapped"] is False
    assert any(row["step"] == "open_chat" and isinstance(row["ms"], int) for row in report["timings"])
    assert any(row["step"] == "clear_composer" for row in report["timings"])


@pytest.mark.asyncio
async def test_desktop_and_settings_are_not_cleaned(tmp_path: Path):
    from scripts.wa_phone_loop_acceptance import async_main, parser

    class Stuck:
        def __init__(self, kind):
            self.kind = kind
            self.homes = 0
            self.backs = 0
            self.clears = 0
            self.taps = 0
            self.urls = []

        async def press_home(self):
            self.homes += 1

        async def open_url(self, url):
            self.urls.append(url)

        async def screencap(self):
            return b"png"

        async def read_screen(self, png):
            if self.kind == "desktop":
                return PhoneScreen(
                    header="全部笔记", composer="GMO",
                    ocr_lines=[
                        {"text": "全部笔记", "box": [40, 180, 280, 240]},
                        {"text": "GMO", "box": [90, 880, 180, 940]},
                    ],
                    shot_size=[720, 1600],
                )
            return PhoneScreen(
                header="隐私", composer="",
                ocr_lines=[{"text": "隐私", "box": [48, 280, 180, 340]}],
                shot_size=[720, 1600],
            )

        async def press_back(self):
            self.backs += 1

        async def tap_xy(self, x, y):
            self.taps += 1

        async def clear_composer(self):
            self.clears += 1

        async def tap_send(self):
            self.taps += 1

    for kind in ("desktop", "settings"):
        phone = Stuck(kind)
        args = parser().parse_args([
            "normalize-ui",
            "--data-dir", str(tmp_path / kind / "data"),
            "--evidence-dir", str(tmp_path / kind / "evidence"),
            "--adb-ssh-host", "hub176",
            "--serial", "SERIAL_A",
            "--phone", PHONE_A,
        ])
        report = await async_main(args, devices={"a": phone})
        assert phone.homes == 2
        assert phone.backs == 0
        assert phone.clears == 0
        assert phone.taps == 0
        assert len(phone.urls) == 2
        assert all("wa.me/" in url and "text=" not in url for url in phone.urls)
        assert report["ok"] is False
        assert report["reason"] == "not_in_chat"
        assert report["cleared"] is False


@pytest.mark.asyncio
async def test_clear_composer_long_presses_then_selects_all():
    calls = []

    def runner(argv):
        remote = argv[-1]
        calls.append(remote)
        if "uiautomator" in remote:
            return 137, b"Killed", ""
        if "screencap" in remote:
            return 0, _png(8, 8), ""
        if "wm" in remote and "size" in remote:
            return 0, b"Physical size: 1080x2400\n", ""
        return 0, b"", ""

    device = SshAdbPhoneDevice(
        "hub176", "SERIAL_A",
        lambda image: PhoneScreen(ocr_lines=[{"text": "全选", "box": [100, 1400, 180, 1450]}]),
        runner=runner,
    )
    await device.clear_composer()
    joined = "\n".join(calls)
    assert "swipe" in joined and "800" in joined
    assert "input tap 140 1425" in joined
    assert "keyevent 67" in joined
    assert "keyevent 123" not in joined


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
