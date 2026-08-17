# Forwarded message → auto lookup user ID and run credit check
from aiogram import Router, F
from aiogram.types import Message

from bot.api_client import check, can_view_flow_get
from bot.i18n import t, get_locale
from bot.keyboards import inline_result_actions, inline_mycredit_actions, inline_invite_btn
from bot.format_result import format_check_result, append_report_footer

router = Router()


@router.message(F.forward_from)
async def handle_forward_from_user(msg: Message):
    """User forwarded a message from someone; we have forward_from.id → run check."""
    if not msg.forward_from:
        return
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    target_id = msg.forward_from.id
    sent = await msg.answer(t("forward_checking", lang=locale))
    try:
        data = await check(user_id=target_id, querier_tg_id=msg.from_user.id if msg.from_user else None)
    except Exception:
        await sent.edit_text(t("check_error", lang=locale))
        return

    tg_id = data.get("tg_id")
    f = msg.forward_from
    display_name = " ".join(filter(None, [getattr(f, "first_name", "") or "", getattr(f, "last_name", "") or ""])).strip() or None
    body = format_check_result(data, locale, display_name=display_name)
    body = await append_report_footer(body, tg_id, locale)
    querier_id = msg.from_user.id if msg.from_user else None
    if tg_id:
        is_self = querier_id is not None and tg_id == querier_id
        if not is_self:
            body = body + "\n\n" + t("result_alert_tip", lang=locale)
        if is_self:
            reply_mk = inline_mycredit_actions(tg_id, locale)
        else:
            try:
                can_view = await can_view_flow_get(tg_id)
            except Exception:
                can_view = False
            reply_mk = inline_result_actions(tg_id, locale, source="forward", can_view_flow=can_view)
    else:
        reply_mk = None
    await sent.edit_text(
        body,
        reply_markup=reply_mk,
        parse_mode="Markdown",
    )


@router.message(F.forward_sender_name)
async def handle_forward_hidden(msg: Message):
    """Forward from a user who hid their account — show invite button."""
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    text = t("forward_hidden_invite", lang=locale) + "\n\n" + t("invite_click_here", lang=locale)
    await msg.answer(text, parse_mode="Markdown", reply_markup=inline_invite_btn(locale))
