# 风险变化提醒：订阅、取消、我的订阅列表、后台发送
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from bot.api_client import alert_subscribe, alert_unsubscribe, alert_subscriptions, monitor_report_event
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import inline_alert_subscription_buttons, reply_main

router = Router()

ALERT_SUBS_TRIGGERS = get_texts_for_key("btn_my_alert_subscriptions")


def _parse_alert_subscribe_callback(data: str) -> tuple[int | None, str, str]:
    """Parse callback_data. Returns (target_tg_id, source, alert_type). alert_type: risk_change | online_offline | report_spike | wallet_change."""
    prefix = "alert_subscribe_"
    if not data.startswith(prefix):
        return None, "bot", "risk_change"
    rest = data[len(prefix) :].strip()
    for atype, key in (("online_offline", "online_"), ("report_spike", "report_spike_"), ("wallet_change", "wallet_change_")):
        if rest.startswith(key):
            try:
                tid = int(rest[len(key) :])
                return tid, "bot", atype
            except ValueError:
                return None, "bot", "risk_change"
    parts = rest.split("_", 1)
    try:
        tid = int(parts[0])
    except (ValueError, IndexError):
        return None, "bot", "risk_change"
    source = (parts[1] if len(parts) > 1 else None) or "bot"
    if source not in ("query", "invite", "recheck", "forward", "bot"):
        source = "bot"
    return tid, source, "risk_change"


@router.callback_query(F.data.startswith("alert_subscribe_"))
async def cb_alert_subscribe(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    target_tg_id, source, alert_type = _parse_alert_subscribe_callback(cb.data or "")
    if target_tg_id is None:
        return
    if cb.from_user.id == target_tg_id:
        await cb.message.answer(t("msg_alert_already", lang=get_locale(cb.from_user.language_code)))
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        result = await alert_subscribe(
            cb.from_user.id, target_tg_id, lang=locale, source=source, alert_type=alert_type
        )
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    status = result.get("status", "")
    if status == "already":
        await cb.message.answer(t("msg_alert_already", lang=locale))
        return
    if status == "limit":
        await cb.message.answer(t("msg_alert_limit", lang=locale))
        return
    if status == "self":
        await cb.message.answer(t("msg_alert_already", lang=locale))
        return
    if alert_type == "online_offline":
        await cb.message.answer(t("msg_alert_subscribed_online", lang=locale))
    elif alert_type == "report_spike":
        await cb.message.answer(t("msg_alert_subscribed_report_spike", lang=locale))
    elif alert_type == "wallet_change":
        await cb.message.answer(t("msg_alert_subscribed_wallet_change", lang=locale))
    else:
        await cb.message.answer(t("msg_alert_subscribed", lang=locale))


def _parse_alert_unsubscribe_callback(data: str) -> tuple[int | None, str | None]:
    """Parse 'alert_unsubscribe_<target_tg_id>' or '..._<target_tg_id>_<alert_type>'. Returns (target_tg_id, alert_type or None)."""
    prefix = "alert_unsubscribe_"
    if not data.startswith(prefix):
        return None, None
    rest = data[len(prefix) :].strip()
    if "_" in rest:
        parts = rest.rsplit("_", 1)
        try:
            tid = int(parts[0])
            atype = parts[1] if parts[1] in ("risk_change", "online_offline", "report_spike", "wallet_change") else None
            return tid, atype
        except (ValueError, IndexError):
            return None, None
    try:
        return int(rest), None
    except ValueError:
        return None, None


@router.callback_query(F.data.startswith("alert_unsubscribe_"))
async def cb_alert_unsubscribe(cb: CallbackQuery):
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    target_tg_id, alert_type = _parse_alert_unsubscribe_callback(cb.data or "")
    if target_tg_id is None:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        ok = await alert_unsubscribe(cb.from_user.id, target_tg_id, alert_type=alert_type)
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    await cb.message.answer(t("msg_alert_unsubscribed", lang=locale))


@router.message(F.text.in_(ALERT_SUBS_TRIGGERS))
async def cmd_my_alert_subscriptions(msg: Message):
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    try:
        data = await alert_subscriptions(msg.from_user.id)
    except Exception:
        await msg.answer(t("check_error", lang=locale), reply_markup=reply_main(locale))
        return
    items = data.get("items") or []
    if not items:
        text = t("alert_my_subscriptions_empty", lang=locale) + "\n\n" + t("alert_my_subscriptions_empty_invite_hint", lang=locale)
        await msg.answer(text, reply_markup=reply_main(locale))
        return
    lines = [t("alert_my_subscriptions_title", lang=locale)]
    for it in items:
        at = it.get("alert_type") or "risk_change"
        if at == "online_offline":
            type_label = t("alert_type_online", lang=locale)
        elif at == "report_spike":
            type_label = t("alert_type_report_spike", lang=locale)
        elif at == "wallet_change":
            type_label = t("alert_type_wallet_change", lang=locale)
        else:
            type_label = t("alert_type_risk", lang=locale)
        lines.append(
            t(
                "alert_my_subscriptions_item",
                lang=locale,
                tid=it["target_tg_id"],
                date=it.get("created_at") or "",
                type_label=type_label,
            )
        )
    await msg.answer(
        "\n".join(lines),
        reply_markup=inline_alert_subscription_buttons(items, locale),
        parse_mode="Markdown",
    )
    await msg.answer(t("menu_hint", lang=locale), reply_markup=reply_main(locale))


@router.callback_query(F.data.startswith("report_status_online_"))
@router.callback_query(F.data.startswith("report_status_offline_"))
async def cb_report_status(cb: CallbackQuery):
    """众包上报：当前用户上报目标在线/离线。"""
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    data = cb.data or ""
    if data.startswith("report_status_online_"):
        event_type = "online"
        rest = data[len("report_status_online_") :]
    else:
        event_type = "offline"
        rest = data[len("report_status_offline_") :]
    try:
        target_tg_id = int(rest.strip())
    except ValueError:
        return
    if cb.from_user.id == target_tg_id:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        await monitor_report_event(
            target_tg_id=target_tg_id,
            event_type=event_type,
            reporter_tg_id=cb.from_user.id,
        )
        await cb.message.answer(t("msg_status_reported", lang=locale))
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
