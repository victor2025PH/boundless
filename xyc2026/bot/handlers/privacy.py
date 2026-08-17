# 阶段 B：/my_privacy 与「隐私设置」入口，Inline 开关
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.api_client import privacy_get, privacy_set
from bot.i18n import t, get_locale

router = Router()

PRIVACY_KEYS = ("show_register_year", "show_premium", "show_tx_30d")
PRIVACY_OPTION_KEYS = {
    "show_register_year": "privacy_option_register_year",
    "show_premium": "privacy_option_premium",
    "show_tx_30d": "privacy_option_tx_30d",
}


def _privacy_keyboard(settings: dict, locale: str) -> InlineKeyboardMarkup:
    """根据当前设置生成每项一行、点击切换的键盘。"""
    rows = []
    for key in PRIVACY_KEYS:
        on = settings.get(key, True)
        label = t(PRIVACY_OPTION_KEYS[key], lang=locale)
        btn_text = f"{label}: " + (t("privacy_value_on", lang=locale) if on else t("privacy_value_off", lang=locale))
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"privacy_toggle_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_privacy_screen(chat_id: int, user_tg_id: int, locale: str, bot, edit_message=None):
    """获取当前隐私设置并发送/编辑隐私设置界面。"""
    try:
        settings = await privacy_get(user_tg_id)
    except Exception:
        settings = {"show_register_year": True, "show_premium": True, "show_tx_30d": True}
    text = f"**{t('privacy_title', lang=locale)}**\n\n{t('privacy_intro', lang=locale)}"
    keyboard = _privacy_keyboard(settings, locale)
    if edit_message:
        await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)


@router.message(F.text.func(lambda t: (t or "").strip().lower() in ("/my_privacy", "/privacy")))
async def cmd_my_privacy(msg: Message):
    """命令 /my_privacy：仅本人可设置。"""
    if not msg.from_user:
        return
    locale = get_locale(msg.from_user.language_code)
    await _send_privacy_screen(msg.chat.id, msg.from_user.id, locale, msg.bot)


@router.callback_query(F.data == "privacy_settings")
async def cb_privacy_settings(cb: CallbackQuery):
    """从「我的信用」点击「隐私设置」进入。"""
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    locale = get_locale(cb.from_user.language_code)
    await _send_privacy_screen(cb.message.chat.id, cb.from_user.id, locale, cb.bot, edit_message=cb.message)


@router.callback_query(F.data.startswith("privacy_toggle_"))
async def cb_privacy_toggle(cb: CallbackQuery):
    """切换某一项隐私：GET 当前 → 取反 → PATCH → 刷新界面。"""
    await cb.answer()
    if not cb.from_user or not cb.message:
        return
    data = (cb.data or "").strip()
    key = data.replace("privacy_toggle_", "")
    if key not in PRIVACY_KEYS:
        return
    locale = get_locale(cb.from_user.language_code)
    try:
        current = await privacy_get(cb.from_user.id)
        new_value = not current.get(key, True)
        await privacy_set(cb.from_user.id, **{key: new_value})
    except Exception:
        await cb.message.answer(t("check_error", lang=locale))
        return
    await _send_privacy_screen(
        cb.message.chat.id, cb.from_user.id, locale, cb.bot, edit_message=cb.message
    )
