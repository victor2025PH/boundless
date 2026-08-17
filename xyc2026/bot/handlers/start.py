from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import reply_main, inline_result_actions
from bot.api_client import invite_consume, check, share_view, activity_log
from bot.format_result import format_check_result
from bot.handlers.check import CheckStates

router = Router()


def _get_start_arg(text: str | None) -> str | None:
    if not text or not text.strip().startswith("/start"):
        return None
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


@router.message(F.text.func(lambda t: t and t.strip().startswith("/start")))
async def cmd_start(msg: Message):
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    arg = _get_start_arg(msg.text)
    # Share flow: /start share_XXXX -> viewer opens link and sees owner's report (one-time)
    if arg and arg.startswith("share_"):
        token = arg
        try:
            data = await share_view(token)
        except Exception:
            data = None
        if not data:
            await msg.answer(t("share_expired", lang=locale), reply_markup=reply_main(locale))
            return
        body = format_check_result(data, locale)
        await msg.answer(body, parse_mode="Markdown", reply_markup=reply_main(locale))
        return
    # Invite flow: /start inv_XXXX -> target user clicked link; send their report to querier
    if arg and arg.startswith("inv_"):
        token = arg
        try:
            data = await invite_consume(token)
        except Exception:
            data = None
        if not data:
            await msg.answer(t("invite_expired", lang=locale), reply_markup=reply_main(locale))
            return
        querier_tg_id = data.get("querier_tg_id")
        if not msg.from_user or not querier_tg_id:
            return
        target_id = msg.from_user.id
        try:
            await activity_log(target_id, "invite_clicked")
        except Exception:
            pass
        try:
            report_data = await check(user_id=target_id, querier_tg_id=querier_tg_id)
        except Exception:
            await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
            return
        from bot.format_result import append_report_footer
        display_name = " ".join(filter(None, [msg.from_user.first_name or "", msg.from_user.last_name or ""])).strip()
        body = format_check_result(report_data, locale, display_name=display_name or None)
        body = await append_report_footer(body, target_id, locale)
        title = t("invite_report_title_to_querier", lang=locale)
        tip = t("result_alert_tip", lang=locale)
        full_report = f"{title}\n\n{body}\n\n{tip}"
        try:
            from bot.api_client import can_view_flow_get
            can_view = await can_view_flow_get(target_id)
        except Exception:
            can_view = False
        try:
            await msg.bot.send_message(
                querier_tg_id,
                full_report,
                parse_mode="Markdown",
                reply_markup=inline_result_actions(target_id, locale, source="invite", can_view_flow=can_view),
            )
        except Exception:
            await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
            return
        await msg.answer(t("invite_done_to_target", lang=locale), reply_markup=reply_main(locale))
        return
    # Normal /start：首屏即带主键盘，所有功能入口可见（P0）
    await msg.answer(
        t("welcome", lang=locale),
        reply_markup=reply_main(locale),
        parse_mode="Markdown",
    )


@router.message(F.text.in_(get_texts_for_key("btn_query")))
async def msg_query_btn(msg: Message, state: FSMContext):
    """User tapped '立即查询' -> prompt to type ID/username/wallet directly."""
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    await state.set_state(CheckStates.waiting_input)
    await msg.answer(t("check_usage_simple", lang=locale), parse_mode="Markdown", reply_markup=reply_main(locale))


@router.callback_query(F.data == "query_now")
async def cb_query_now(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    locale = get_locale(cb.from_user.language_code if cb.from_user else None)
    await state.set_state(CheckStates.waiting_input)
    if cb.message:
        await cb.message.answer(
            t("check_usage_simple", lang=locale),
            parse_mode="Markdown",
            reply_markup=reply_main(locale),
        )
