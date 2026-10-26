"""Точка входа: webhook-версия для Render Web Service (Free)."""
import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db
import pr

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

# Render сам подставит PORT; для webhook нужен публичный URL
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")  # https://your-app.onrender.com
WEBHOOK_PATH = "webhook"


# ---------- служебные команды ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        await db.upsert_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "Привет! Я PR-бот.\n"
        "Я проверяю подписку на канал и удаляю сообщения у неподписанных.\n\n"
        "Команды:\n"
        "/check — проверить свою подписку\n"
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
        "• /check — проверить подписку\n"
        "• /stats — эта справка"
    )


# ---------- hooks ----------

async def post_init(app: Application) -> None:
    await db.init_db()
    full_url = f"{WEBHOOK_URL}/{WEBHOOK_PATH}"
    await app.bot.set_webhook(
        url=full_url,
        drop_pending_updates=True,
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


# ---------- сборка ----------

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
