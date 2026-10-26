"""Точка входа: webhook-версия для Render Web Service (Free)."""
import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import db
import pr
import roles
import top
import ship
import wedding

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = "webhook"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        await db.upsert_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "Привет! Я PR-бот.\n"
        "Проверяю подписку, роли, муты и баны, веду топ чата, шип и свадьбы.\n\n"
        "/check — проверить подписку\n"
        "/ping — проверить, что бот жив"
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")


async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or u.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return
    await update.message.reply_text(
        "📊 Админ-панель:\n"
        "• +Шериф / -Шериф @user\n"
        "• +Комиссар / -Комиссар @user\n"
        "• Список шерифы / Список комиссары\n"
        "• Поймать @user (шериф)\n"
        "• +Мут @user 1ч 30м / -Мут @user\n"
        "• +Бан @user / -Бан @user\n"
        "• Топ все / Топ день / Мое место\n"
        "• Шип @u1 @u2\n"
        "• Сделать предложение @user\n"
        "• Расторгнуть брак / Мой брак"
    )


async def post_init(app: Application) -> None:
    await db.init_db()
    full_url = f"{WEBHOOK_URL}/{WEBHOOK_PATH}"
    await app.bot.set_webhook(
        url=full_url, drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Webhook set to %s", full_url)


async def post_shutdown(app: Application) -> None:
    try:
        await app.bot.delete_webhook()
    except Exception as e:
        logger.warning("delete_webhook: %s", e)
    await db.close_db()
    logger.info("Bot stopped")


def build_app() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("check", pr.recheck_subscription))

    # ---------- роли ----------
    app.add_handler(MessageHandler(filters.Regex(r"^\+Шериф(\s|$)"), roles.grant_sheriff))
    app.add_handler(MessageHandler(filters.Regex(r"^[-–−]Шериф(\s|$)"), roles.revoke_sheriff))
    app.add_handler(MessageHandler(filters.Regex(r"^\+Комиссар(\s|$)"), roles.grant_commissar))
    app.add_handler(MessageHandler(filters.Regex(r"^[-–−]Комиссар(\s|$)"), roles.revoke_commissar))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Список\s+шерифы(\s|$)"), roles.list_sheriffs))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Список\s+комиссары(\s|$)"), roles.list_commissars))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Поймать(\s|$)"), roles.sheriff_catch))
    app.add_handler(CallbackQueryHandler(roles.catch_callback, pattern=r"^catch:"))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Мут(\s|$)"), roles.mute_user))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Мут(\s|$)"), roles.unmute_user))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Бан(\s|$)"), roles.ban_user))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Бан(\s|$)"), roles.unban_user))

    # ---------- 2.3 Топ ----------
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+все(\s|$)"), top.cmd_top_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+день(\s|$)"), top.cmd_top_day))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мое\s+место(\s|$)"), top.cmd_my_place))

    # ---------- 2.4 Шип ----------
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Шип(\s|$)"), ship.cmd_ship))

    # ---------- 2.5 Свадьба ----------
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Сделать\s+предложение(\s|$)"), wedding.cmd_propose))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Расторгнуть\s+брак(\s|$)"), wedding.cmd_divorce))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мой\s+брак(\s|$)"), wedding.cmd_marriage_info))
    app.add_handler(CallbackQueryHandler(wedding.wedding_callback, pattern=r"^wed:"))

    # ---------- трекинг сообщений для топа ----------
    # group=1 — работает параллельно с check_and_delete (group=0)
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=CHAT_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            top.track_message,
        ),
        group=1,
    )

    # ---------- существующая логика подписки ----------
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, pr.on_new_member)
    )
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=CHAT_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            pr.check_and_delete,
        )
    )

    return app


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    if not WEBHOOK_URL:
        raise SystemExit("WEBHOOK_URL не задан (например https://my-bot.onrender.com)")

    app = build_app()
    logger.info("Starting webhook server on port %s", PORT)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
