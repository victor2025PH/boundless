"""唯一发送口：wa.me 预填，截图核对，再点发送。

不读取界面层级，也不把中文逐字敲进输入法。
输入框对不上、发出后看不到己方气泡，都记 failed，调用方不得自动重发。
``dry_run`` 只记账，不打开链接，也不记成 sent。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def composer_matches(composer: str, expected: str) -> bool:
    """输入框里的字必须和要发的全文一致。多字、少字都不发。"""
    want = _norm(expected)
    got = _norm(composer)
    return bool(want) and got == want


def bubble_visible(outgoing: List[str], expected: str) -> bool:
    """发出后的截图里要能看到这句己方文字。"""
    want = _norm(expected)
    if not want:
        return False
    for bubble in outgoing or []:
        got = _norm(bubble)
        if got == want or want in got:
            return True
    return False


@dataclass
class PhoneScreen:
    header: str = ""
    composer: str = ""
    outgoing: List[str] = field(default_factory=list)
    last_incoming: str = ""


class PhoneDevice(Protocol):
    async def open_url(self, url: str) -> None: ...

    async def screencap(self) -> bytes: ...

    async def tap_send(self) -> None: ...

    async def read_screen(self, png: bytes) -> PhoneScreen: ...

    async def press_back(self) -> None: ...


@dataclass
class SendOutcome:
    status: str
    reason: str = ""
    tapped: bool = False
    url: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "tapped": self.tapped,
            "url": self.url,
            "delivered": self.status == "sent",
        }


async def deliver_via_wa_me(
    device: PhoneDevice,
    phone: str,
    text: str,
    *,
    dry_run: bool = False,
) -> SendOutcome:
    """预填、核对、点发送、再核对气泡。任何一步对不上都不再点。"""
    from src.integrations.whatsapp_rpa.phone_identity import wa_me_url

    body = str(text or "").strip()
    url = wa_me_url(phone, body)
    if not url or not body:
        return SendOutcome(status="failed", reason="bad_target")
    if dry_run:
        return SendOutcome(status="dry_run", reason="dry_run", url=url)

    await device.open_url(url)
    before = await device.read_screen(await device.screencap())
    if not composer_matches(before.composer, body):
        try:
            await device.press_back()
        except Exception:
            pass
        return SendOutcome(
            status="failed", reason="composer_mismatch", tapped=False, url=url,
        )

    await device.tap_send()
    after = await device.read_screen(await device.screencap())
    if not bubble_visible(after.outgoing, body):
        return SendOutcome(
            status="failed", reason="bubble_missing", tapped=True, url=url,
        )
    return SendOutcome(status="sent", reason="", tapped=True, url=url)


def _png_of(image) -> bytes:
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _ocr_line(png: bytes) -> str:
    from src.integrations.line_rpa.screen_ocr import ocr_png_bytes
    for line in (ocr_png_bytes(png, "chi_sim+eng") or "").splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def ocr_phone_screen(png: bytes) -> PhoneScreen:
    """用本机 OCR 读顶栏、输入框和画面下半部的文字。

    没装 OCR 或截图不是 PNG 时返回空画面。发送口看到空输入框会失败，不会盲发。
    """
    if not png or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return PhoneScreen()
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
        width, height = image.size
        header = _ocr_line(_png_of(image.crop((0, 0, width, max(1, int(height * 0.12))))))
        composer = _ocr_line(_png_of(image.crop(
            (0, int(height * 0.86), width, height))))
        lower = image.crop((int(width * 0.30), int(height * 0.18), width, int(height * 0.84)))
        from src.integrations.line_rpa.screen_ocr import ocr_png_bytes
        outgoing = [
            line.strip()
            for line in (ocr_png_bytes(_png_of(lower), "chi_sim+eng") or "").splitlines()
            if line.strip()
        ]
        return PhoneScreen(header=header, composer=composer, outgoing=outgoing)
    except Exception:
        return PhoneScreen()


class AdbPhoneDevice:
    """真机适配器。单元测试不调用它的 adb 方法。

    默认用 ``ocr_phone_screen`` 读截图。读不到字就当没对上，发送失败。
    """

    def __init__(self, serial: str,
                 read_screen_fn: Optional[Callable[[bytes], PhoneScreen]] = None) -> None:
        self._serial = str(serial or "").strip()
        self._read_screen_fn = read_screen_fn or ocr_phone_screen

    async def open_url(self, url: str) -> None:
        from src.integrations.line_rpa import adb_helpers as adb
        from src.integrations.whatsapp_rpa.phone_identity import view_intent_args

        adb.run_adb(view_intent_args(url), serial=self._serial or None, timeout=20.0)

    async def screencap(self) -> bytes:
        from src.integrations.line_rpa import adb_helpers as adb

        data, _err, code = adb.run_adb_binary(
            ["exec-out", "screencap", "-p"], serial=self._serial or None, timeout=20.0,
        )
        if code != 0:
            return b""
        return data or b""

    async def tap_send(self) -> None:
        from src.integrations.line_rpa import adb_helpers as adb

        size = adb.screen_size(self._serial) or (720, 1600)
        sw, sh = int(size[0]), int(size[1])
        adb.input_tap(self._serial, int(sw * 0.94), int(sh * 0.956))

    async def read_screen(self, png: bytes) -> PhoneScreen:
        if self._read_screen_fn is None:
            return PhoneScreen()
        screen = self._read_screen_fn(png)
        if isinstance(screen, PhoneScreen):
            return screen
        return PhoneScreen()

    async def press_back(self) -> None:
        from src.integrations.line_rpa import adb_helpers as adb

        adb.input_keyevent(self._serial, "KEYCODE_BACK")
