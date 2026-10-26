"""Точка входа: сборка Application, регистрация хендлеров."""
import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

import db
import pr

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))


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
    """Пример админ-команды — легко расширять новыми."""
    u = update.effective_user
    if not u or u.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return
    await update.message.reply_text(
        "📊 Админ-панель:\n"
        "• /check — проверить подписку\n"
        "• /stats — эта справка\n"
        "(сюда легко добавить новые команды)"
    )


# ---------- post_init / post_shutdown ----------

async def post_init(app: Application) -> None:
    await db.init_db()
    logger.info("Bot ready")


async def post_shutdown(app: Application) -> None:
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

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("check", pr.recheck_subscription))

    # Новые участники группы
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, pr.on_new_member)
    )

    # Все сообщения в целевой группе (кроме команд)
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
    app = build_app()
    logger.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
