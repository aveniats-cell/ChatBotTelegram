"""Глобальная система репутации пользователей Telegram."""
import logging
import re
import time
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)
_USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")


def _display_name(user_data: dict | None, fallback: str = "Пользователь") -> str:
    if not user_data:
        return escape(fallback)
    username = (user_data.get("username") or "").strip()
    first_name = (user_data.get("first_name") or "").strip()
    if first_name:
        return escape(first_name)
    if username:
        return "@" + escape(username)
    return f"ID {int(user_data.get('user_id') or 0)}"


async def _show_reputation(message, target_id: int, fallback: str = "Пользователь") -> None:
    stats = await db.reputation_get(target_id)
    user_data = await db.get_user(target_id)
    name = _display_name(user_data, fallback)
    text = (
        f"⭐ <b>Репутация: {name}</b>\n\n"
        f"➕ Плюсы: <b>{stats['plus']}</b>\n"
        f"➖ Минусы: <b>{stats['minus']}</b>\n"
        f"📊 Оценка: <b>{stats['score']:.1f}/10</b>\n"
        f"🗳 Всего голосов: <b>{stats['total']}</b>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_reputation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    requester = update.effective_user
    if not message or not requester:
        return

    # Реплай имеет приоритет, если имя пользователя не указано.
    parts = (message.text or "").strip().split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query and message.reply_to_message:
        target = message.reply_to_message.from_user
        if not target:
            await message.reply_text("Не удалось определить автора этого сообщения.")
            return
        await db.upsert_user(target.id, target.username, target.first_name)
        await _show_reputation(message, target.id, target.first_name)
        return

    if not query:
        await db.upsert_user(requester.id, requester.username, requester.first_name)
        await _show_reputation(message, requester.id, requester.first_name)
        return

    match = _USERNAME_RE.fullmatch(query)
    if not match:
        await message.reply_text("Формат: <b>Репутация</b> или <b>Репутация @username</b>.",
                                 parse_mode=ParseMode.HTML)
        return
    username = match.group(1)
    target_id = await db.get_user_by_username(username)
    if not target_id:
        await message.reply_text(
            "Пользователь не найден в базе бота. Он должен хотя бы раз написать в одном из чатов, где есть бот.")
        return
    await _show_reputation(message, target_id, "@" + username)


async def cmd_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    voter = update.effective_user
    replied = message.reply_to_message if message else None
    if not message or not voter or not replied:
        return

    target = replied.from_user
    if not target:
        await message.reply_text("Нельзя поставить репутацию этому сообщению: автор не определён.")
        return
    if target.is_bot:
        await message.reply_text("🤖 Ботам нельзя ставить репутацию.")
        return
    if voter.id == target.id:
        await message.reply_text("Нельзя голосовать за самого себя.")
        return

    raw = (message.text or "").strip()
    vote = 1 if raw == "+" else -1 if raw == "-" else 0
    if not vote:
        return

    await db.upsert_user(voter.id, voter.username, voter.first_name)
    await db.upsert_user(target.id, target.username, target.first_name)
    result = await db.reputation_cast_vote(voter.id, target.id, vote, int(time.time()), 10)

    if result == "cooldown":
        await message.reply_text("⏳ Голосовать можно не чаще одного раза в 10 секунд.")
    elif result == "same":
        await message.reply_text("Ты уже поставил этому пользователю такую оценку.")
    elif result == "created":
        await message.reply_text("✅ Голос учтён: " + ("➕" if vote > 0 else "➖"))
    elif result == "changed":
        await message.reply_text("🔄 Твой голос изменён на " + ("➕" if vote > 0 else "➖"))
    else:
        logger.error("Не удалось сохранить голос репутации: voter=%s target=%s", voter.id, target.id)
        await message.reply_text("⚠️ Не удалось сохранить голос. Попробуй позже.")
