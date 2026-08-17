import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter

from bot.api_client import check, report_breakdown, can_view_flow_get
from bot.i18n import t, get_locale, translate_tip
from bot.keyboards import inline_result_actions, inline_mycredit_actions, inline_invite_btn
from bot.format_result import format_check_result, append_report_footer

router = Router()


class CheckStates(StatesGroup):
    waiting_input = State()


def _parse_input(text: str) -> tuple[int | None, str | None, str | None]:
    """Parse raw input to (user_id, username, wallet). One of them is set."""
    raw = (text or "").strip()
    if not raw:
        return None, None, None
    if raw.isdigit():
        return int(raw), None, None
    if raw.startswith("@"):
        return None, raw[:128], None
    return None, None, raw[:128]


async def _do_check_and_reply(msg: Message, locale: str, user_id: int | None, username: str | None, wallet: str | None, querier_tg_id: int | None = None):
    sent = await msg.answer(t("check_loading", lang=locale))
    try:
        data = await check(user_id=user_id, username=username, wallet=wallet, querier_tg_id=querier_tg_id)
    except Exception:
        await sent.edit_text(t("check_error", lang=locale))
        return
    # Enrich with V2.0 breakdown when we have a resolved user (举报分类 / 风险来源占比)
    if data.get("tg_id"):
        try:
            b = await report_breakdown(user_id=data["tg_id"])
            for key in ("risk_factor_breakdown", "report_by_category", "last_7d_reports", "last_30d_reports"):
                if key in b:
                    data[key] = b[key]
        except Exception:
            pass
    api_message = data.get("message") or ""
    if api_message and not data.get("tg_id") and not data.get("blacklist"):
        if "Username not found" in api_message or "private" in api_message.lower():
            display_msg = t("msg_username_not_found", lang=locale) + "\n\n" + t("invite_click_here", lang=locale)
        elif "No record" in api_message and "wallet" in api_message.lower():
            display_msg = t("msg_wallet_no_record", lang=locale)
        else:
            display_msg = api_message
        await sent.edit_text(
            f"ℹ️ {display_msg}\n\n{t('result_disclaimer', lang=locale)}",
            parse_mode="Markdown",
            reply_markup=inline_invite_btn(locale),
        )
        return
    tg_id = data.get("tg_id")
    body = format_check_result(data, locale)
    body = await append_report_footer(body, tg_id, locale)
    if tg_id:
        if querier_tg_id is not None and tg_id == querier_tg_id:
            reply_mk = inline_mycredit_actions(tg_id, locale)
        else:
            body = body + "\n\n" + t("result_alert_tip", lang=locale)
            try:
                can_view = await can_view_flow_get(tg_id)
            except Exception:
                can_view = False
            reply_mk = inline_result_actions(tg_id, locale, source="query", can_view_flow=can_view)
    else:
        reply_mk = None
    await sent.edit_text(body, reply_markup=reply_mk, parse_mode="Markdown")


def get_check_arg(text: str) -> str | None:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


# After clicking "立即查询", user is in waiting_input; next message = ID/username/wallet (no command)
@router.message(StateFilter(CheckStates.waiting_input), F.text)
async def handle_direct_check_input(msg: Message, state: FSMContext):
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    text = (msg.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await msg.answer(t("check_cancelled", lang=locale))
        return
    if not text:
        await msg.answer(t("check_input_empty", lang=locale))
        return
    await state.clear()
    user_id, username, wallet = _parse_input(text)
    await _do_check_and_reply(msg, locale, user_id, username, wallet, querier_tg_id=msg.from_user.id if msg.from_user else None)


@router.message(F.text.regexp(re.compile(r"^/check(\s|@\w+)?", re.I)))
async def cmd_check(msg: Message):
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    arg = get_check_arg(msg.text or "")
    if not arg:
        await msg.answer(t("check_usage", lang=locale), parse_mode="Markdown")
        return
    user_id, username, wallet = _parse_input(arg)
    await _do_check_and_reply(msg, locale, user_id, username, wallet, querier_tg_id=msg.from_user.id if msg.from_user else None)


@router.callback_query(F.data == "ask_show_report")
async def cb_ask_show_report(cb: CallbackQuery):
    await cb.answer()
    locale = get_locale(cb.from_user.language_code if cb.from_user else None)
    await cb.message.answer(t("msg_ask_show_report", lang=locale), parse_mode="Markdown")


@router.callback_query(F.data.startswith("forward_report_"))
async def cb_forward_report(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    try:
        target_tg_id = int(cb.data.replace("forward_report_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        data = await check(user_id=target_tg_id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    if not data.get("tg_id"):
        await cb.message.answer(t("check_error", lang=locale))
        return
    try:
        b = await report_breakdown(user_id=target_tg_id)
        for key in ("risk_factor_breakdown", "report_by_category", "last_7d_reports", "last_30d_reports"):
            if key in b:
                data[key] = b[key]
    except Exception:
        pass
    body = format_check_result(data, locale)
    body = await append_report_footer(body, target_tg_id, locale)
    is_self = cb.from_user and target_tg_id == cb.from_user.id
    if not is_self:
        body = body + "\n\n" + t("result_alert_tip", lang=locale)
    if is_self:
        reply_mk = inline_mycredit_actions(target_tg_id, locale)
    else:
        try:
            can_view = await can_view_flow_get(target_tg_id)
        except Exception:
            can_view = False
        reply_mk = inline_result_actions(target_tg_id, locale, source="forward", can_view_flow=can_view)
    await cb.message.answer(body, parse_mode="Markdown", reply_markup=reply_mk)


@router.callback_query(F.data.startswith("resend_report_"))
async def cb_resend_report(cb: CallbackQuery):
    """P1: Resend report to user (for saving or forwarding)."""
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    try:
        target_tg_id = int(cb.data.replace("resend_report_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        data = await check(user_id=target_tg_id, querier_tg_id=cb.from_user.id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    if not data.get("tg_id"):
        await cb.message.answer(t("check_error", lang=locale))
        return
    try:
        b = await report_breakdown(user_id=target_tg_id)
        for key in ("risk_factor_breakdown", "report_by_category", "last_7d_reports", "last_30d_reports"):
            if key in b:
                data[key] = b[key]
    except Exception:
        pass
    body = format_check_result(data, locale)
    body = await append_report_footer(body, target_tg_id, locale)
    is_self = cb.from_user and target_tg_id == cb.from_user.id
    if not is_self:
        body = body + "\n\n" + t("result_alert_tip", lang=locale)
    # Send as a new message to user (they can save or forward)
    await cb.message.answer(body, parse_mode="Markdown")
    await cb.message.answer(t("msg_resend_report", lang=locale))
