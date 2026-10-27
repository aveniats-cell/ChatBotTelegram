"""2.3 Топ чата: счётчик сообщений, Топ день / Топ все, Моё место.

P1: track_message использует один batch (db.inc_message_and_day) вместо
трёх HTTP-запросов на каждое сообщение.
"""
import logging
from html import escape
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return
    chat = update.effective_chat
    if not chat:
        return
    try:
        await db.upsert_user(user.id, user.username, user.first_name)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ok = await db.inc_message_and_day(chat.id, user.id, day)
        if not ok:
            logger.warning("track_message: batch не выполнен (chat=%s user=%s)",
                           chat.id, user.id)
    except Exception as e:
        logger.warning("track_message: %s", e)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        deleted = await db.cleanup_old_days(keep_days=0)
        logger.info("cleanup_job: удалено строк messages_day: %s", deleted)
    except Exception as e:
        logger.warning("cleanup_job: %s", e)


def _medal(i: int) -> str:
    if i == 1:
        return "🥇"
    if i == 2:
        return "🥈"
    if i == 3:
        return "🥉"
    return f"{i}."


async def _build_top_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 50,
    title: str = "🏆 Топ чата",
) -> str:
    rows = await db.top_messages(chat_id, limit=limit)
    if not rows:
        return "Пока никто не писал — топ пуст."
    lines = [f"<b>{escape(title)}</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        name = str(uid)
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.first_name or chat.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"{_medal(i)} {escape(name)} — <b>{cnt}</b>")
    return "\n".join(lines)


async def _build_top_day_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 50,
) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = await db.top_messages_day(chat_id, day, limit=limit)
    if not rows:
        return "Сегодня ещё никто не писал — топ дня пуст."
    lines = [f"<b>🏆 Топ за день ({day})</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        name = str(uid)
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.first_name or chat.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"{_medal(i)} {escape(name)} — <b>{cnt}</b>")
    return "\n".join(lines)


async def cmd_top_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    text = await _build_top_text(context, chat.id, limit=50, title="🏆 Топ за всё время")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    text = await _build_top_day_text(context, chat.id, limit=50)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_my_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    res = await db.user_rank(chat.id, user.id)
    if not res:
        await update.message.reply_text("Вы ещё не писали в этом чате — места нет.")
        return
    count, rank, total = res
    await update.message.reply_text(
        f"📊 Ваше место: <b>{rank}/{total}</b>\n"
        f"Сообщений: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )
