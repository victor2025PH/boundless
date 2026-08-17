# V2.0 交易确认：发起请求、待我确认列表、同意/拒绝
import httpx
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.api_client import transaction_confirm, transaction_respond, transaction_pending
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import (
    reply_main,
    inline_tx_respond_buttons,
    inline_pending_tx_buttons,
)

router = Router()


class TxStates(StatesGroup):
    waiting_amount = State()


def _parse_amount_and_memo(text: str) -> tuple[str, str | None]:
    """First line = amount (strip), second line = optional memo (strip, max 500)."""
    lines = (text or "").strip().split("\n", 1)
    amount = (lines[0] or "").strip()[:32]
    memo = (lines[1].strip()[:500]) if len(lines) > 1 and lines[1].strip() else None
    return amount, memo


# 从结果卡点击「请求对方确认交易」
@router.callback_query(F.data.startswith("tx_confirm_"))
async def cb_tx_confirm_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not cb.from_user:
        return
    try:
        target_tg_id = int(cb.data.replace("tx_confirm_", ""))
    except ValueError:
        return
    if target_tg_id == cb.from_user.id:
        locale = get_locale(cb.from_user.language_code)
        await cb.message.answer(t("tx_err_self", lang=locale), reply_markup=reply_main(locale))
        return
    await state.set_state(TxStates.waiting_amount)
    await state.update_data(tx_target_tg_id=target_tg_id)
    locale = get_locale(cb.from_user.language_code)
    await cb.message.answer(t("tx_confirm_prompt", lang=locale))


@router.message(TxStates.waiting_amount, F.text)
async def tx_amount_received(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    data = await state.get_data()
    target_tg_id = data.get("tx_target_tg_id")
    await state.clear()
    if target_tg_id is None:
        await msg.answer(t("report_session_expired", lang=locale), reply_markup=reply_main(locale))
        return
    amount, memo = _parse_amount_and_memo(msg.text or "")
    if not amount:
        await msg.answer(t("tx_confirm_prompt", lang=locale), reply_markup=reply_main(locale))
        return
    try:
        result = await transaction_confirm(
            initiator_tg_id=msg.from_user.id,
            counterparty_tg_id=target_tg_id,
            amount=amount,
            currency="USDT",
            memo=memo,
        )
    except httpx.HTTPStatusError as e:
        detail = ""
        code = ""
        try:
            body = e.response.json()
            detail = (body.get("detail") or "").lower()
            code = (body.get("code") or "").lower()
        except Exception:
            detail = str(e).lower()
        if code == "self_trade" or "yourself" in detail or "self" in detail:
            await msg.answer(t("tx_err_self", lang=locale), reply_markup=reply_main(locale))
        elif code == "too_many_pending" or "too many" in detail or "pending" in detail:
            await msg.answer(t("tx_err_too_many", lang=locale), reply_markup=reply_main(locale))
        else:
            await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    tx_id = result.get("transaction_id")
    if tx_id and msg.bot:
        try:
            await msg.bot.send_message(
                chat_id=target_tg_id,
                text=f"📋 {t('tx_pending_title', lang=locale)}\n\n{t('tx_pending_line', lang=locale, amount=amount, currency='USDT')}",
                reply_markup=inline_tx_respond_buttons(tx_id, locale),
            )
        except Exception:
            pass
    await msg.answer(t("tx_confirm_sent", lang=locale), reply_markup=reply_main(locale))


# 主菜单「待我确认」或 /pending
PENDING_TX_TRIGGERS = ["/pending"] + get_texts_for_key("btn_pending_tx")


@router.message(F.text.in_(PENDING_TX_TRIGGERS))
async def cmd_pending_tx(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    try:
        items = await transaction_pending(msg.from_user.id)
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    if not items:
        await msg.answer(t("tx_pending_empty", lang=locale), reply_markup=reply_main(locale))
        return
    header = t("tx_pending_list_header", lang=locale)
    lines = []
    for it in items[:15]:
        amount = it.get("amount", "")
        currency = it.get("currency", "USDT")
        initiator = it.get("initiator_tg_id", "")
        lines.append(f"• ID {it.get('id')}: {amount} {currency} (from {initiator})")
    body = header + "\n\n" + "\n".join(lines)
    await msg.answer(body, reply_markup=inline_pending_tx_buttons(items, locale), parse_mode="Markdown")


# 对方点击「同意」
@router.callback_query(F.data.startswith("tx_accept_"))
async def cb_tx_accept(cb: CallbackQuery):
    if not cb.from_user or not cb.message:
        return
    try:
        tx_id = int(cb.data.replace("tx_accept_", ""))
    except ValueError:
        await cb.answer()
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        await transaction_respond(transaction_id=tx_id, responder_tg_id=cb.from_user.id, accept=True)
    except Exception:
        await cb.answer(t("check_error", lang=locale), show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(t("tx_accepted", lang=locale), reply_markup=None)


# 对方点击「拒绝」
@router.callback_query(F.data.startswith("tx_reject_"))
async def cb_tx_reject(cb: CallbackQuery):
    if not cb.from_user or not cb.message:
        return
    try:
        tx_id = int(cb.data.replace("tx_reject_", ""))
    except ValueError:
        await cb.answer()
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        await transaction_respond(transaction_id=tx_id, responder_tg_id=cb.from_user.id, accept=False)
    except Exception:
        await cb.answer(t("check_error", lang=locale), show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(t("tx_rejected", lang=locale), reply_markup=None)
