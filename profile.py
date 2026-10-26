"""Профиль: имя, смс, брак, монеты, кредит, ССС, роль, статистика."""
import os
import re
import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает user_id из реплая или @username, либо None."""
    msg = update.effective_message
    if not msg:
        return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    m = USERNAME_RE.search(msg.text or "")
    if m:
        try:
            chat = await context.bot.get_chat(f"@{m.group(1)}")
            return chat.id
        except TelegramError as e:
            logger.warning("get_chat @%s: %s", m.group(1), e)
    return None


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Профиль. Формат: Профиль | Профиль @user | реплай + Профиль."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    target_uid = await _resolve_target(update, context)
    if target_uid is None:
        u = update.effective_user
        if not u:
            return
        target_uid = u.id

    # гарантируем наличие строки в users
    user_row = await db.get_user(target_uid)
    if not user_row:
        try:
            c = await context.bot.get_chat(target_uid)
            await db.upsert_user(target_uid, c.username, c.first_name or "")
            user_row = await db.get_user(target_uid)
        except TelegramError:
            pass
    if not user_row:
        await msg.reply_text("Не нашёл этого пользователя.")
        return

    name = user_row.get("first_name") or str(target_uid)

    # всего сообщений
    rank = await db.user_rank(chat.id, target_uid)
    total_msgs = rank[0] if rank else 0

    # брак
    m = await db.find_active_marriage(chat.id, target_uid)
    if m:
        partner_id = m["user1_id"] if m["user2_id"] == target_uid else m["user2_id"]
        partner_name = str(partner_id)
        try:
            pc = await context.bot.get_chat(partner_id)
            partner_name = pc.first_name or pc.username or str(partner_id)
        except TelegramError:
            pass
        forced = " 🔒" if m["forced"] else ""
        marriage_line = f"в браке с {escape(partner_name)}{forced}"
    else:
        divorces = await db.count_divorces(chat.id, target_uid)
        if divorces > 0:
            marriage_line = f"разведён(а) ({divorces} раз)"
        else:
            marriage_line = "нет"

    # роль
    role = await db.get_role(target_uid)
    if target_uid == ADMIN_ID:
        role_str = "Создатель"
    elif role == "sheriff":
        role_str = "Шериф"
    elif role == "commissar":
        role_str = "Комиссар"
    else:
        role_str = "Участник"

    # монеты / кредит
    balance = user_row.get("balance")
    if balance is None:
        balance = 10
    credit = user_row.get("credit") or 0

    # ССС
    sss = "да" if user_row.get("sss_member") else "нет"

    # игры
    wins = user_row.get("tictactoe_wins") or 0
    losses = user_row.get("tictactoe_losses") or 0
    draws = user_row.get("tictactoe_draws") or 0
    played = wins + losses + draws

    # счётчики
    mutes = user_row.get("mutes_issued") or 0
    reports = user_row.get("reports_made") or 0
    courts = user_row.get("courts_held") or 0

    text = (
        f"👤 <b>Профиль</b>\n"
        f"Имя: <b>{escape(name)}</b>\n"
        f"Всего смс: <b>{total_msgs}</b>\n"
        f"Брак: {marriage_line}\n"
        f"Монеты: <b>{balance}</b>\n"
        f"Кредит: <b>{credit}</b>\n"
        f"ССС: <b>{sss}</b>\n"
        f"Роль: <b>{role_str}</b>\n\n"
        f"🎮 Крестики-нолики:\n"
        f"  Побед: <b>{wins}</b>\n"
        f"  Поражений: <b>{losses}</b>\n"
        f"  Ничьих: <b>{draws}</b>\n"
        f"  Сыграно: <b>{played}</b>\n\n"
        f"📋 Счётчики:\n"
        f"  Выдано мутов: <b>{mutes}</b>\n"
        f"  Жалоб: <b>{reports}</b>\n"
        f"  Судов: <b>{courts}</b>"
    )
    await msg.reply_text(text, parse_mode=ParseMode.HTML)
