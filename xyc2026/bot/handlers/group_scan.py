# V2.0 群扫描：仅群管理员、每群每日 1 次
import httpx
from aiogram import Router, F
from aiogram.types import Message

from bot.api_client import group_scan_post
from bot.i18n import t, get_locale

router = Router()


@router.message(F.text == "/scan_group")
async def cmd_scan_group(msg: Message):
    if not msg.from_user or not msg.chat:
        return
    locale = get_locale(msg.from_user.language_code)
    chat = msg.chat
    if chat.type not in ("group", "supergroup"):
        await msg.answer(t("scan_group_private", lang=locale))
        return
    try:
        admins = await msg.bot.get_chat_administrators(chat.id)
    except Exception:
        await msg.answer(t("check_error", lang=locale))
        return
    admin_ids = {m.user.id for m in admins}
    if msg.from_user.id not in admin_ids:
        await msg.answer(t("scan_group_not_admin", lang=locale))
        return
    try:
        await group_scan_post(group_id=chat.id, requester_tg_id=msg.from_user.id)
        await msg.answer(t("scan_group_ok", lang=locale))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            await msg.answer(t("scan_group_already", lang=locale))
        else:
            await msg.answer(t("check_error", lang=locale))
    except Exception:
        await msg.answer(t("check_error", lang=locale))
