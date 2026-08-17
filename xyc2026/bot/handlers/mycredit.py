from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from bot.api_client import mycredit, share_create, notify_on_checked_set, sharecard_get
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import inline_mycredit_actions, reply_main
from bot.format_result import format_check_result, append_report_footer, format_sharecard

router = Router()

# Match /mycredit or any localized "My credit" button text
MYCREDIT_TRIGGERS = ["/mycredit"] + get_texts_for_key("btn_my_credit")
SHARECARD_TRIGGERS = ["/sharecard"] + get_texts_for_key("btn_sharecard")


@router.message(F.text.in_(MYCREDIT_TRIGGERS))
async def cmd_mycredit(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    sent = await msg.answer(t("mycredit_loading", lang=locale))
    try:
        data = await mycredit(tg_id=msg.from_user.id)
    except Exception:
        await sent.edit_text(t("mycredit_error", lang=locale))
        await msg.answer(t("menu_hint", lang=locale), reply_markup=reply_main(locale))
        return
    tg_id = data.get("tg_id")
    body = format_check_result(data, locale)
    body = await append_report_footer(body, tg_id, locale)
    notify_on = data.get("notify_on_checked", True)
    await sent.edit_text(
        body,
        reply_markup=inline_mycredit_actions(tg_id, locale, notify_on_checked=notify_on) if tg_id else None,
        parse_mode="Markdown",
    )
    await msg.answer(t("menu_hint", lang=locale), reply_markup=reply_main(locale))


@router.message(F.text.in_(SHARECARD_TRIGGERS))
async def cmd_sharecard(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    try:
        data = await sharecard_get(msg.from_user.id)
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    text = format_sharecard(data, locale)
    await msg.answer(text, reply_markup=reply_main(locale))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("notify_off_"))
async def cb_notify_off(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    try:
        target_tg_id = int(cb.data.replace("notify_off_", ""))
    except ValueError:
        return
    if cb.from_user.id != target_tg_id:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        await notify_on_checked_set(cb.from_user.id, False)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    await cb.message.answer(t("notify_turned_off", lang=locale))
    try:
        data = await mycredit(tg_id=cb.from_user.id)
        body = format_check_result(data, locale)
        await cb.message.edit_reply_markup(
            reply_markup=inline_mycredit_actions(cb.from_user.id, locale, notify_on_checked=False),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("notify_on_"))
async def cb_notify_on(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    try:
        target_tg_id = int(cb.data.replace("notify_on_", ""))
    except ValueError:
        return
    if cb.from_user.id != target_tg_id:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        await notify_on_checked_set(cb.from_user.id, True)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    await cb.message.answer(t("notify_turned_on", lang=locale))
    try:
        await cb.message.edit_reply_markup(
            reply_markup=inline_mycredit_actions(cb.from_user.id, locale, notify_on_checked=True),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sharecard")
async def cb_sharecard(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        data = await sharecard_get(cb.from_user.id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    text = format_sharecard(data, locale)
    await cb.message.answer(text, reply_markup=reply_main(locale))


@router.callback_query(F.data == "show_my_report")
async def cb_show_my_report(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    await cb.message.answer(t("share_loading", lang=locale))
    try:
        data = await share_create(tg_id=cb.from_user.id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    link = data.get("link", "")
    hours = data.get("expires_in_hours", 24)
    instructions = t("share_link_instructions", lang=locale, hours=hours)
    label = t("share_link_label", lang=locale)
    link_safe = link.replace("&", "&amp;")
    await cb.message.answer(
        f"🔗 {instructions}\n\n<b>{label}</b>\n{link_safe}",
        parse_mode="HTML",
        reply_markup=reply_main(locale),
    )
