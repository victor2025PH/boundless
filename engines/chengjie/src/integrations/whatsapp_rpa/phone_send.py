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


# 输入框里常见的系统提示。整行是提示时丢掉；正文里夹着提示时也剥掉再比。
_COMPOSER_HINTS = (
    "type a message",
    "输入消息",
    "写消息",
    "发消息",
    "双击以使用表情",
    "点按以使用表情",
    "tap to use emoji",
)

# WhatsApp 自己弹出的限速提示。看到就先停，不靠条数去掐正在聊的朋友。
_RATE_LIMIT_MARKERS = (
    "too many messages",
    "sending messages too fast",
    "try again later",
    "couldn't send",
    "could not send",
    "发送过于频繁",
    "发得太快",
    "消息发得太快",
    "请稍后再试",
    "暂时无法发送",
    "尝试次数过多",
)


def _is_hint_line(line: str) -> bool:
    """整行是系统提示才丢掉。正文里出现「发消息」这三个字时保留。"""
    cleaned = _norm(line).casefold()
    if not cleaned:
        return True
    return any(cleaned == hint or cleaned.startswith(hint) for hint in _COMPOSER_HINTS)


def _without_hints(line: str) -> str:
    text = line
    for hint in _COMPOSER_HINTS:
        text = re.sub(re.escape(hint), " ", text, flags=re.IGNORECASE)
    return _norm(text)


def composer_matches(composer: str, expected: str) -> bool:
    """要发的全文必须和输入框里的正文一致。系统提示可以多一行，字不能少、不能改。"""
    want = _norm(expected)
    if not want:
        return False
    raw_lines = str(composer or "").splitlines() or [str(composer or "")]
    kept = []
    for raw in raw_lines:
        line = _norm(raw)
        if line == want or _without_hints(line) == want:
            return True
        if _is_hint_line(line):
            continue
        kept.append(line)
    return _norm(" ".join(kept)) == want


def screen_blob(screen: "PhoneScreen") -> str:
    parts = [screen.header, screen.composer, screen.last_incoming, *list(screen.outgoing or [])]
    return "\n".join(str(part or "") for part in parts)


def screen_is_rate_limited(screen: "PhoneScreen") -> bool:
    blob = screen_blob(screen).casefold()
    return any(marker.casefold() in blob for marker in _RATE_LIMIT_MARKERS)


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
    if screen_is_rate_limited(before):
        try:
            await device.press_back()
        except Exception:
            pass
        return SendOutcome(
            status="throttled", reason="wa_rate_limit", tapped=False, url=url,
        )
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
    """用本机 Tesseract 读顶栏、输入框和画面下半部的文字。

    没装 OCR 或截图不是 PNG 时返回空画面。发送口看到空输入框会失败，不会盲发。

    安装（推荐，不访问外网模型）：

    - Debian/Ubuntu: ``sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim``
    - Python 包: ``pip install pytesseract Pillow``（本仓库的 line_rpa 截图识字已走这条）
    - 检查: ``tesseract --list-langs`` 里要有 ``chi_sim``

    中文界面短句用 chi_sim 够用。若顶栏人名经常认错，再把 ``read_screen_fn``
    换成收件箱那套 PaddleOCR（``ppocr``）服务，不要改发送口本身。
    不把识图请求发到云端视觉模型，避免发送前多一次外网依赖。
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
