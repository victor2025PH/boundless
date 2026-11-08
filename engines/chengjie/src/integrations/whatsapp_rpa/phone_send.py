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


# 气泡右下角的发送时间，以及已读勾。正文句号要留下。
_BUBBLE_TAIL = re.compile(
    r"(?:\s*\d{1,2}\s*[:：]\s*\d{2}\s*)?[√✓✔☑]{1,3}\s*$"
    r"|"
    r"(?:(?<=\s)|(?<=[。．.!?！？]))"
    r"(?:(?:凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|夜间)\s*)?"
    r"\d{1,2}\s*[:：]\s*\d{2}\s*$"
)


def strip_bubble_tail(text: str) -> str:
    """去掉气泡尾部的时间戳和已读勾，例如「7:07 ✓✓」。"""
    cleaned = str(text or "")
    while True:
        nxt = _BUBBLE_TAIL.sub("", cleaned).strip()
        if nxt == cleaned:
            return cleaned.strip()
        cleaned = nxt


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
        # 时间和勾在句尾，是气泡角标，正文要留下。时间和勾夹在句子中间，才是系统条。
        stripped = strip_bubble_tail(cleaned)
        if (
            stripped
            and stripped != cleaned
            and not _CLOCK_ANY.search(stripped)
            and not _CHECK_MARK.search(stripped)
        ):
            return False
        return True
    return False


_PUNCT_FOLD = str.maketrans({
    "，": ",", "。": ".", "？": "?", "！": "!",
    "：": ":", "；": ";",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")",
})
_PUNCT_DROP = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def compare_key(text: str) -> str:
    """NFKC、全半角标点对齐，再去掉标点和空白。问号全角半角不再判成两句。"""
    import unicodedata
    folded = unicodedata.normalize("NFKC", composer_body(text)).translate(_PUNCT_FOLD)
    return _PUNCT_DROP.sub("", folded)


def composer_body(text: str) -> str:
    """去掉全部空白、换行和行尾光标。系统提示和加密/时间条不参与比对。"""
    parts = []
    for raw in str(text or "").splitlines() or [""]:
        line = strip_bubble_tail(_strip_cursor(_norm(raw)))
        if not line or _is_hint_line(line) or is_screen_chrome(line):
            continue
        parts.append(_without_hints(line) or line)
    return re.sub(r"\s+", "", "".join(parts))


def composer_is_placeholder(text: str) -> bool:
    """输入框还是「发消息」这类占位，正文没写进去。"""
    if composer_body(text):
        return False
    raw = str(text or "")
    folded = raw.casefold()
    return any(hint in folded or hint in raw for hint in _COMPOSER_HINTS)


def composer_matches(composer: str, expected: str) -> bool:
    """去掉标点和空白后一致，或相似度至少 0.9。全角「？」和半角「?」算同一句。"""
    return texts_close(composer, expected)


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


def frame_boxes(screen: "PhoneScreen") -> tuple:
    """按从上到下的顺序记下 OCR 框。两次截屏用它判断画面有没有还在滑。"""
    signature = []
    for line in getattr(screen, "ocr_lines", None) or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        box = line.get("box") or []
        if not text or len(box) < 4:
            continue
        signature.append((text, tuple(int(round(float(v))) for v in box[:4])))
    return tuple(signature)


def screen_is_sliding(screen: "PhoneScreen") -> bool:
    """正文左缘超过屏幕宽度的 0.3，是聊天页还在横向滑入，不能拿来核对。"""
    left = float(getattr(screen, "composer_left", 0) or 0)
    size = getattr(screen, "shot_size", None) or []
    width = float(size[0]) if size else 0.0
    if left <= 0 or width <= 0:
        return False
    return left > width * 0.3


def screen_is_transient(screen: "PhoneScreen") -> bool:
    """联系人选择页或搜索弹窗。要继续等，不能拿来核对输入框。"""
    blob = screen_blob(screen).casefold()
    return any(marker.casefold() in blob for marker in _TRANSIENT_MARKERS)


def _composer_still_arriving(composer: str, expected: str) -> bool:
    """空串、单个噪声字、占位提示，或正文还没打全，都继续等。

    标点已经对齐之后仍对不上，而且不是对方句子的前缀，就不要再等。
    """
    text = compare_key(composer)
    if not text or len(text) <= 1:
        return True
    if texts_close(composer, expected):
        return False
    want = compare_key(expected)
    return bool(want) and want.startswith(text) and text != want


def composer_settled(composer: str, expected: str) -> bool:
    """输入框里已经是要发的那句中文。空串、对不上的单个噪声字、只有占位提示都不算就绪。"""
    if not str(expected or "").strip():
        return True
    text = compare_key(composer)
    if not text:
        return False
    if len(text) <= 1 and not texts_close(composer, expected):
        return False
    return composer_matches(composer, expected)


def texts_close(observed: str, expected: str) -> bool:
    """去掉标点和空白后一致、互相包含，或相似度至少 0.9。"""
    from difflib import SequenceMatcher
    got = compare_key(observed)
    want = compare_key(expected)
    if not got or not want:
        return False
    if got == want:
        return True
    shorter, longer = (got, want) if len(got) <= len(want) else (want, got)
    if len(shorter) >= 8 and shorter in longer:
        return True
    return SequenceMatcher(None, got, want).ratio() >= 0.9


def is_bubble_boundary(text: str) -> bool:
    """时间戳或单独的已读勾。它是上一个气泡的结束，不是下一句的开头。"""
    raw = str(text or "").strip()
    cleaned = re.sub(r"^[□■•·|\-\s]+|[□■•·|\-\s]+$", "", raw)
    if not cleaned:
        return False
    if _DATE_LINE.match(cleaned) or _CLOCK_ANY.fullmatch(cleaned):
        return True
    return re.fullmatch(r"[\s\d:：.。√✓✔☑]+", cleaned) is not None and (
        _CLOCK_ANY.search(cleaned) is not None or _CHECK_MARK.search(cleaned) is not None
    )


def send_confirmed(screen: "PhoneScreen", expected: str) -> bool:
    """输入框不再是待发正文，底部己方气泡和待发对得上，才算已经发出。

    占位「发消息」表示输入框已经空出来。底部气泡去空白后包含待发，
    或相似度至少 0.9，都算这一句已经在屏幕上。
    """
    if composer_matches(getattr(screen, "composer", ""), expected):
        return False
    outgoing = list(getattr(screen, "outgoing", []) or [])
    incoming = list(getattr(screen, "incoming", []) or [])
    bottom = outgoing[-1] if outgoing else (incoming[-1] if incoming else "")
    return texts_close(bottom, expected)


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
    bubbles: List[dict] = field(default_factory=list)
    composer_left: float = 0.0
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
    timings: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = {
            "status": self.status,
            "reason": self.reason,
            "tapped": self.tapped,
            "url": self.url,
            "delivered": self.status == "sent",
        }
        if self.timings:
            data["timings"] = dict(self.timings)
        return data


def _wait_result(ready: bool, status: str, reason: str, polls: int, screen: "PhoneScreen",
                 profile_confirmed: bool, started: float, clock, stable_tries: int,
                 match: bool = False) -> Dict[str, Any]:
    elapsed = max(0.0, float(clock()) - float(started))
    return {
        "ready": ready, "status": status, "reason": reason,
        "polls": polls, "header": screen.header, "composer": screen.composer,
        "match": match, "profile_confirmed": profile_confirmed,
        "timings": {
            "wait_ms": int(elapsed * 1000),
            "polls": polls,
            "stability_tries": stable_tries,
        },
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
    stable_tries = 0
    placeholder_seen = 0
    while True:
        png = await device.screencap()
        screen = await device.read_screen(png)
        if not isinstance(screen, PhoneScreen):
            screen = PhoneScreen()
        last = screen
        polls += 1
        jittery = screen_is_sliding(screen)
        if frame_boxes(screen) and not jittery and stable_tries < 3:
            await sleep(0.4)
            png2 = await device.screencap()
            screen2 = await device.read_screen(png2)
            if not isinstance(screen2, PhoneScreen):
                screen2 = PhoneScreen()
            polls += 1
            last = screen2
            if screen_is_sliding(screen2) or frame_boxes(screen2) != frame_boxes(screen):
                stable_tries += 1
                screen = screen2
                if stable_tries < 3:
                    continue
            else:
                screen = screen2
        elif jittery:
            stable_tries += 1
            if stable_tries < 3:
                await sleep(0.4)
                continue
        transient = screen_is_transient(screen)
        header_ok = profile_confirmed or header_matches(screen.header, phone=phone)
        if not transient and header_ok and expected and composer_is_placeholder(screen.composer):
            placeholder_seen += 1
            if placeholder_seen >= 2:
                return _wait_result(
                    False, "failed", "composer_not_ready", polls, screen,
                    profile_confirmed, started, clock, stable_tries,
                )
            await sleep(min(step, 0.4))
            continue
        placeholder_seen = 0
        if (
            not transient and not header_ok and not profile_checked
            and header_needs_profile(screen.header, phone=phone)
            and (not expected or not _composer_still_arriving(screen.composer, expected))
            and getattr(device, "tap_header", None) is not None
        ):
            profile_checked = True
            confirmed = await confirm_phone_on_profile(device, phone)
            if not confirmed:
                return _wait_result(
                    False, "failed", "header_mismatch", polls, screen, False,
                    started, clock, stable_tries,
                )
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
            return _wait_result(
                False, "throttled", "wa_rate_limit", polls, screen, profile_confirmed,
                started, clock, stable_tries,
            )
        if not transient and header_ok and composer_settled(screen.composer, expected):
            matched = (not expected) or composer_matches(screen.composer, expected)
            return _wait_result(
                True, "ready", "", polls, screen, profile_confirmed,
                started, clock, stable_tries, match=matched,
            )
        if (
            not transient and header_ok and expected
            and not _composer_still_arriving(screen.composer, expected)
        ):
            return _wait_result(
                False, "failed", "composer_mismatch", polls, screen, profile_confirmed,
                started, clock, stable_tries,
            )
        if float(clock()) - started >= limit or polls >= 12:
            return _wait_result(
                False, "failed", _wait_timeout_reason(screen, phone, expected),
                polls, screen, profile_confirmed, started, clock, stable_tries,
            )
        await sleep(step)


def _still_on_profile(screen: "PhoneScreen", phone: str) -> bool:
    """资料页上有完整号码，输入框是空的。聊天页的截断顶栏对不上完整号码。"""
    from src.integrations.whatsapp_rpa.phone_identity import profile_shows_phone

    if not isinstance(screen, PhoneScreen):
        return False
    if composer_body(getattr(screen, "composer", "")):
        return False
    return profile_shows_phone(screen, phone)


def _profile_opened(screen: "PhoneScreen", phone: str) -> bool:
    """顶栏点下去之后，输入框空了、上面是一串号码，就是资料页已经打开。"""
    if not isinstance(screen, PhoneScreen):
        return False
    if composer_body(getattr(screen, "composer", "")):
        return False
    if _still_on_profile(screen, phone):
        return True
    header = str(getattr(screen, "header", "") or "")
    return header.strip().startswith("+") and sum(ch.isdigit() for ch in header) >= 8


async def _read_once(device: Any) -> "PhoneScreen":
    try:
        screen = await device.read_screen(await device.screencap())
    except Exception:
        return PhoneScreen()
    if not isinstance(screen, PhoneScreen):
        return PhoneScreen()
    return screen


async def confirm_phone_on_profile(device: Any, phone: str) -> bool:
    """点顶栏进资料页，读完整号码，再退回聊天。没看到资料页就不按返回。

    返回键如果抢在资料页出现之前，会把聊天页退掉，资料页随后盖上来就停在那里。
    看到资料页才返回；返回后如果还在资料页，再按一次。
    """
    import time

    from src.integrations.whatsapp_rpa.phone_identity import profile_shows_phone

    await device.tap_header()
    clock = getattr(device, "clock", None) or time.monotonic
    sleep = getattr(device, "sleep_fn", None) or __import__("asyncio").sleep
    started = float(clock())
    screen = PhoneScreen()
    seen = False
    while True:
        screen = await _read_once(device)
        if _profile_opened(screen, phone):
            seen = True
            break
        if float(clock()) - started >= 2.0:
            break
        await sleep(0.3)
    opened = _profile_opened(screen, phone)
    confirmed = opened and profile_shows_phone(screen, phone)
    if not opened:
        return False
    back = getattr(device, "press_back", None)
    if back is None:
        return confirmed
    for _ in range(2):
        await back()
        landed = await _read_once(device)
        if composer_body(getattr(landed, "composer", "")) or not _profile_opened(landed, phone):
            break
    return confirmed


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


async def _peek(device: Any) -> "PhoneScreen":
    try:
        screen = await device.read_screen(await device.screencap())
    except Exception:
        return PhoneScreen()
    return screen if isinstance(screen, PhoneScreen) else PhoneScreen()


def _on_chat(screen: "PhoneScreen", phone: str) -> bool:
    from src.integrations.whatsapp_rpa.phone_identity import header_matches
    return header_matches(screen.header, phone=phone) and not screen_is_transient(screen)


def _needs_link(text: str) -> bool:
    """汉字不在当前页逐字敲。这些手机的剪贴板后台写不进去，回到 wa.me。"""
    return not str(text or "").isascii()


async def _keyboard_ready(device: Any) -> bool:
    probe = getattr(device, "unicode_keyboard_available", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False


async def _place_text(device: Any, url: str, body: str, phone: str, stay: bool) -> str:
    """中文用 wa.me 覆盖输入框。先把别的草稿清掉，避免叠进联系人选择页。

    已经是这句就不再打开。英文留在当前页打字。
    剪贴板和 ADBKeyBoard 只在设备上确实有的时候才作为备用。
    """
    screen = await _peek(device)
    if _on_chat(screen, phone) and send_confirmed(screen, body):
        return "already"
    on_chat = _on_chat(screen, phone)
    if stay and not on_chat and not _needs_link(body):
        back = getattr(device, "press_back", None)
        if back is not None:
            try:
                await back()
            except Exception:
                pass
        screen = await _peek(device)
        on_chat = _on_chat(screen, phone)
        if not on_chat:
            return "off_chat"
    current = str(screen.composer or "") if isinstance(screen, PhoneScreen) else ""
    if composer_matches(current, body) and (on_chat or not stay):
        return "held" if on_chat else ""
    if _needs_link(body):
        if composer_body(current) and not composer_is_placeholder(current):
            clear = getattr(device, "clear_composer", None)
            if clear is not None:
                await clear()
        await device.open_url(url)
        return "wa_me"
    if on_chat or stay:
        if composer_body(current):
            clear = getattr(device, "clear_composer", None)
            if clear is not None:
                await clear()
        typer = getattr(device, "prefill_text", None)
        if typer is not None:
            await typer(body)
            return "typed"
        if stay:
            return "need_type"
    await device.open_url(url)
    return ""


async def _switch_prefill(device: Any, url: str, body: str, placed: str) -> str:
    """占位「发消息」还在，就换另一种预填，只试一次。"""
    if placed == "wa_me":
        if await _keyboard_ready(device):
            typer = getattr(device, "prefill_unicode", None)
            if typer is not None:
                await typer(body)
                return "keyboard"
        return "gave_up"
    await device.open_url(url)
    return "wa_me"


async def deliver_via_wa_me(
    device: PhoneDevice,
    phone: str,
    text: str,
    *,
    dry_run: bool = False,
    stay: bool = False,
) -> SendOutcome:
    """第一段可以打开 wa.me。已经在聊天页，或同一轮的后续段，只在当前页预填。"""
    import time

    from src.integrations.whatsapp_rpa.phone_identity import wa_me_url

    body = str(text or "").strip()
    url = wa_me_url(phone, body)
    if not url or not body:
        return SendOutcome(status="failed", reason="bad_target")
    if dry_run:
        return SendOutcome(status="dry_run", reason="dry_run", url=url)

    started = time.monotonic()
    placed = await _place_text(device, url, body, phone, stay)
    retried = False
    if placed == "already":
        return SendOutcome(
            status="sent", reason="already_visible", tapped=False, url="",
            timings={"place": placed, "total_ms": int((time.monotonic() - started) * 1000)},
        )
    if placed in {"need_type", "off_chat"}:
        return SendOutcome(
            status="failed",
            reason="contact_picker" if placed == "off_chat" else "prefill_missing",
            tapped=False, url="",
            timings={"place": placed, "total_ms": int((time.monotonic() - started) * 1000)},
        )
    dismiss = getattr(device, "dismiss_keyboard", None)
    if dismiss is not None:
        await dismiss()
    limit = 8.0 if placed in {"typed", "held", "keyboard"} or stay else None
    ready = await wait_for_chat(device, phone, expected=body, timeout=limit)
    if (
        not ready.get("ready")
        and ready.get("reason") == "composer_not_ready"
        and not retried
    ):
        retried = True
        switched = await _switch_prefill(device, url, body, placed)
        placed = switched
        if switched != "gave_up":
            ready = await wait_for_chat(device, phone, expected=body, timeout=8.0)
    timings = dict(ready.get("timings") or {})
    timings["place"] = placed
    timings["retried"] = retried
    timings["total_ms"] = int((time.monotonic() - started) * 1000)
    used_url = "" if placed in {"typed", "held", "keyboard"} else url
    if placed == "gave_up":
        return SendOutcome(
            status="failed", reason="composer_not_ready", tapped=False,
            url=url, timings=timings,
        )
    if not ready.get("ready"):
        if placed == "":
            try:
                await device.press_back()
            except Exception:
                pass
        status = "throttled" if ready.get("reason") == "wa_rate_limit" else "failed"
        return SendOutcome(
            status=status, reason=str(ready.get("reason") or "chat_not_ready"),
            url=used_url, timings=timings,
        )

    await device.tap_send()
    after = await device.read_screen(await device.screencap())
    if send_confirmed(after, body):
        timings["total_ms"] = int((time.monotonic() - started) * 1000)
        return SendOutcome(status="sent", reason="", tapped=True, url=used_url, timings=timings)
    # 原文还在输入框、也没有新气泡：这一下没点中发送按钮，重新定位再点一次。
    # 输入框已经空了就绝不补发，气泡可能只是没被读出来。
    if composer_matches(after.composer, body):
        await device.tap_send()
        after = await device.read_screen(await device.screencap())
        if send_confirmed(after, body):
            timings["total_ms"] = int((time.monotonic() - started) * 1000)
            return SendOutcome(status="sent", reason="", tapped=True, url=used_url, timings=timings)
    timings["total_ms"] = int((time.monotonic() - started) * 1000)
    return SendOutcome(
        status="failed", reason="bubble_missing", tapped=True, url=used_url, timings=timings,
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
