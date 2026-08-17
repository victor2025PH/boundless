# TrustCheck Bot entrypoint
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from bot.handlers import start, check, forward, mycredit, help_cmd, report, admin, invite, history, alerts, transaction, group_scan, verify, privacy, wallets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _alert_poll_worker(bot: Bot) -> None:
    """Background: poll pending risk alerts + 被查通知, send to users."""
    from bot.api_client import alert_pending, alert_mark_sent, notify_checked_pending, notify_checked_mark_sent
    from bot.i18n import t
    if not getattr(settings, "api_internal_key", None):
        logger.info("Alert worker skipped: no api_internal_key")
        return
    while True:
        try:
            await asyncio.sleep(35)
            # 1) Risk alerts
            data = await alert_pending(limit=50)
            for item in (data.get("items") or []):
                pid = item.get("id")
                sub_tg_id = item.get("subscriber_tg_id")
                target_tg_id = item.get("target_tg_id")
                event_type = item.get("event_type", "new_report")
                if not pid or not sub_tg_id:
                    continue
                try:
                    push_lang = (item.get("subscriber_lang") or "en").strip() or "en"
                    if push_lang not in ("en", "zh", "tl"):
                        push_lang = "en"
                    if event_type == "blacklist_added":
                        text = t("alert_push_blacklist_added", lang=push_lang, tid=target_tg_id)
                    elif event_type == "online":
                        text = t("alert_push_online", lang=push_lang, tid=target_tg_id)
                    elif event_type == "offline":
                        text = t("alert_push_offline", lang=push_lang, tid=target_tg_id)
                    elif event_type == "report_spike":
                        text = t("alert_push_report_spike", lang=push_lang, tid=target_tg_id)
                    elif event_type == "wallet_change":
                        text = t("alert_push_wallet_change", lang=push_lang, tid=target_tg_id)
                    else:
                        text = t("alert_push_new_report", lang=push_lang, tid=target_tg_id)
                    await bot.send_message(sub_tg_id, text)
                    await alert_mark_sent(pid)
                except Exception as e:
                    logger.warning("Alert send failed for %s: %s", sub_tg_id, e)
            # 2) 被查通知（默认用 en，被查人语言未知）
            try:
                nc_data = await notify_checked_pending(limit=50)
                for item in (nc_data.get("items") or []):
                    pid = item.get("id")
                    target_tg_id = item.get("target_tg_id")
                    if not pid or not target_tg_id:
                        continue
                    try:
                        text = t("notify_checked_push", lang="en")
                        await bot.send_message(target_tg_id, text)
                        await notify_checked_mark_sent(pid)
                    except Exception as e:
                        logger.warning("Notify checked send failed for %s: %s", target_tg_id, e)
            except Exception as e:
                logger.warning("Notify checked poll error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Alert worker error: %s", e)


async def main():
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start.router)
    dp.include_router(check.router)
    dp.include_router(forward.router)
    dp.include_router(mycredit.router)
    dp.include_router(history.router)
    dp.include_router(alerts.router)
    dp.include_router(help_cmd.router)
    dp.include_router(invite.router)
    dp.include_router(report.router)
    dp.include_router(admin.router)
    dp.include_router(transaction.router)
    dp.include_router(group_scan.router)
    dp.include_router(verify.router)
    dp.include_router(privacy.router)
    dp.include_router(wallets.router)
    worker = asyncio.create_task(_alert_poll_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
