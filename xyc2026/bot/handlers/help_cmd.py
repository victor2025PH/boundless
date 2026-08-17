from aiogram import Router, F
from aiogram.types import Message
from bot.i18n import t, get_locale, get_texts_for_key
from bot.keyboards import reply_main

router = Router()

HELP_TRIGGERS = ["/help"] + get_texts_for_key("btn_help")

HELP_SECTIONS = [
    ("help_section_query_title", "help_section_query"),
    ("help_section_invite_title", "help_section_invite"),
    ("help_section_mycredit_title", "help_section_mycredit"),
    ("help_section_dont_trust_me_title", "help_section_dont_trust_me"),
    ("help_section_report_title", "help_section_report"),
    ("help_section_data_title", "help_section_data"),
]


@router.message(F.text.in_(HELP_TRIGGERS))
async def cmd_help(msg: Message):
    locale = get_locale(msg.from_user.language_code if msg.from_user else None)
    parts = [t("help_title", lang=locale)]
    for title_key, body_key in HELP_SECTIONS:
        parts.append(f"**{t(title_key, lang=locale)}**\n{t(body_key, lang=locale)}")
    text = "\n\n".join(parts)
    await msg.answer(text, parse_mode="Markdown", reply_markup=reply_main(locale))
