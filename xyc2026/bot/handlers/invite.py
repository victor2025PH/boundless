# Invite link: button or /invite; when they open link & message, report goes to querier
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from bot.api_client import invite_create, InviteLinkConnectionError
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import reply_main

router = Router()

INVITE_MINUTES = 15


async def _send_invite_link(chat_id: int, user_id: int, locale: str, bot):
    try:
        data = await invite_create(querier_tg_id=user_id)
    except InviteLinkConnectionError:
        await bot.send_message(chat_id, t("error_invite_connection", lang=locale), reply_markup=reply_main(locale))
        return
    except Exception:
        await bot.send_message(chat_id, t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    link = data.get("link", "")
    minutes = data.get("expires_in_minutes", INVITE_MINUTES)
    instructions = t("invite_instructions", lang=locale, minutes=minutes)
    link_label = t("invite_link_label", lang=locale, minutes=minutes)
    # Use HTML so URL with underscore (e.g. t.me/xyc2026_bot) doesn't break Markdown
    link_safe = link.replace("&", "&amp;")
    await bot.send_message(
        chat_id,
        f"🔗 {instructions}\n\n<b>{link_label}</b>\n{link_safe}",
        parse_mode="HTML",
        reply_markup=reply_main(locale),
    )


@router.message(F.text.in_(["/invite"] + get_texts_for_key("invite_btn")))
async def cmd_invite(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    await _send_invite_link(msg.chat.id, msg.from_user.id, locale, msg.bot)


@router.callback_query(F.data == "get_invite")
async def cb_get_invite(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    await _send_invite_link(cb.message.chat.id, cb.from_user.id, locale, cb.bot)
