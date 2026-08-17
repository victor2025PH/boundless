from aiogram import Router, F
from aiogram.types import Message
from config import settings
from bot.api_client import admin_stats
from bot.i18n import t, get_locale

router = Router()


def is_admin(tg_id: int) -> bool:
    return tg_id in settings.get_admin_ids()


@router.message(F.text == "/stats")
async def cmd_stats(msg: Message):
    if not msg.from_user or not is_admin(msg.from_user.id):
        locale = get_locale(msg.from_user.language_code if msg.from_user else None)
        await msg.answer(t("admin_forbidden", lang=locale))
        return
    locale = get_locale(msg.from_user.language_code)
    try:
        s = await admin_stats()
        await msg.answer(
            t(
                "stats_format",
                lang=locale,
                queries_today=s.get("queries_today", 0),
                total_users=s.get("total_users", 0),
                blacklist_count=s.get("blacklist_count", 0),
                reports_pending=s.get("reports_pending", 0),
            ),
            parse_mode="Markdown",
        )
    except Exception:
        await msg.answer(t("check_error", lang=locale))
