# 绑定收款地址与我的流水、查看对方流水
import httpx
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.api_client import (
    wallets_list,
    wallets_bind,
    wallets_unbind,
    wallets_set_show_flow,
    my_flow_get,
    report_flow_get,
    report_flow_from_db_get,
    sync_flow_post,
    flow_from_db_get,
    flow_stats_30d_get,
)
from bot.i18n import t, get_locale
from bot.keyboards import reply_main

router = Router()


class WalletStates(StatesGroup):
    waiting_address = State()


def _wallets_list_keyboard(items: list[dict], locale: str) -> InlineKeyboardMarkup:
    rows = []
    for w in items[:10]:
        wid = w.get("id")
        chain = w.get("chain", "")
        masked = w.get("address_masked", "***")
        show = w.get("show_flow_to_public", False)
        label = (w.get("label") or "").strip() or masked
        flow_btn = t("wallet_show_flow_off", lang=locale) if show else t("wallet_show_flow_on", lang=locale)
        rows.append([
            InlineKeyboardButton(text=f"{chain} {label[:20]}", callback_data="noop"),
        ])
        rows.append([
            InlineKeyboardButton(text=flow_btn, callback_data=f"wallet_flow_{wid}"),
            InlineKeyboardButton(text=t("wallet_unbind", lang=locale), callback_data=f"wallet_del_{wid}"),
        ])
    rows.append([InlineKeyboardButton(text=t("wallets_add", lang=locale), callback_data="wallet_add")])
    rows.append([InlineKeyboardButton(text=t("wallets_my_flow", lang=locale), callback_data="my_flow")])
    rows.append([
        InlineKeyboardButton(text=t("wallets_sync_flow", lang=locale), callback_data="sync_flow"),
        InlineKeyboardButton(text=t("flow_from_db", lang=locale), callback_data="flow_from_db_0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _chain_choice_keyboard(locale: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("wallet_chain_trc20", lang=locale), callback_data="wallet_add_chain_TRC20"),
                InlineKeyboardButton(text=t("wallet_chain_ton", lang=locale), callback_data="wallet_add_chain_TON"),
            ],
        ]
    )


@router.callback_query(F.data == "wallets")
async def cb_wallets(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        items = await wallets_list(cb.from_user.id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    if not items:
        text = t("wallets_title", lang=locale) + "\n\n" + t("wallets_empty", lang=locale)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("wallets_add", lang=locale), callback_data="wallet_add")],
                [InlineKeyboardButton(text=t("wallets_my_flow", lang=locale), callback_data="my_flow")],
                [
                    InlineKeyboardButton(text=t("wallets_sync_flow", lang=locale), callback_data="sync_flow"),
                    InlineKeyboardButton(text=t("flow_from_db", lang=locale), callback_data="flow_from_db_0"),
                ],
            ]
        )
    else:
        lines = [t("wallets_title", lang=locale)]
        for w in items:
            show = " ✓" if w.get("show_flow_to_public") else ""
            verified = " · " + t("wallet_verified", lang=locale) if w.get("verified_at") else " · " + t("wallet_unverified", lang=locale)
            lines.append(f"• {w.get('chain', '')} {w.get('address_masked', '')}{show}{verified}")
        lines.append("")
        lines.append("_" + t("wallet_flow_privacy_hint", lang=locale) + "_")
        text = "\n".join(lines)
        kb = _wallets_list_keyboard(items, locale)
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "wallet_add")
async def cb_wallet_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not cb.from_user:
        return
    locale = get_locale(cb.from_user.language_code)
    await cb.message.answer(t("wallet_add_choose_chain", lang=locale), reply_markup=_chain_choice_keyboard(locale))


@router.callback_query(F.data.startswith("wallet_add_chain_"))
async def cb_wallet_add_chain(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not cb.from_user:
        return
    locale = get_locale(cb.from_user.language_code)
    chain = cb.data.replace("wallet_add_chain_", "")
    if chain not in ("TRC20", "TON"):
        return
    await state.set_state(WalletStates.waiting_address)
    await state.update_data(wallet_add_chain=chain)
    chain_label = t("wallet_chain_trc20", lang=locale) if chain == "TRC20" else t("wallet_chain_ton", lang=locale)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(t("wallet_add_paste_address", lang=locale, chain=chain_label))


@router.message(WalletStates.waiting_address, F.text)
async def wallet_address_received(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    data = await state.get_data()
    chain = data.get("wallet_add_chain", "TRC20")
    await state.clear()
    address = (msg.text or "").strip().split("\n")[0][:128]
    if not address:
        await msg.answer(t("wallet_add_invalid", lang=locale), reply_markup=reply_main(locale))
        return
    try:
        await wallets_bind(msg.from_user.id, chain, address, show_flow_to_public=False)
        await msg.answer(t("wallet_add_ok", lang=locale), reply_markup=reply_main(locale))
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        detail = (getattr(getattr(e, "response", None), "text", None) or "").lower()
        if code == 409:
            await msg.answer(t("wallet_add_duplicate", lang=locale), reply_markup=reply_main(locale))
        elif code == 400 and "limit" in detail:
            await msg.answer(t("wallet_add_limit", lang=locale), reply_markup=reply_main(locale))
        elif code == 400:
            await msg.answer(t("wallet_add_invalid", lang=locale), reply_markup=reply_main(locale))
        else:
            await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))


@router.callback_query(F.data.startswith("wallet_del_"))
async def cb_wallet_del(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    try:
        wallet_id = int(cb.data.replace("wallet_del_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        ok = await wallets_unbind(cb.from_user.id, wallet_id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    items = await wallets_list(cb.from_user.id)
    if not items:
        text = t("wallets_title", lang=locale) + "\n\n" + (t("wallet_unbind", lang=locale) + " ✓\n\n" if ok else "") + t("wallets_empty", lang=locale)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("wallets_add", lang=locale), callback_data="wallet_add")],
                [InlineKeyboardButton(text=t("wallets_my_flow", lang=locale), callback_data="my_flow")],
                [
                    InlineKeyboardButton(text=t("wallets_sync_flow", lang=locale), callback_data="sync_flow"),
                    InlineKeyboardButton(text=t("flow_from_db", lang=locale), callback_data="flow_from_db_0"),
                ],
            ]
        )
    else:
        text = t("wallets_title", lang=locale) + ("\n\n" + t("wallet_unbind", lang=locale) + " ✓" if ok else "")
        for w in items:
            show = " ✓" if w.get("show_flow_to_public") else ""
            verified = " · " + t("wallet_verified", lang=locale) if w.get("verified_at") else " · " + t("wallet_unverified", lang=locale)
            text += f"\n• {w.get('chain', '')} {w.get('address_masked', '')}{show}{verified}"
        kb = _wallets_list_keyboard(items, locale)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("wallet_flow_"))
async def cb_wallet_flow_toggle(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    try:
        wallet_id = int(cb.data.replace("wallet_flow_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        items = await wallets_list(cb.from_user.id)
        w = next((x for x in items if x.get("id") == wallet_id), None)
        if not w:
            return
        new_show = not w.get("show_flow_to_public", False)
        ok = await wallets_set_show_flow(cb.from_user.id, wallet_id, new_show)
    except Exception:
        return
    if ok:
        items = await wallets_list(cb.from_user.id)
        kb = _wallets_list_keyboard(items, locale)
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data == "my_flow")
async def cb_my_flow(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        items, err = await my_flow_get(cb.from_user.id, limit=30)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 429:
            await cb.message.answer(t("flow_rate_limit", lang=locale))
        else:
            await cb.message.answer(t("check_error", lang=locale))
        return
    if err == "no_wallets":
        await cb.message.answer(t("wallets_empty", lang=locale))
        return
    text = t("flow_title_mine", lang=locale) + "\n\n"
    if not items:
        text += t("flow_empty", lang=locale)
    else:
        for x in items[:20]:
            direction = t("flow_in", lang=locale) if x.get("direction") == "in" else t("flow_out", lang=locale)
            text += t(
                "flow_line",
                lang=locale,
                dir=direction,
                amount=x.get("amount", 0),
                currency=x.get("currency", "USDT"),
                counterparty=x.get("counterparty_masked", "***"),
                chain=x.get("chain", ""),
            ) + "\n"
    text += "\n" + t("flow_disclaimer", lang=locale)
    await cb.message.answer(text[:4000])


@router.callback_query(F.data == "sync_flow")
async def cb_sync_flow(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        written = await sync_flow_post(cb.from_user.id)
    except Exception as e:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
            retry_after = e.response.headers.get("Retry-After", "5")
            try:
                minutes = max(1, int(retry_after) // 60)
            except ValueError:
                minutes = 5
            await cb.message.answer(t("flow_sync_rate_limited", lang=locale, minutes=minutes))
        else:
            await cb.message.answer(t("check_error", lang=locale))
        return
    if written > 0:
        await cb.message.answer(t("flow_synced", lang=locale, n=written))
    else:
        await cb.message.answer(t("flow_sync_none", lang=locale))


@router.callback_query(F.data.startswith("flow_from_db_"))
async def cb_flow_from_db(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    try:
        offset = int(cb.data.replace("flow_from_db_", ""))
    except ValueError:
        offset = 0
    locale = get_locale(cb.from_user.language_code)
    limit = 20
    try:
        # 仅在首页尝试获取近 30 天统计，失败可忽略
        stats = {}
        if offset == 0:
            try:
                stats = await flow_stats_30d_get(cb.from_user.id)
            except Exception:
                stats = {}
        items = await flow_from_db_get(cb.from_user.id, limit=limit, offset=offset)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    if not items:
        msg = t("flow_from_db_empty", lang=locale) if offset == 0 else t("flow_empty", lang=locale)
        await cb.message.answer(msg)
        return
    text = t("flow_title_mine", lang=locale) + " " + t("flow_from_db_page", lang=locale, offset=offset) + "\n\n"
    if offset == 0 and stats:
        text += t(
            "flow_stats_30d_line",
            lang=locale,
            days=stats.get("days", 30),
            in_count=stats.get("in_count", 0),
            in_amount=stats.get("in_amount", 0),
            out_count=stats.get("out_count", 0),
            out_amount=stats.get("out_amount", 0),
        ) + "\n\n"
    for x in items:
        direction = t("flow_in", lang=locale) if x.get("direction") == "in" else t("flow_out", lang=locale)
        text += t(
            "flow_line",
            lang=locale,
            dir=direction,
            amount=x.get("amount", 0),
            currency=x.get("currency", "USDT"),
            counterparty=x.get("counterparty_masked", "***"),
            chain=x.get("chain", ""),
        ) + "\n"
    text += "\n" + t("flow_disclaimer", lang=locale)
    rows = []
    if offset > 0:
        rows.append([InlineKeyboardButton(text=t("flow_from_db_prev", lang=locale), callback_data=f"flow_from_db_{max(0, offset - limit)}")])
    if len(items) >= limit:
        rows.append([InlineKeyboardButton(text=t("flow_from_db_next", lang=locale), callback_data=f"flow_from_db_{offset + limit}")])
    # 统一返回层级：提供「返回收款地址」按钮
    rows.append([InlineKeyboardButton(text="◀ " + t("wallets_title", lang=locale), callback_data="wallets")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await cb.message.answer(text[:4000], reply_markup=kb)


@router.callback_query(F.data.startswith("view_flow_"))
async def cb_view_flow(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user:
        return
    data = cb.data
    # view_flow_db_{target}_{offset} -> 对方流水快照分页
    if data.startswith("view_flow_db_"):
        parts = data.replace("view_flow_db_", "").split("_")
        try:
            target_tg_id = int(parts[0])
            offset = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return
        locale = get_locale(cb.from_user.language_code)
        limit = 20
        try:
            items = await report_flow_from_db_get(target_tg_id, limit=limit, offset=offset)
        except Exception:
            await cb.message.answer(t("check_error", lang=locale))
            return
        if not items:
            msg = t("flow_empty", lang=locale) if offset > 0 else t("flow_from_db_empty", lang=locale)
            await cb.message.answer(msg)
            return
        text = t("flow_title_other", lang=locale) + " " + t("flow_other_from_db_note", lang=locale) + f"\n({offset}+)\n\n"
        for x in items:
            direction = t("flow_in", lang=locale) if x.get("direction") == "in" else t("flow_out", lang=locale)
            text += t(
                "flow_line",
                lang=locale,
                dir=direction,
                amount=x.get("amount", 0),
                currency=x.get("currency", "USDT"),
                counterparty=x.get("counterparty_masked", "***"),
                chain=x.get("chain", ""),
            ) + "\n"
        text += "\n" + t("flow_disclaimer", lang=locale)
        rows = []
        if offset > 0:
            rows.append([InlineKeyboardButton(text=t("flow_from_db_prev", lang=locale), callback_data=f"view_flow_db_{target_tg_id}_{max(0, offset - limit)}")])
        if len(items) >= limit:
            rows.append([InlineKeyboardButton(text=t("flow_from_db_next", lang=locale), callback_data=f"view_flow_db_{target_tg_id}_{offset + limit}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        await cb.message.answer(text[:4000], reply_markup=kb)
        return
    # view_flow_{target} -> 首次查看对方流水：优先快照分页，无则实时
    try:
        target_tg_id = int(cb.data.replace("view_flow_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    limit = 20
    items_from_db = []
    try:
        items_from_db = await report_flow_from_db_get(target_tg_id, limit=limit, offset=0)
    except Exception:
        pass
    if items_from_db:
        text = t("flow_title_other", lang=locale) + " " + t("flow_other_from_db_note", lang=locale) + "\n\n"
        for x in items_from_db:
            direction = t("flow_in", lang=locale) if x.get("direction") == "in" else t("flow_out", lang=locale)
            text += t(
                "flow_line",
                lang=locale,
                dir=direction,
                amount=x.get("amount", 0),
                currency=x.get("currency", "USDT"),
                counterparty=x.get("counterparty_masked", "***"),
                chain=x.get("chain", ""),
            ) + "\n"
        text += "\n" + t("flow_disclaimer", lang=locale)
        rows = []
        if len(items_from_db) >= limit:
            rows.append([InlineKeyboardButton(text=t("flow_from_db_next", lang=locale), callback_data=f"view_flow_db_{target_tg_id}_{limit}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        await cb.message.answer(text[:4000], reply_markup=kb)
        return
    try:
        items, err = await report_flow_get(target_tg_id, limit=30, caller_tg_id=cb.from_user.id)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 429:
            await cb.message.answer(t("flow_rate_limit", lang=locale))
        else:
            await cb.message.answer(t("check_error", lang=locale))
        return
    if err == "no_wallets" or (not items and err):
        await cb.message.answer(t("flow_error_no_wallets", lang=locale))
        return
    text = t("flow_title_other", lang=locale) + "\n\n"
    if not items:
        text += t("flow_empty", lang=locale)
    else:
        for x in items[:20]:
            direction = t("flow_in", lang=locale) if x.get("direction") == "in" else t("flow_out", lang=locale)
            text += t(
                "flow_line",
                lang=locale,
                dir=direction,
                amount=x.get("amount", 0),
                currency=x.get("currency", "USDT"),
                counterparty=x.get("counterparty_masked", "***"),
                chain=x.get("chain", ""),
            ) + "\n"
    text += "\n" + t("flow_disclaimer", lang=locale)
    await cb.message.answer(text[:4000])
