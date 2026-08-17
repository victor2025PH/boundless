# 最近查询 + 我的申诉
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from bot.api_client import recent_queries, my_feedback, check, can_view_flow_get
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import inline_recent_query_buttons, reply_main
from bot.format_result import format_check_result

router = Router()

RECENT_TRIGGERS = ["/recent"] + get_texts_for_key("btn_recent_queries")
APPEALS_TRIGGERS = get_texts_for_key("btn_my_appeals")


def _feedback_status_key(status: str) -> str:
    s = (status or "").lower()
    if s in ("processed", "verified", "done"):
        return "feedback_status_processed"
    if s == "rejected":
        return "feedback_status_rejected"
    return "feedback_status_pending"


@router.message(F.text.in_(RECENT_TRIGGERS))
async def cmd_recent_queries(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    try:
        data = await recent_queries(msg.from_user.id, limit=10)
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    items = data.get("items") or []
    if not items:
        await msg.answer(t("recent_queries_empty", lang=locale), reply_markup=reply_main(locale))
        return
    lines = [t("recent_queries_title", lang=locale)]
    for it in items:
        lines.append(t("recent_queries_item", lang=locale, tid=it["target_tg_id"], time=it["last_queried_at"]))
    await msg.answer(
        "\n".join(lines),
        reply_markup=inline_recent_query_buttons(items, locale),
        parse_mode="Markdown",
    )
    await msg.answer(t("menu_hint", lang=locale), reply_markup=reply_main(locale))


async def _send_my_appeals(chat_id: int, user_id: int, locale: str, bot):
    try:
        data = await my_feedback(user_id)
    except Exception:
        await bot.send_message(chat_id, t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    items = data.get("items") or []
    if not items:
        await bot.send_message(chat_id, t("feedback_empty", lang=locale), reply_markup=reply_main(locale))
        return
    lines = [t("feedback_title", lang=locale)]
    for it in items:
        status_key = _feedback_status_key(it.get("status"))
        status_label = t(status_key, lang=locale)
        reason = (it.get("reason") or "")[:80]
        date = it.get("created_at") or ""
        lines.append(t("feedback_item", lang=locale, tid=it["target_tg_id"], reason=reason, status=status_label, date=date))
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=reply_main(locale))


@router.message(F.text.in_(APPEALS_TRIGGERS))
async def cmd_my_appeals(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    await _send_my_appeals(msg.chat.id, msg.from_user.id, locale, msg.bot)


@router.callback_query(F.data == "my_appeals")
async def cb_my_appeals(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    await _send_my_appeals(cb.message.chat.id, cb.from_user.id, locale, cb.bot)


@router.callback_query(F.data.startswith("recheck_"))
async def cb_recheck(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    try:
        target_tg_id = int(cb.data.replace("recheck_", ""))
    except ValueError:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        data = await check(user_id=target_tg_id, querier_tg_id=cb.from_user.id)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    if not data.get("tg_id"):
        await cb.message.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    from bot.format_result import append_report_footer
    from bot.keyboards import inline_result_actions, inline_mycredit_actions
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
        reply_mk = inline_result_actions(target_tg_id, locale, source="recheck", can_view_flow=can_view)
    await cb.message.answer(
        body,
        reply_markup=reply_mk,
        parse_mode="Markdown",
    )
    await cb.message.answer(t("menu_hint", lang=locale), reply_markup=reply_main(locale))
