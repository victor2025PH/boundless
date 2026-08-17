from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.api_client import report
from bot.i18n import t, get_locale
from bot.keyboards import reply_main

router = Router()

REPORT_CATEGORIES = ("scam", "run_order", "lost_contact", "other")


class ReportStates(StatesGroup):
    waiting_category = State()
    waiting_reason = State()


def _report_category_keyboard(locale: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text=t("report_category_scam", lang=locale), callback_data="report_cat_scam"),
            InlineKeyboardButton(text=t("report_category_run_order", lang=locale), callback_data="report_cat_run_order"),
        ],
        [
            InlineKeyboardButton(text=t("report_category_lost_contact", lang=locale), callback_data="report_cat_lost_contact"),
            InlineKeyboardButton(text=t("report_category_other", lang=locale), callback_data="report_cat_other"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("report_") & ~F.data.startswith("report_cat_"))
async def cb_report_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    locale = get_locale(cb.from_user.language_code if cb.from_user else None)
    # callback_data: "report_<target_tg_id>"
    suffix = cb.data.split("_", 1)[1]
    try:
        target_tg_id = int(suffix)
    except ValueError:
        return
    await state.set_state(ReportStates.waiting_category)
    await state.update_data(report_target_tg_id=target_tg_id)
    await cb.message.answer(
        t("report_choose_category", lang=locale),
        reply_markup=_report_category_keyboard(locale),
    )


@router.callback_query(F.data.startswith("report_cat_"), ReportStates.waiting_category)
async def cb_report_category(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    locale = get_locale(cb.from_user.language_code if cb.from_user else None)
    cat = cb.data.replace("report_cat_", "")
    if cat not in REPORT_CATEGORIES:
        return
    await state.update_data(report_category=cat)
    await state.set_state(ReportStates.waiting_reason)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(t("report_prompt", lang=locale))


@router.message(ReportStates.waiting_reason, F.text)
async def report_reason_received(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    data = await state.get_data()
    target_tg_id = data.get("report_target_tg_id")
    category = data.get("report_category")
    await state.clear()
    if target_tg_id is None:
        await msg.answer(t("report_session_expired", lang=locale), reply_markup=reply_main(locale))
        return
    reason = (msg.text or "").strip()[:2000]
    if not reason:
        await msg.answer(t("report_reason_empty", lang=locale), reply_markup=reply_main(locale))
        return
    try:
        await report(
            reporter_tg_id=msg.from_user.id,
            target_tg_id=target_tg_id,
            reason=reason,
            category=category if category in REPORT_CATEGORIES else None,
        )
        await msg.answer(t("report_received", lang=locale), reply_markup=reply_main(locale))
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
