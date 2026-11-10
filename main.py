"""Точка входа: webhook-версия для Render Web Service (Free)."""

import os
import re
import logging
from datetime import time as dtime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, ContextTypes, filters,
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
import aura_admin
import awards
import actions
import ai
import help_menu
import casino
import polls
import rules
import command_registry   # noqa: F401 — должен идти до commands_dop
import commands_dop       # noqa: F401 — регистрирует команды в реестре
import commands_dop_handlers
import shop
import reputation
import blacklist
import helper


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

# Обращения к Ксении
_AI_WORD = re.compile(
    r"(?i)^(ксюш(?:а|и)?|кеся|ксения|ксюха|ксю)(\s|$)"
)


async def _error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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
        "Магазин — пакеты команд"
    )


async def cmd_ping(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("pong 🏓")


async def cmd_delete_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Удаляет сообщение-реплай и команду -смс."""

    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if not await roles._require_role(update, ("commissar",)):
        await msg.reply_text(
            "⛔ Только владелец или комиссар могут использовать -смс."
        )
        return

    target = msg.reply_to_message

    if not target:
        await msg.reply_text(
            "Ответь командой <code>-смс</code> на сообщение, которое нужно удалить.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=target.message_id,
        )
    except Exception as e:
        logger.warning(
            "cmd_delete_message: не удалось удалить цель: "
            "chat_id=%s message_id=%s: %s",
            chat.id,
            target.message_id,
            e,
        )
        await msg.reply_text(
            "⚠️ Не удалось удалить отмеченное сообщение. "
            "Проверь права бота администратора и возможность удаления этого сообщения."
        )
        return

    try:
        await msg.delete()
    except Exception as e:
        logger.warning(
            "cmd_delete_message: цель удалена, но команда осталась: "
            "chat_id=%s message_id=%s: %s",
            chat.id,
            msg.message_id,
            e,
        )


async def cmd_restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg:
        return

    ai.reset_history(user.id)
    await msg.reply_text("🔄 Контекст ИИ сброшен.")


def _split_telegram_text(
    text: str,
    limit: int = 3900,
) -> list[str]:
    """Делит длинный ответ на сообщения."""

    text = str(text or "")
    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)

        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)

        if cut < limit // 2:
            cut = limit

        chunks.append(text[:cut].strip())
        text = text[cut:].strip()

    if text:
        chunks.append(text)

    return chunks or [""]


async def _run_ai(user, msg, prompt: str) -> None:
    """Отправляет GIF до генерации, удаляет её и только затем отвечает."""
    remain = await ai.try_acquire(user.id)
    if remain > 0:
        # Не отправляем дополнительное сообщение при cooldown.
        return

    gif_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "videomateria.gif",
    )

    # GIF — необязательный индикатор ожидания. Если Telegram временно
    # недоступен или файл отсутствует, сам запрос к ИИ всё равно выполняется.
    thinking = None
    if os.path.isfile(gif_path):
        try:
            with open(gif_path, "rb") as gif_file:
                thinking = await msg.reply_animation(animation=gif_file)
            logger.info(
                "GIF отправлена: message_id=%s user_id=%s",
                thinking.message_id,
                user.id,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось отправить GIF (запрос ИИ продолжится): "
                "user_id=%s error=%s",
                user.id,
                exc,
                exc_info=True,
            )
    else:
        logger.warning("GIF не найдена: %s — продолжаю без анимации", gif_path)

    answer = None
    error_text = None
    chunks = []

    try:
        logger.info("Начинаю генерацию ИИ: user_id=%s", user.id)
        answer = await ai.ask_ai(prompt, user_id=user.id)
        logger.info("Генерация ИИ завершена: user_id=%s", user.id)

        needed = ai.count_words(prompt[:ai.MAX_PROMPT_CHARS].strip())

        if not answer:
            left = await db.get_ai_tokens(user.id)
            if left < needed:
                error_text = (
                    f"🧠 Недостаточно токенов. Нужно {needed}, у тебя {left}.\n"
                    "Купи токены в «Магазине» → «Токены ИИ»."
                )
            else:
                error_text = (
                    "⚠️ ИИ не ответил. Если токены были списаны, "
                    "они возвращены. Попробуй позже."
                )
        elif answer.startswith("🧠 Недостаточно токенов:"):
            error_text = answer
        else:
            left = await db.get_ai_tokens(user.id)
            output_cost = ai.count_words(answer)
            total_spent = needed + output_cost
            chunks = _split_telegram_text(answer)
            chunks[-1] += (
                f"\n\n🧠 Списано: {total_spent} "
                f"(запрос: {needed}, ответ: {output_cost}) · Осталось: {left}"
            )

    except Exception:
        logger.exception("Ошибка генерации ИИ: user_id=%s", user.id)
        error_text = "⚠️ Ошибка ИИ. Попробуй позже."

    finally:
        # GIF удаляется после окончания генерации и ДО текстового ответа.
        if thinking is not None:
            try:
                await thinking.delete()
                logger.info(
                    "GIF удалена: message_id=%s user_id=%s",
                    thinking.message_id,
                    user.id,
                )
            except Exception:
                logger.exception(
                    "Не удалось удалить GIF: message_id=%s user_id=%s",
                    thinking.message_id,
                    user.id,
                )

    # Отправляем текст только после попытки удалить GIF.
    try:
        if error_text:
            await msg.reply_text(error_text)
        else:
            for chunk in chunks:
                await msg.reply_text(chunk)
    except Exception:
        logger.exception("Не удалось отправить ответ ИИ: user_id=%s", user.id)


async def cmd_ai(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg:
        return

    text = (msg.text or "").strip()
    match = _AI_WORD.match(text)

    if not match:
        return

    prompt = text[match.end():].strip()

    if not prompt:
        await msg.reply_text(
            "Формат: <code>Ксю Привет</code> или "
            "<code>Ксения Как дела?</code>.\n"
            "1 слово в запросе = 1 токен. "
            "Токены можно купить в «Магазине».",
            parse_mode=ParseMode.HTML,
        )
        return

    await _run_ai(user, msg, prompt)


async def cmd_admin_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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
        "• /MarriedClean @user\n"
        "• Профиль\n"
        "• хо @user 50 — крестики (комиссия 5%)\n"
        "• +Кредит 50 / -Кредит 50\n"
        "• Сберкнижка / +Сберкнижка 500 / -Сберкнижка 200\n"
        "• +Монеты / -Монеты @user\n"
        "• +Утил / -Утил @user / казна\n"
        "• Перевод 50 @user\n"
        "• Казна / +ВКассу 500 / ОбрезатьКасну 15000\n"
        "• Зарплата\n"
        "• +ССС / -ССС / Список ССС\n"
        "• +Суд ССС / +Суд\n"
        "• дроч / Аура / Топ аура\n"
        "• Казино монетка/кубик/слоты\n"
        "• Ксюша / Ксения / Ксю <запрос>\n"
        "• Правила / Магазин\n"
        "• -смс\n"
        "• /Defloult220099\n"
        "• /ALLMONEY 100\n"
        "• /restart /help"
    )


async def _cleanup_polls_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        n = polls.cleanup_expired_polls()

        if n:
            logger.info("polls cleanup: removed %d", n)

    except Exception as e:
        logger.warning("polls cleanup: %s", e)


async def post_init(app: Application) -> None:
    await db.init_db()
    await helper.init_db()
    await sss.restore_sss_members()
    await resolver.start_client()
    await commands_dop_handlers.sync_to_db()

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


def build_app() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_error_handler(_error_handler)

    # Личный кабинет предложки: обычные пользователи не попадают
    # в обработчики групповых команд, экономики и активности.
    # Администратор сохраняет существующий функционал в личке.
    app.add_handler(
        TypeHandler(
            Update,
            lambda update, context: helper.handle_private_update(
                update, context, ADMIN_ID
            ),
        ),
        group=-2,
    )

    # Глобальный фильтр заблокированных пользователей.
    app.add_handler(
        TypeHandler(
            Update,
            blacklist.guard_blacklisted_update,
        ),
        group=-3,
    )

    app.add_handler(CommandHandler("banbot", blacklist.cmd_banbot))
    app.add_handler(CommandHandler("unbanbot", blacklist.cmd_unbanbot))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("check", pr.recheck_subscription))
    app.add_handler(CommandHandler("help", help_menu.cmd_help))
    app.add_handler(
        CallbackQueryHandler(
            help_menu.help_callback,
            pattern=r"^help:",
        )
    )
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("casino", casino.cmd_casino))
    app.add_handler(
        CommandHandler("auraplush", aura_admin.cmd_aura_plush)
    )
    app.add_handler(
        CommandHandler("Defloult220099", economy.cmd_force_default)
    )
    app.add_handler(
        CommandHandler("ALLMONEY", economy.cmd_all_money)
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"^\+Шериф(\s|$)"),
        roles.grant_sheriff,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[-–−]Шериф(\s|$)"),
        roles.revoke_sheriff,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^\+Комиссар(\s|$)"),
        roles.grant_commissar,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[-–−]Комиссар(\s|$)"),
        roles.revoke_commissar,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+шерифы(\s|$)"),
        roles.list_sheriffs,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+комиссары(\s|$)"),
        roles.list_commissars,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Поймать(\s|$)"),
        roles.sheriff_catch,
    ))
    app.add_handler(
        CallbackQueryHandler(
            roles.catch_callback,
            pattern=r"^catch:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^-смс\s*$"),
        cmd_delete_message,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Мут(\s|$)"),
        roles.mute_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Мут(\s|$)"),
        roles.unmute_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Бан(\s|$)"),
        roles.ban_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Бан(\s|$)"),
        roles.unban_user,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Баланс\s*$"),
        economy.cmd_balance,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сберкнижка\s*$"),
        economy.cmd_savings_book,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Сберкнижка(\s|$)"),
        economy.cmd_savings_deposit,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Сберкнижка(\s|$)"),
        economy.cmd_savings_withdraw,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Кредит(\s|$)"),
        economy.cmd_credit_take,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Кредит(\s|$)"),
        economy.cmd_credit_repay,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Монеты(\s|$)"),
        economy.cmd_admin_coins,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Монеты(\s|$)"),
        economy.cmd_admin_coins_minus,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Перевод(\s|$)"),
        economy.cmd_transfer,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Казна(\s|$)"),
        economy.cmd_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+ВКассу(\s|$)"),
        economy.cmd_treasury_add,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^ОбрезатьКасну(\s|$)"),
        economy.cmd_treasury_trim,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Зарплата(\s|$)"),
        economy.cmd_salary,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+богачи(\s|$)"),
        top.cmd_top_rich,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Утил\s+\d+\s+казна(\s|$)"),
        economy.cmd_burn_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Утил(\s|$)"),
        economy.cmd_burn_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Утил\s+\d+\s+казна(\s|$)"),
        economy.cmd_mint_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Утил(\s|$)"),
        economy.cmd_mint_user,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^казино(\s|$)"),
        casino.cmd_casino,
    ))
    app.add_handler(
        CallbackQueryHandler(
            casino.casino_help_callback,
            pattern=r"^cas:help:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+все(\s|$)"),
        top.cmd_top_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+день(\s|$)"),
        top.cmd_top_day,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Мое\s+место(\s|$)"),
        top.cmd_my_place,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Шип(\s|$)"),
        ship.cmd_ship,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сделать\s+предложение(\s|$)"),
        wedding.cmd_propose,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Брак(\s|$)"),
        wedding.cmd_force_divorce_or_self,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Брак(\s|$)"),
        wedding.cmd_force_marriage,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Мой\s+брак(\s|$)"),
        wedding.cmd_marriage_info,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+браки(\s|$)"),
        wedding.cmd_marriage_top,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^/marriedclean(?:@\w+)?(?:\s|$)"),
        wedding.cmd_married_clean,
    ))
    app.add_handler(
        CallbackQueryHandler(
            wedding.wedding_callback,
            pattern=r"^wed:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Репутация(?:\s+.*)?$"),
        reputation.cmd_reputation,
    ))
    app.add_handler(MessageHandler(
        filters.REPLY & filters.Regex(r"^[+-]$"),
        reputation.cmd_vote,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Профиль(\s|$)"),
        profile.cmd_profile,
    ))
    app.add_handler(
        CallbackQueryHandler(
            profile.profile_callback,
            pattern=r"^prof:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^хо(\s|$)"),
        tictactoe.cmd_challenge,
    ))
    app.add_handler(
        CallbackQueryHandler(
            tictactoe.ttt_callback,
            pattern=r"^ttt:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+ССС(\s|$)"),
        sss.cmd_sss_add,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]ССС(\s|$)"),
        sss.cmd_sss_remove,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+ССС(\s|$)"),
        sss.cmd_sss_list,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Суд\s+ССС(\s|$)"),
        sss.cmd_sss_trial,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Суд(\s|$)"),
        sss.cmd_public_trial,
    ))
    app.add_handler(
        CallbackQueryHandler(
            sss.sss_trial_callback,
            pattern=r"^ssstrial:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            sss.public_trial_callback,
            pattern=r"^pubtrial:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            sss.public_admin_callback,
            pattern=r"^pubadm:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(rules.RULES_RE),
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
    app.add_handler(
        CallbackQueryHandler(
            polls.poll_callback,
            pattern=r"^poll:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Жалоба|ЖБ|Жал)(\s|$)"),
        complaints.cmd_complaint,
    ))
    app.add_handler(
        CallbackQueryHandler(
            complaints.complaint_callback,
            pattern=r"^compl:",
        )
    )
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_warn,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−](Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_unwarn,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^дроч(\s|$)"),
        aura.cmd_droch,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+аура(\s|$)"),
        aura.cmd_aura_top,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Аура(\s|$)"),
        aura.cmd_aura,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Награда(\s|$)"),
        awards.cmd_award,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Награда(\s|$)"),
        awards.cmd_unaward,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Дать\s+леща(\s|$)"),
        actions.cmd_slap,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Поцеловать(\s|$)"),
        actions.cmd_kiss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обнять(\s|$)"),
        actions.cmd_hug,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Побить(\s|$)"),
        actions.cmd_hit,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обоссать(\s|$)"),
        actions.cmd_piss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Погладить(\s|$)"),
        actions.cmd_pat,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Засос(\s|$)"),
        actions.cmd_kiss_deep,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Расстрел(\s|$)"),
        actions.cmd_shoot,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Дать\s+по\s+шапке(\s|$)"),
        actions.cmd_bonk,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Выстрел(\s|$)"),
        actions.cmd_gunshot,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Расцеловать(\s|$)"),
        actions.cmd_kiss_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сделать\s+18\+?(\s|$)"),
        actions.cmd_make18,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Станцевать(\s|$)"),
        actions.cmd_dance,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обнять\s+всех(\s|$)"),
        actions.cmd_hug_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Погладить\s+всех(\s|$)"),
        actions.cmd_pat_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Лещей\s+всем(\s|$)"),
        actions.cmd_slap_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Тыкнуть(\s|$)"),
        actions.cmd_poke,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Защекотать(\s|$)"),
        actions.cmd_tickle,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^(Пожать\s+руку|Рукопожатие)(\s|$)"),
        actions.cmd_handshake,
    ))

    # ИИ Ксения — только обращения к Ксюше.
    app.add_handler(MessageHandler(
        filters.Regex(_AI_WORD),
        cmd_ai,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Магазин(\s|$)"),
        shop.cmd_shop,
    ))
    app.add_handler(
        CallbackQueryHandler(
            shop.shop_callback,
            pattern=r"^shop:",
        )
    )

    commands_dop_handlers.register(app)

    if CHAT_ID:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CHAT_ID)
                & ~filters.COMMAND
                & ~filters.StatusUpdate.ALL,
                top.track_message,
            ),
            group=1,
        )

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            pr.on_new_member,
        )
    )

    if CHAT_ID:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CHAT_ID)
                & ~filters.COMMAND
                & ~filters.StatusUpdate.ALL,
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
