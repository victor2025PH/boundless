"""唯一发送口：wa.me 预填，截图核对，再点发送。

不读取界面层级，也不把中文逐字敲进输入法。
输入框对不上、发出后看不到己方气泡，都记 failed，调用方不得自动重发。
``dry_run`` 只记账，不打开链接，也不记成 sent。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


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


_CURSOR_TAIL = re.compile(r"(?:\s*-\s*)+$")
_CLOCK_ANY = re.compile(
    r"(?:凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|夜间)?\s*\d{1,2}\s*[:：]\s*\d{2}"
)
_CHECK_MARK = re.compile(r"[√✓✔☑]")
_DATE_LINE = re.compile(
    r"^(?:今天|昨天|前天|Today|Yesterday|"
    r"星期[一二三四五六日天]|周[一二三四五六日天]|"
    r"\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2})$",
    re.IGNORECASE,
)


def _strip_cursor(line: str) -> str:
    """行尾单独的「-」是输入框光标，不是正文。"""
    return _CURSOR_TAIL.sub("", str(line or "")).strip()


def is_screen_chrome(text: str) -> bool:
    """时间、日期、勾号、加密提示。这些不是消息，也不是输入框正文。"""
    raw = str(text or "").strip()
    cleaned = re.sub(r"^[□■•·|\-\s]+|[□■•·|\-\s]+$", "", raw)
    if not cleaned:
        return True
    if _DATE_LINE.match(cleaned) or _CLOCK_ANY.fullmatch(cleaned):
        return True
    if re.fullmatch(r"[\s\d:：.。√✓✔☑]+", cleaned):
        return True
    folded = cleaned.casefold()
    if "端到端加密" in cleaned or "end-to-end" in folded:
        return True
    if _CLOCK_ANY.search(cleaned) and _CHECK_MARK.search(cleaned):
        return True
    return False


def composer_body(text: str) -> str:
    """去掉全部空白、换行和行尾光标。系统提示和加密/时间条不参与比对。"""
    parts = []
    for raw in str(text or "").splitlines() or [""]:
        line = _strip_cursor(_norm(raw))
        if not line or _is_hint_line(line) or is_screen_chrome(line):
            continue
        parts.append(_without_hints(line) or line)
    return re.sub(r"\s+", "", "".join(parts))


def composer_matches(composer: str, expected: str) -> bool:
    """要发的全文必须和输入框里的连续文字逐字一致。

    换行是折行，行尾「-」是光标。系统提示可以多一行，字不能少、不能改。
    """
    want = composer_body(expected)
    if not want:
        return False
    return composer_body(composer) == want


def screen_blob(screen: "PhoneScreen") -> str:
    parts = [
        screen.header, screen.composer, screen.last_incoming,
        *list(screen.outgoing or []), *list(screen.notes or []),
    ]
    return "\n".join(str(part or "") for part in parts)


def screen_is_rate_limited(screen: "PhoneScreen") -> bool:
    blob = screen_blob(screen).casefold()
    return any(marker.casefold() in blob for marker in _RATE_LIMIT_MARKERS)


# wa.me 打开后会先停在「发送给…」联系人列表或「正在搜索…」弹窗。
# 这些画面不是聊天页，输入框 OCR 会读成「O」或空串。
_TRANSIENT_MARKERS = (
    "发送给",
    "正在搜索",
    "选择联系人",
    "搜索联系人",
    "send to",
    "searching",
    "select contact",
    "choose contact",
)


def screen_is_transient(screen: "PhoneScreen") -> bool:
    """联系人选择页或搜索弹窗。要继续等，不能拿来核对输入框。"""
    blob = screen_blob(screen).casefold()
    return any(marker.casefold() in blob for marker in _TRANSIENT_MARKERS)


def _composer_still_arriving(composer: str, expected: str) -> bool:
    """空串、单个噪声字、占位提示，或正文还没打全，都继续等。"""
    text = composer_body(composer)
    if not text or len(text) <= 1:
        return True
    want = composer_body(expected)
    return bool(want) and want.startswith(text) and text != want


def composer_settled(composer: str, expected: str) -> bool:
    """输入框里已经是要发的那句中文。空串、对不上的单个噪声字、只有占位提示都不算就绪。"""
    if not str(expected or "").strip():
        return True
    text = composer_body(composer)
    if not text:
        return False
    if len(text) <= 1 and text != composer_body(expected):
        return False
    return composer_matches(composer, expected)


def send_confirmed(screen: "PhoneScreen", expected: str) -> bool:
    """输入框已经空了，并且任一气泡里的文字和刚发的那句逐字一致。"""
    if composer_matches(getattr(screen, "composer", ""), expected):
        return False
    for text in list(getattr(screen, "outgoing", []) or []) + list(getattr(screen, "incoming", []) or []):
        if composer_matches(text, expected):
            return True
    return False


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
    incoming: List[str] = field(default_factory=list)
    last_incoming: str = ""
    notes: List[str] = field(default_factory=list)
    ocr_lines: List[dict] = field(default_factory=list)
    source: str = ""
    ui_dump: str = ""
    shot_size: List[int] = field(default_factory=list)
    ocr_size: List[int] = field(default_factory=list)


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


async def wait_for_chat(
    device: Any,
    phone: str,
    *,
    expected: str = "",
    timeout: Optional[float] = None,
    interval: Optional[float] = None,
) -> Dict[str, Any]:
    """wa.me 打开之后轮询，直到顶栏是目标号码且输入框就绪。

    联系人选择页和搜索弹窗会继续等。超时后按最后一帧说明停在哪，
    不把「O」或空串报成正文对不上。
    """
    import asyncio
    import time

    from src.integrations.whatsapp_rpa.phone_identity import header_matches, header_needs_profile

    if timeout is None:
        raw_limit = getattr(device, "open_timeout", None)
        limit = 25.0 if raw_limit is None else float(raw_limit)
    else:
        limit = float(timeout)
    if interval is None:
        raw_step = getattr(device, "open_interval", None)
        step = 0.8 if raw_step is None else float(raw_step)
    else:
        step = float(interval)
    clock = getattr(device, "clock", None) or time.monotonic
    sleep = getattr(device, "sleep_fn", None) or asyncio.sleep
    started = float(clock())
    last: Optional[PhoneScreen] = None
    polls = 0
    profile_checked = False
    profile_confirmed = False
    while True:
        png = await device.screencap()
        screen = await device.read_screen(png)
        if not isinstance(screen, PhoneScreen):
            screen = PhoneScreen()
        last = screen
        polls += 1
        transient = screen_is_transient(screen)
        header_ok = profile_confirmed or header_matches(screen.header, phone=phone)
        if (
            not transient and not header_ok and not profile_checked
            and header_needs_profile(screen.header, phone=phone)
            and (not expected or not _composer_still_arriving(screen.composer, expected))
            and getattr(device, "tap_header", None) is not None
        ):
            profile_checked = True
            confirmed = await confirm_phone_on_profile(device, phone)
            if not confirmed:
                return {
                    "ready": False, "status": "failed", "reason": "header_mismatch",
                    "polls": polls, "header": screen.header, "composer": screen.composer,
                    "match": False, "profile_confirmed": False,
                }
            profile_confirmed = True
            png = await device.screencap()
            screen = await device.read_screen(png)
            if not isinstance(screen, PhoneScreen):
                screen = PhoneScreen()
            last = screen
            polls += 1
            transient = screen_is_transient(screen)
            header_ok = True
        if not transient and header_ok and screen_is_rate_limited(screen):
            return {
                "ready": False, "status": "throttled", "reason": "wa_rate_limit",
                "polls": polls, "header": screen.header, "composer": screen.composer,
                "match": False,
            }
        if not transient and header_ok and composer_settled(screen.composer, expected):
            matched = (not expected) or composer_matches(screen.composer, expected)
            return {
                "ready": True, "status": "ready", "reason": "",
                "polls": polls, "header": screen.header, "composer": screen.composer,
                "match": matched, "profile_confirmed": profile_confirmed,
            }
        if (
            not transient and header_ok and expected
            and not _composer_still_arriving(screen.composer, expected)
        ):
            return {
                "ready": False, "status": "failed", "reason": "composer_mismatch",
                "polls": polls, "header": screen.header, "composer": screen.composer,
                "match": False,
            }
        if float(clock()) - started >= limit or polls >= 40:
            return {
                "ready": False, "status": "failed",
                "reason": _wait_timeout_reason(screen, phone, expected),
                "polls": polls, "header": screen.header, "composer": screen.composer,
                "match": False,
            }
        await sleep(step)


async def confirm_phone_on_profile(device: Any, phone: str) -> bool:
    """点顶栏进联系人资料，读完整号码，再返回聊天页。对不上就不是这个会话。"""
    from src.integrations.whatsapp_rpa.phone_identity import profile_shows_phone

    await device.tap_header()
    try:
        screen = await device.read_screen(await device.screencap())
    finally:
        back = getattr(device, "press_back", None)
        if back is not None:
            await back()
    if not isinstance(screen, PhoneScreen):
        return False
    return profile_shows_phone(screen, phone)


def _wait_timeout_reason(screen: PhoneScreen, phone: str, expected: str) -> str:
    from src.integrations.whatsapp_rpa.phone_identity import header_matches
    if screen_is_transient(screen):
        return "contact_picker"
    if not header_matches(screen.header, phone=phone):
        return "header_timeout"
    text = str(screen.composer or "").strip()
    if expected and (not text or len(text) <= 1):
        return "composer_not_ready"
    if expected and not composer_matches(screen.composer, expected):
        return "composer_mismatch"
    return "chat_not_ready"


async def _replace_draft_if_needed(device: Any, url: str, body: str) -> None:
    """预填前先看输入框。已经是这句就不再打开链接，以免叠上一遍。

    框里是别的草稿就先清空，再预填。空框直接预填。
    """
    current = ""
    try:
        screen = await device.read_screen(await device.screencap())
    except Exception:
        screen = None
    if isinstance(screen, PhoneScreen):
        current = str(screen.composer or "")
    if composer_matches(current, body):
        return
    if composer_body(current):
        clear = getattr(device, "clear_composer", None)
        if clear is not None:
            await clear()
    await device.open_url(url)


async def deliver_via_wa_me(
    device: PhoneDevice,
    phone: str,
    text: str,
    *,
    dry_run: bool = False,
) -> SendOutcome:
    """预填、等到聊天页、核对中文、点发送、再核对气泡。选择页上不点。"""
    from src.integrations.whatsapp_rpa.phone_identity import wa_me_url

    body = str(text or "").strip()
    url = wa_me_url(phone, body)
    if not url or not body:
        return SendOutcome(status="failed", reason="bad_target")
    if dry_run:
        return SendOutcome(status="dry_run", reason="dry_run", url=url)

    await _replace_draft_if_needed(device, url, body)
    dismiss = getattr(device, "dismiss_keyboard", None)
    if dismiss is not None:
        await dismiss()
    ready = await wait_for_chat(device, phone, expected=body)
    if not ready.get("ready"):
        try:
            await device.press_back()
        except Exception:
            pass
        status = "throttled" if ready.get("reason") == "wa_rate_limit" else "failed"
        return SendOutcome(status=status, reason=str(ready.get("reason") or "chat_not_ready"), url=url)

    await device.tap_send()
    after = await device.read_screen(await device.screencap())
    if send_confirmed(after, body):
        return SendOutcome(status="sent", reason="", tapped=True, url=url)
    # 原文还在输入框、也没有新气泡：这一下没点中发送按钮，重新定位再点一次。
    # 输入框已经空了就绝不补发，气泡可能只是没被读出来。
    if composer_matches(after.composer, body):
        await device.tap_send()
        after = await device.read_screen(await device.screencap())
        if send_confirmed(after, body):
            return SendOutcome(status="sent", reason="", tapped=True, url=url)
    return SendOutcome(
        status="failed", reason="bubble_missing", tapped=True, url=url,
    )


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

    async def dismiss_keyboard(self) -> None:
        from src.integrations.line_rpa import adb_helpers as adb

        adb.input_keyevent(self._serial, "111")

    async def tap_send(self) -> None:
        from src.integrations.line_rpa import adb_helpers as adb

        adb.input_keyevent(self._serial, "111")
        size = adb.screen_size(self._serial) or (1080, 2400)
        sw, sh = int(size[0]), int(size[1])
        adb.input_tap(self._serial, int(round(sw * 0.907)), int(round(sh * 0.904)))

    async def tap_header(self) -> None:
        from src.integrations.line_rpa import adb_helpers as adb

        size = adb.screen_size(self._serial) or (720, 1600)
        sw, sh = int(size[0]), int(size[1])
        adb.input_tap(self._serial, int(sw * 0.32), int(sh * 0.09))

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
