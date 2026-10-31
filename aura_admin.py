"""Административное начисление ауры пользователям."""
import logging
import os
import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)
_USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")
_MAX_AURA_GRANT = 1_000_000


async def cmd_aura_plush(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Формат: /auraplush КОЛИЧЕСТВО @username. Только ADMIN_ID."""
    message = update.effective_message
    actor = update.effective_user
    if not message:
        return

    admin_id = int(os.getenv("ADMIN_ID", "0") or "0")
    if not actor or not admin_id or actor.id != admin_id:
        await message.reply_text("⛔ Команда доступна только главному администратору.")
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.reply_text("Используй эту команду в группе, где нужно начислить ауру.")
        return

    if len(context.args) != 2:
        await message.reply_text(
            "Формат: <code>/auraplush 100 @username</code>\n"
            "Количество должно быть от 1 до 1 000 000.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        amount = int(context.args[0])
    except (TypeError, ValueError):
        await message.reply_text("Количество ауры должно быть целым числом.")
        return
    if not 1 <= amount <= _MAX_AURA_GRANT:
        await message.reply_text(f"Количество должно быть от 1 до {_MAX_AURA_GRANT}.")
        return

    match = _USERNAME_RE.fullmatch(context.args[1])
    if not match:
        await message.reply_text("Укажи корректный username: @username.")
        return

    username = match.group(1)
    target_id = await db.get_user_by_username(username)
    if not target_id:
        await message.reply_text(
            "Пользователь не найден в базе бота. Он должен хотя бы раз написать "
            "в одном из чатов, где есть бот."
        )
        return

    target = await db.get_user(target_id)
    if not target:
        await message.reply_text("Не удалось получить данные пользователя из базы.")
        return

    ok = await db.aura_add_points(message.chat_id, int(target_id), amount)
    if not ok:
        logger.error("aura plush failed: chat_id=%s target_id=%s amount=%s",
                     message.chat_id, target_id, amount)
        await message.reply_text("❌ Не удалось начислить ауру. Проверь логи базы данных.")
        return

    current = await db.aura_get(message.chat_id, int(target_id))
    total = int(current["value"]) if current else amount
    display = escape(target.get("first_name") or ("@" + username))
    await message.reply_text(
        f"✨ Пользователю <b>{display}</b> начислено <b>+{amount}</b> ауры.\n"
        f"Текущая аура в этом чате: <b>{total}</b>.",
        parse_mode=ParseMode.HTML,
    )
