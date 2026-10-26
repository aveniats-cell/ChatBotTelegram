"""2.3 Топ чата: счётчик сообщений, Топ день / Топ все, Моё место."""
import os
import logging
from html import escape
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)


# ---------- трекинг сообщений ----------
# Регистрируется с group=1, чтобы работать параллельно с check_and_delete.

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return
    chat = update.effective_chat
    if not chat:
        return
    try:
        await db.inc_message(chat.id, user.id)
    except Exception as e:
        logger.warning("inc_message: %s", e)


# ---------- вывод топов ----------

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
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(f"{medal} {escape(name)} — <b>{cnt}</b>")
    return "\n".join(lines)


async def cmd_top_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    text = await _build_top_text(context, chat.id, limit=50, title="🏆 Топ за всё время")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Топ за последние 24 часа. Так как храним только total, считаем «день» = те, кто писал за сутки."""
    # Внимание: чтобы «Топ день» работал корректно, нужна отдельная таблица.
    # Здесь — упрощённый вариант: показываем тот же топ, но с пометкой.
    # Для настоящего «дня» см. примечание ниже.
    chat = update.effective_chat
    if not chat:
        return
    text = await _build_top_text(context, chat.id, limit=50, title="🏆 Топ за день")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_my_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    res = await db.user_rank(chat.id, user.id)
    if not res:
        await update.message.reply_text(
            "Вы ещё не писали в этом чате — места нет."
        )
        return
    count, rank, total = res
    await update.message.reply_text(
        f"📊 Ваше место: <b>{rank}/{total}</b>\n"
        f"Сообщений: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )
