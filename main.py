"""Точка входа: webhook-версия для Render Web Service (Free)."""
import os
import re
import logging
from datetime import time as dtime, timezone

from telegram import Update
from telegram.constants import ParseMode
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
import warns
import aura
import awards
import actions
import ai
import help_menu
import casino
import polls
import rules
import coins
import coin_market
import coin_broker
import coin_jobs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

if not ADMIN_ID:
    logger.error("main: ADMIN_ID не задан — админ-команды работать не будут!")

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = "webhook"

_AI_WORD = re.compile(r"(?i)^(ии|браин|браини|мозг|brainy|brain)(\s|$)")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    if u:
        await db.upsert_user(u.id, u.username, u.first_name)
    await msg.reply_text(
        "Привет! Я PR-бот.\n"
        "/help — список команд по ролям\n"
        "/check — подписка\n"
        "/ping — жив?\n"
        "/restart — сбросить контекст ИИ\n"
        "Профиль — профиль\n"
        "Казино — мини-казино\n"
        "Коины — магазин коинов\n"
        "Рынок — рынок коинов\n"
        "Панель брокера — портфель"
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("pong 🏓")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    ai.reset_history(user.id)
    await msg.reply_text("🔄 Контекст ИИ сброшен.")


async def _run_ai(user, msg, prompt: str) -> None:
    remain = await ai.try_acquire(user.id)
    if remain > 0:
        await msg.reply_text(f"⏳ Подожди {remain:.1f} сек.")
        return
    thinking = await msg.reply_text("🤔 Думаю…")
    answer = await ai.ask_ai(prompt, user_id=user.id)
    if not answer:
        try:
            await thinking.edit_text("⚠️ ИИ не ответил. Попробуй позже.")
        except Exception:
            pass
        return
    if len(answer) > 4000:
        answer = answer[:4000] + "…"
    try:
        await thinking.edit_text(answer)
    except Exception:
        try:
            await msg.reply_text(answer)
        except Exception:
            pass


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    text = (msg.text or "").strip()
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == context.bot.id:
            prompt = _AI_WORD.sub("", text).strip()
            if not prompt:
                await msg.reply_text("Напиши вопрос реплаем.")
                return
            await _run_ai(user, msg, prompt)
            return
    m = _AI_WORD.match(text)
    if not m:
        return
    prompt = text[m.end():].strip()
    if not prompt:
        await msg.reply_text(
            "Формат: <code>ИИ Привет</code> или реплай на сообщение бота.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _run_ai(user, msg, prompt)


async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    if not ADMIN_ID or not u or u.id != ADMIN_ID:
        await msg.reply_text("⛔ Только для админа.")
        return
    await msg.reply_text(
        "📊 Админ-панель:\n"
        "• +Шериф / -Шериф @user\n"
        "• +Комиссар / -Комиссар @user\n"
        "• Список шерифы / Список комиссары\n"
        "• Поймать @user (шериф)\n"
        "• +Мут 5ч Причина / -Мут @user\n"
        "• +Бан @user / -Бан @user\n"
        "• +Жалоба / +жб @user\n"
        "• +Пред / -Пред @user\n"
        "• +Награда Название / 5 @user\n"
        "• -Награда Название @user\n"
        "• Дать леща / Поцеловать / Обнять / Побить / Обоссать / Погладить\n"
        "• Засос / Расстрел / По шапке / Выстрел / Расцеловать / Сделать 18+\n"
        "• Пожать руку / Станцевать / Обнять всех / Погладить всех / Лещей всем / Тыкнуть / Защекотать\n"
        "• Опрос / описание /1/ Вариант1 / Вариант2 (5 мин)\n"
        "• Опрос ССС / описание /1/ Вариант1 / Вариант2 (5 мин)\n"
        "• Топ все / Топ день / Топ богачи / Мое место\n"
        "• Шип @u1 @u2 (с ИИ)\n"
        "• Сделать предложение / +Брак / -Брак / Мой брак / Топ браки\n"
        "• Профиль\n"
        "• хо @user 50 — крестики (комиссия 5%)\n"
        "• +Кредит 50 / -Кредит 50 — из казны\n"
        "• +Монеты / -Монеты @user\n"
        "• +Утил / -Утил @user / казна\n"
        "• Перевод 50 @user — комиссия 3%\n"
        "• Казна / +ВКассу 500 / ОбрезатьКасну 15000\n"
        "• Зарплата — инфо за сегодня\n"
        "• +ССС / -ССС / Список ССС\n"
        "• +Суд ССС / +Суд (с / Причина)\n"
        "• дроч / Аура / Топ аура\n"
        "• Казино монетка/кубик/слоты\n"
        "• ИИ / Браини / Мозг <запрос>\n"
        "• Правила — текст правил чата\n"
        "• Коины / Рынок / Панель брокера\n"
        "• /Defloult220099 — экстремальный дефолт\n"
        "• /ALLMONEY 100 — начислить всем по 100 монет\n"
        "• /restart /help"
    )


async def _cleanup_polls_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        n = polls.cleanup_expired_polls()
        if n:
            logger.info("polls cleanup: removed %d", n)
    except Exception as e:
        logger.warning("polls cleanup: %s", e)


async def post_init(app: Application) -> None:
    await db.init_db()
    await sss.restore_sss_members()
    await resolver.start_client()

    if app.job_queue:
        app.job_queue.run_daily(
            top.cleanup_job,
            time=dtime(hour=0, minute=5, tzinfo=timezone.utc),
        )
        app.job_queue.run_repeating(
            top.flush_job,
            interval=top.FLUSH_INTERVAL_SEC,
            first=top.FLUSH_INTERVAL_SEC,
        )
        app.job_queue.run_repeating(
            _cleanup_polls_job,
            interval=60,
            first=60,
        )
        economy.register_jobs(app)
        app.job_queue.run_daily(
            coin_jobs.daily_coin_income_job,
            time=dtime(hour=1, minute=0, tzinfo=timezone.utc),
        )
        app.job_queue.run_repeating(
            coin_jobs.hourly_price_history_job,
            interval=3600,
            first=300,
        )
    else:
        logger.warning("JobQueue не инициализирован")


async def post_shutdown(app: Application) -> None:
    try:
        await top._flush()
    except Exception as e:
        logger.warning("flush on shutdown: %s", e)
    await resolver.stop_client()
    await ai.close_session()
    await db.close_db()
    logger.info("Bot stopped")


async def _coins_input_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Маршрутизирует текстовый ввод для FSM создания/продажи коинов."""
    if await coins.create_input(update, context):
        return
    if await coin_market.sell_price_input(update, context):
        return


async def _coins_photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await coins.photo_input(update, context)


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
    app.add_handler(CommandHandler("help", help_menu.cmd_help))
    app.add_handler(CallbackQueryHandler(help_menu.help_callback, pattern=r"^help:"))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("casino", casino.cmd_casino))
    app.add_handler(CommandHandler("Defloult220099", economy.cmd_force_default))
    app.add_handler(CommandHandler("ALLMONEY", economy.cmd_all_money))

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

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Кредит(\s|$)"), economy.cmd_credit_take))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Кредит(\s|$)"), economy.cmd_credit_repay))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Монеты(\s|$)"), economy.cmd_admin_coins))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Монеты(\s|$)"), economy.cmd_admin_coins_minus))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Перевод(\s|$)"), economy.cmd_transfer))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Казна(\s|$)"), economy.cmd_treasury))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+ВКассу(\s|$)"), economy.cmd_treasury_add))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^ОбрезатьКасну(\s|$)"), economy.cmd_treasury_trim))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Зарплата(\s|$)"), economy.cmd_salary))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+богачи(\s|$)"), economy.cmd_top_rich))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Утил\s+\d+\s+казна(\s|$)"),
                                   economy.cmd_burn_treasury))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Утил(\s|$)"),
                                   economy.cmd_burn_user))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Утил\s+\d+\s+казна(\s|$)"),
                                   economy.cmd_mint_treasury))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Утил(\s|$)"),
                                   economy.cmd_mint_user))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^казино(\s|$)"), casino.cmd_casino,
    ))
    app.add_handler(CallbackQueryHandler(
        casino.casino_help_callback, pattern=r"^cas:help:",
    ))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+все(\s|$)"), top.cmd_top_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+день(\s|$)"), top.cmd_top_day))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мое\s+место(\s|$)"), top.cmd_my_place))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Шип(\s|$)"), ship.cmd_ship))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Сделать\s+предложение(\s|$)"), wedding.cmd_propose))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Брак(\s|$)"), wedding.cmd_force_divorce_or_self))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Брак(\s|$)"), wedding.cmd_force_marriage))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Мой\s+брак(\s|$)"), wedding.cmd_marriage_info))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+браки(\s|$)"), wedding.cmd_marriage_top))
    app.add_handler(CallbackQueryHandler(wedding.wedding_callback, pattern=r"^wed:"))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Профиль(\s|$)"),
                                   profile.cmd_profile))
    app.add_handler(CallbackQueryHandler(profile.profile_callback,
                                         pattern=r"^prof:"))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^хо(\s|$)"), tictactoe.cmd_challenge))
    app.add_handler(CallbackQueryHandler(tictactoe.ttt_callback, pattern=r"^ttt:"))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+ССС(\s|$)"), sss.cmd_sss_add))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]ССС(\s|$)"), sss.cmd_sss_remove))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Список\s+ССС(\s|$)"), sss.cmd_sss_list))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Суд\s+ССС(\s|$)"), sss.cmd_sss_trial))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Суд(\s|$)"), sss.cmd_public_trial))
    app.add_handler(CallbackQueryHandler(sss.sss_trial_callback, pattern=r"^ssstrial:"))
    app.add_handler(CallbackQueryHandler(sss.public_trial_callback, pattern=r"^pubtrial:"))
    app.add_handler(CallbackQueryHandler(sss.public_admin_callback, pattern=r"^pubadm:"))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^правила(\s+чата)?\s*[.!?]*\s*$"),
        rules.cmd_rules,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Опрос\s+ССС(\s|/)"),
        polls.cmd_poll_sss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Опрос(?!\s+ССС)(\s|/)"),
        polls.cmd_poll_public,
    ))
    app.add_handler(CallbackQueryHandler(
        polls.poll_callback, pattern=r"^poll:",
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Жалоба|ЖБ|Жал)(\s|$)"),
        complaints.cmd_complaint,
    ))
    app.add_handler(CallbackQueryHandler(complaints.complaint_callback,
                                         pattern=r"^compl:"))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_warn,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−](Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_unwarn,
    ))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^дроч(\s|$)"),
                                   aura.cmd_droch))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Топ\s+аура(\s|$)"),
                                   aura.cmd_aura_top))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Аура(\s|$)"),
                                   aura.cmd_aura))

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\+Награда(\s|$)"),
                                   awards.cmd_award))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^[-–−]Награда(\s|$)"),
                                   awards.cmd_unaward))

    # --- старые действия ---
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Дать\s+леща(\s|$)"),
                                   actions.cmd_slap))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Поцеловать(\s|$)"),
                                   actions.cmd_kiss))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Обнять(\s|$)"),
                                   actions.cmd_hug))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Побить(\s|$)"),
                                   actions.cmd_hit))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Обоссать(\s|$)"),
                                   actions.cmd_piss))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Погладить(\s|$)"),
                                   actions.cmd_pat))

    # --- новые действия ---
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Засос(\s|$)"),
                                   actions.cmd_kiss_deep))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Расстрел(\s|$)"),
                                   actions.cmd_shoot))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Дать\s+по\s+шапке(\s|$)"),
                                   actions.cmd_bonk))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Выстрел(\s|$)"),
                                   actions.cmd_gunshot))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Расцеловать(\s|$)"),
                                   actions.cmd_kiss_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Сделать\s+18\+?(\s|$)"),
                                   actions.cmd_make18))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Станцевать(\s|$)"),
                                   actions.cmd_dance))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Обнять\s+всех(\s|$)"),
                                   actions.cmd_hug_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Погладить\s+всех(\s|$)"),
                                   actions.cmd_pat_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Лещей\s+всем(\s|$)"),
                                   actions.cmd_slap_all))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Тыкнуть(\s|$)"),
                                   actions.cmd_poke))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Защекотать(\s|$)"),
                                   actions.cmd_tickle))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(Пожать\s+руку|Рукопожатие)(\s|$)"),
                                   actions.cmd_handshake))

    # --- коины ---
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Коины(\s|$)"), coins.cmd_coins))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Рынок(\s|$)"), coin_market.cmd_market))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^Панель\s+брокера(\s|$)"), coin_broker.cmd_broker))

    # coin:view / buy / chart / mine / main / new — один роутер
    app.add_handler(CallbackQueryHandler(
        coins.coin_callback,
        pattern=r"^coin:(view|buy|chart|mine|main|new)(:|$)",
    ))
    app.add_handler(CallbackQueryHandler(coins.tier_callback, pattern=r"^coin:tier:"))
    app.add_handler(CallbackQueryHandler(coins.photo_decision_callback, pattern=r"^coin:photo_(ok|no)$"))
    app.add_handler(CallbackQueryHandler(coin_market.market_callback, pattern=r"^cmkt:"))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^(ии|браин|браини|мозг|brainy|brain)(\s|$)"),
        cmd_ai,
    ))

    # FSM коинов: текст и фото — раньше track_message, чтобы перехватить
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _coins_input_router), group=-2)
    app.add_handler(MessageHandler(filters.PHOTO, _coins_photo_router), group=-2)

    if CHAT_ID:
        app.add_handler(MessageHandler(
            filters.REPLY & filters.Chat(chat_id=CHAT_ID),
            cmd_ai,
        ))

        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CHAT_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                top.track_message,
            ),
            group=1,
        )

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, pr.on_new_member))
    if CHAT_ID:
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
