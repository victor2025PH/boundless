# Inline and reply keyboards — all text via i18n with lang
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from bot.i18n import t, DEFAULT_LOCALE


def inline_query_now(lang: str | None = None) -> InlineKeyboardMarkup:
    loc = lang or DEFAULT_LOCALE
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("btn_query", lang=loc), callback_data="query_now")]]
    )


def inline_invite_btn(lang: str | None = None) -> InlineKeyboardMarkup:
    """Single button: get invite link (for 'hidden forward' / 'username not found' replies)."""
    loc = lang or DEFAULT_LOCALE
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("invite_btn", lang=loc), callback_data="get_invite")]]
    )


def inline_start_buttons(lang: str | None = None) -> InlineKeyboardMarkup:
    """Welcome: 立即查询 + 获取邀请链接 (no slash commands)."""
    loc = lang or DEFAULT_LOCALE
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("btn_query", lang=loc), callback_data="query_now"),
                InlineKeyboardButton(text=t("invite_btn", lang=loc), callback_data="get_invite"),
            ],
        ]
    )


def inline_help(lang: str | None = None) -> InlineKeyboardMarkup:
    loc = lang or DEFAULT_LOCALE
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("btn_help", lang=loc), url="https://t.me/xyc2026_bot")]]
    )


def inline_result_actions(
    target_tg_id: int, lang: str | None = None, source: str | None = None, can_view_flow: bool = False
) -> InlineKeyboardMarkup:
    """source: query | invite | recheck | forward；can_view_flow 为 true 时才显示「查看对方流水」按钮。"""
    loc = lang or DEFAULT_LOCALE
    sub_data = f"alert_subscribe_{target_tg_id}_{source}" if source else f"alert_subscribe_{target_tg_id}"
    rows = [
        [InlineKeyboardButton(text=t("btn_report", lang=loc), callback_data=f"report_{target_tg_id}")],
        [
            InlineKeyboardButton(text=t("btn_ask_show_report", lang=loc), callback_data="ask_show_report"),
            InlineKeyboardButton(text=t("btn_forward_report", lang=loc), callback_data=f"forward_report_{target_tg_id}"),
        ],
        [InlineKeyboardButton(text=t("btn_resend_report", lang=loc), callback_data=f"resend_report_{target_tg_id}")],
        [InlineKeyboardButton(text=t("btn_resend_report", lang=loc), callback_data=f"resend_report_{target_tg_id}")],
    ]
    if can_view_flow:
        rows.append([InlineKeyboardButton(text=t("view_flow_btn", lang=loc), callback_data=f"view_flow_{target_tg_id}")])
    rows.extend([
        [InlineKeyboardButton(text=t("btn_verify_report", lang=loc), callback_data="ask_verify_report")],
        [InlineKeyboardButton(text=t("btn_tx_confirm", lang=loc), callback_data=f"tx_confirm_{target_tg_id}")],
        [InlineKeyboardButton(text=t("btn_alert_subscribe", lang=loc), callback_data=sub_data)],
        [InlineKeyboardButton(text=t("btn_subscribe_online_offline", lang=loc), callback_data=f"alert_subscribe_online_{target_tg_id}")],
        [
            InlineKeyboardButton(text=t("btn_subscribe_report_spike", lang=loc), callback_data=f"alert_subscribe_report_spike_{target_tg_id}"),
            InlineKeyboardButton(text=t("btn_subscribe_wallet_change", lang=loc), callback_data=f"alert_subscribe_wallet_change_{target_tg_id}"),
        ],
        [
            InlineKeyboardButton(text=t("btn_report_online", lang=loc), callback_data=f"report_status_online_{target_tg_id}"),
            InlineKeyboardButton(text=t("btn_report_offline", lang=loc), callback_data=f"report_status_offline_{target_tg_id}"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_tx_respond_buttons(transaction_id: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """Single transaction: [Accept] [Reject] for counterparty."""
    loc = lang or DEFAULT_LOCALE
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("tx_accept", lang=loc), callback_data=f"tx_accept_{transaction_id}"),
                InlineKeyboardButton(text=t("tx_reject", lang=loc), callback_data=f"tx_reject_{transaction_id}"),
            ],
        ]
    )


def inline_pending_tx_buttons(items: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """List of pending: each row [Amount summary] [Accept] [Reject] or one row per item with accept/reject."""
    loc = lang or DEFAULT_LOCALE
    rows = []
    for it in items[:15]:
        tx_id = it.get("id")
        amount = it.get("amount", "")
        currency = it.get("currency", "USDT")
        rows.append([
            InlineKeyboardButton(text=f"{amount} {currency}", callback_data=f"tx_view_{tx_id}"),
            InlineKeyboardButton(text=t("tx_accept", lang=loc), callback_data=f"tx_accept_{tx_id}"),
            InlineKeyboardButton(text=t("tx_reject", lang=loc), callback_data=f"tx_reject_{tx_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_mycredit_actions(
    target_tg_id: int, lang: str | None = None, notify_on_checked: bool = True
) -> InlineKeyboardMarkup:
    """Same as result actions + 出示我的报告 + 我的申诉 + 被查通知开关."""
    loc = lang or DEFAULT_LOCALE
    rows = [
        [InlineKeyboardButton(text=t("btn_report", lang=loc), callback_data=f"report_{target_tg_id}")],
        [
            InlineKeyboardButton(text=t("btn_ask_show_report", lang=loc), callback_data="ask_show_report"),
            InlineKeyboardButton(text=t("btn_forward_report", lang=loc), callback_data=f"forward_report_{target_tg_id}"),
        ],
        [InlineKeyboardButton(text=t("btn_resend_report", lang=loc), callback_data=f"resend_report_{target_tg_id}")],
        [
            InlineKeyboardButton(text=t("btn_show_my_report", lang=loc), callback_data="show_my_report"),
            InlineKeyboardButton(text=t("btn_sharecard", lang=loc), callback_data="sharecard"),
        ],
        [InlineKeyboardButton(text=t("btn_wallets", lang=loc), callback_data="wallets")],
        [InlineKeyboardButton(text=t("btn_my_appeals", lang=loc), callback_data="my_appeals")],
        [InlineKeyboardButton(text=t("btn_privacy_settings", lang=loc), callback_data="privacy_settings")],
    ]
    if notify_on_checked:
        rows.append([
            InlineKeyboardButton(text=t("notify_setting_on", lang=loc), callback_data="noop"),
            InlineKeyboardButton(text=t("notify_btn_turn_off", lang=loc), callback_data=f"notify_off_{target_tg_id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text=t("notify_setting_off", lang=loc), callback_data="noop"),
            InlineKeyboardButton(text=t("notify_btn_turn_on", lang=loc), callback_data=f"notify_on_{target_tg_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reply_main(lang: str | None = None) -> ReplyKeyboardMarkup:
    """主菜单：仅显示易懂的按钮文案，不出现斜杠命令。"""
    loc = lang or DEFAULT_LOCALE
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t("btn_query", lang=loc)), KeyboardButton(text=t("invite_btn", lang=loc))],
            [KeyboardButton(text=t("btn_my_credit", lang=loc)), KeyboardButton(text=t("btn_help", lang=loc))],
            [KeyboardButton(text=t("btn_recent_queries", lang=loc)), KeyboardButton(text=t("btn_my_appeals", lang=loc))],
            [KeyboardButton(text=t("btn_pending_tx", lang=loc)), KeyboardButton(text=t("btn_verify_report", lang=loc))],
            [KeyboardButton(text=t("btn_my_alert_subscriptions", lang=loc))],
        ],
        resize_keyboard=True,
    )


def inline_alert_subscription_buttons(items: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """Per subscription: [Re-check ID] [Unsubscribe]; callback includes alert_type for per-type unsubscribe."""
    loc = lang or DEFAULT_LOCALE
    recheck_label = t("btn_recheck", lang=loc)
    unsub_label = t("btn_alert_unsubscribe", lang=loc)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{recheck_label} {it['target_tg_id']}", callback_data=f"recheck_{it['target_tg_id']}"),
                InlineKeyboardButton(
                    text=unsub_label,
                    callback_data=f"alert_unsubscribe_{it['target_tg_id']}_{it.get('alert_type', 'risk_change')}",
                ),
            ]
            for it in items[:15]
        ]
    )


def inline_recent_query_buttons(items: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """One «Re-check» button per recent query (callback_data=recheck_<tg_id>)."""
    loc = lang or DEFAULT_LOCALE
    btn_label = t("btn_recheck", lang=loc)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{btn_label} {item['target_tg_id']}", callback_data=f"recheck_{item['target_tg_id']}")]
            for item in items[:10]
        ]
    )
