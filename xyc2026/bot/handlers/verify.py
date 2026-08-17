# 验真报告：按钮 + 直接输入验真码；第四阶段：支持粘贴报告全文做 content_hash 篡改校验
import hashlib
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from bot.api_client import report_verify
from bot.i18n import t, get_locale, get_texts_for_key

router = Router()

# 验真码格式：TC- 加 6 位十六进制（不区分大小写）
VERIFY_CODE_PATTERN = re.compile(r"^TC-[0-9A-Fa-f]{6}$")
VERIFY_CODE_IN_TEXT = re.compile(r"TC-[0-9A-Fa-f]{6}")


def _get_verify_code(text: str) -> str | None:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


async def _do_verify_and_reply(msg: Message, code: str, locale: str, content_hash: str | None = None):
    """执行验真并回复结果。content_hash 可选，用于校验正文是否与存档一致。"""
    try:
        data = await report_verify(code.strip(), content_hash=content_hash)
    except Exception:
        await msg.answer(t("verify_invalid", lang=locale), parse_mode="Markdown")
        return
    if data.get("valid"):
        created_at = data.get("created_at") or "—"
        out = t("verify_valid", lang=locale, created_at=created_at)
        if data.get("content_match") is True:
            out += "\n\n" + t("verify_content_match_yes", lang=locale)
        elif data.get("content_match") is False:
            out += "\n\n" + t("verify_content_match_no", lang=locale)
        await msg.answer(out, parse_mode="Markdown")
    else:
        await msg.answer(t("verify_invalid", lang=locale), parse_mode="Markdown")


@router.message(F.text.func(lambda t: (t or "").strip() and VERIFY_CODE_PATTERN.match((t or "").strip())))
async def msg_verify_code(msg: Message):
    """用户直接输入验真码（如 TC-78A2B9），无需输入 /verify。"""
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    code = (msg.text or "").strip()
    await _do_verify_and_reply(msg, code, locale)


@router.message(F.text.func(lambda t: (t or "").strip() and len((t or "").strip()) > 50 and VERIFY_CODE_IN_TEXT.search((t or "").strip())))
async def msg_verify_pasted_report(msg: Message):
    """第四阶段：用户粘贴报告全文，提取验真码并做 content_hash 校验。"""
    text = (msg.text or "").strip()
    match = VERIFY_CODE_IN_TEXT.search(text)
    if not match:
        return
    code = match.group(0)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    await _do_verify_and_reply(msg, code, locale, content_hash=content_hash)


@router.message(F.text.func(lambda t: (t or "").strip().lower().startswith("/verify")))
async def cmd_verify(msg: Message):
    """兼容：/verify TC-78A2B9 仍可用；仅 /verify 时提示用按钮或直接输入；可粘贴全文做篡改校验。"""
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    raw = (msg.text or "").strip()
    match = VERIFY_CODE_IN_TEXT.search(raw)
    if not match:
        await msg.answer(t("verify_enter_prompt", lang=locale))
        return
    code = match.group(0)
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest() if len(raw) > 50 else None
    await _do_verify_and_reply(msg, code, locale, content_hash=content_hash)


@router.message(F.text.in_(get_texts_for_key("btn_verify_report")))
async def msg_btn_verify_report(msg: Message):
    """主菜单点击「验真报告」：提示直接输入验真码。"""
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    await msg.answer(t("verify_enter_prompt", lang=locale))


@router.callback_query(F.data == "ask_verify_report")
async def cb_ask_verify_report(cb: CallbackQuery):
    """报告下方点击「验真报告」：提示直接输入验真码。"""
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    await cb.message.answer(t("verify_enter_prompt", lang=locale))
