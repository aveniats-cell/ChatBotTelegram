"""Точка входа: webhook-версия для Render Web Service (Free).

- set_webhook вызывается только внутри run_webhook (в post_init убран),
  чтобы не было Flood control exceeded.
- track_message использует два отдельных _exec вместо batch
  (libsql-client 0.3.1 через HTTP отдаёт 400 на batch).
- delete_webhook в shutdown убран — не создаём окно недоставки.
- cleanup_job запускается с tzinfo=UTC (единообразно с economy).
- Добавлены +Жалоба (всем) и +Предупреждение (шериф/комиссар/админ).
"""
import os
import logging
from datetime import time as dtime, timezone

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
import resolver
import pr
import roles
import top
import ship
import wedding
import profile
import economy
import tictactoe
import sss
import complaints
import warnings as warnings_mod

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = "webhook"


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        await db.upsert_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "Привет! Я PR-бот.\n"
        "/check — подписка\n"
        "/ping — жив?\n"
        "Профиль — посмотреть профиль"
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")


async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not ADMIN_ID or not u or u.id != ADMIN_ID:
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
        "• +Брак @u1 @u2 (принудительно)\n"
        "• -Брак (свой) / -Брак @user (админ)\n"
        "• Мой брак / Профиль\n"
        "• хо @user 50 — крестики-нолики\n"
        "• +Кредит 50 / -Кредит 50\n"
        "• +Монеты 100 @user (админ)\n"
        "• +ССС @user / -ССС @user / Список ССС\n"
        "• +Суд ССС @user / +Суд @user\n"
        "• +Жалоба @user (всем)\n"
        "• +Предупреждение @user (шериф/комиссар/админ)"
    )


async def post_init(app: Application) -> None:
    await db.init_db()
    await sss.restore_sss_members()
    await resolver.start_client()

    # set_webhook здесь НЕ вызываем: run_webhook сам выставит webhook
    # с нужным url_path и drop_pending_updates. Двойной вызов даёт
    # "Flood control exceeded".

    if app.job_queue:
        app.job_queue.run_daily(
            top.cleanup_job,
            time=dtime(hour=0, minute=5, tzinfo=timezone.utc),
        )
        logger.info("JobQueue: ежедневная очистка messages_day запланирована")
        economy.register_jobs(app)
    else:
        logger.warning("JobQueue не инициализирован — проверь "
                       "python-telegram-bot[webhooks,job-queue]")


async def post_shutdown(app: Application) -> None:
    await resolver.stop_client()
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
    app.add_error_handler(_error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("check", pr.recheck_subscription))

    # Роли
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

    # Экономика
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Кредит(\s|$)"), economy.cmd_credit_take))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Кредит(\s|$)"), economy.cmd_credit_repay))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Монеты(\s|$)"), economy.cmd_admin_coins))

    # Топ
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+все(\s|$)"), top.cmd_top_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+день(\s|$)"), top.cmd_top_day))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мое\s+место(\s|$)"), top.cmd_my_place))

    # Шип
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Шип(\s|$)"), ship.cmd_ship))

    # Свадьба
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Сделать\s+предложение(\s|$)"), wedding.cmd_propose))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Брак(\s|$)"), wedding.cmd_force_divorce_or_self))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Брак(\s|$)"), wedding.cmd_force_marriage))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мой\s+брак(\s|$)"), wedding.cmd_marriage_info))
    app.add_handler(CallbackQueryHandler(wedding.wedding_callback, pattern=r"^wed:"))

    # Профиль
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Профиль(\s|$)"), profile.cmd_profile))

    # Крестики-нолики
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^хо(\s|$)"), tictactoe.cmd_challenge))
    app.add_handler(CallbackQueryHandler(tictactoe.ttt_callback, pattern=r"^ttt:"))

    # ССС и суды — порядок ВАЖЕН: +Суд ССС раньше, чем +Суд
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+ССС(\s|$)"), sss.cmd_sss_add))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]ССС(\s|$)"), sss.cmd_sss_remove))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Список\s+ССС(\s|$)"), sss.cmd_sss_list))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Суд\s+ССС(\s|$)"), sss.cmd_sss_trial))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Суд(\s|$)"), sss.cmd_public_trial))
    app.add_handler(CallbackQueryHandler(sss.sss_trial_callback, pattern=r"^ssstrial:"))
    app.add_handler(CallbackQueryHandler(sss.public_trial_callback, pattern=r"^pubtrial:"))
    app.add_handler(CallbackQueryHandler(sss.public_admin_callback, pattern=r"^pubadm:"))

    # Жалобы и предупреждения
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Жалоба(\s|$)"),
                                   complaints.cmd_complaint))
    app.add_handler(CallbackQueryHandler(complaints.complaint_callback,
                                         pattern=r"^compl:"))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Предупреждение(\s|$)"),
                                   warnings_mod.cmd_warn))

    # Счётчик сообщений
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=CHAT_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            top.track_message,
        ),
        group=1,
    )

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, pr.on_new_member))
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
        raise SystemExit("WEBHOOK_URL не задан")
    if not WEBHOOK_URL.startswith("https://"):
        raise SystemExit("WEBHOOK_URL должен начинаться с https://")

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
