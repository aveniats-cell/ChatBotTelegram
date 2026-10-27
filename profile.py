"""Профиль: имя, смс, брак, монеты, кредит, ССС, роль, статистика, аура."""
import os
import re
import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import resolver
import sss

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    m = USERNAME_RE.search(msg.text or "")
    if not m:
        return None
    uname = m.group(1)
    try:
        uid = await db.get_user_by_username(uname)
        if uid:
            return uid
    except Exception as e:
        logger.warning("get_user_by_username(%s): %s", uname, e)
    r = await resolver.resolve_username(uname)
    if r:
        return r[0]
    try:
        chat = await context.bot.get_chat(f"@{uname}")
        return chat.id
    except TelegramError as e:
        logger.warning("get_chat @%s: %s", uname, e)
    return None


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _cmd_profile_inner(update, context)
    except Exception as e:
        logger.exception("cmd_profile error: %s", e)
        try:
            await update.effective_message.reply_text(
                "⚠️ Не удалось получить профиль. Попробуйте позже."
            )
        except Exception:
            pass


async def _cmd_profile_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    rank = await db.user_rank(chat.id, target_uid)
    total_msgs = rank[0] if rank else 0

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

    role = await db.get_role(target_uid)
    sss_member = bool(user_row.get("sss_member")) or (target_uid in sss._sss_all())

    if target_uid == ADMIN_ID:
        role_str = "Создатель"
    elif sss_member and role == "sheriff":
        role_str = "Шериф · ССС"
    elif sss_member and role == "commissar":
        role_str = "Комиссар · ССС"
    elif sss_member:
        role_str = "ССС"
    elif role == "sheriff":
        role_str = "Шериф"
    elif role == "commissar":
        role_str = "Комиссар"
    else:
        role_str = "Участник"

    balance = user_row.get("balance")
    if balance is None:
        balance = 10
    credit = user_row.get("credit") or 0
    sss_label = "да" if sss_member else "нет"

    # Аура
    aura_value = 0
    aura_line = "0"
    try:
        aura_row = await db.aura_get(chat.id, target_uid)
        if aura_row:
            aura_value = aura_row.get("value") or 0
        if aura_value > 0:
            r = await db.aura_rank(chat.id, target_uid)
            if r:
                _, arank, atotal = r
                aura_line = f"{aura_value} (место {arank}/{atotal})"
            else:
                aura_line = str(aura_value)
        else:
            aura_line = "0"
    except Exception as e:
        logger.warning("profile aura: %s", e)
        aura_line = str(aura_value)

    wins = user_row.get("tictactoe_wins") or 0
    losses = user_row.get("tictactoe_losses") or 0
    draws = user_row.get("tictactoe_draws") or 0
    played = wins + losses + draws

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
        f"Аура: <b>{aura_line}</b>\n"
        f"ССС: <b>{sss_label}</b>\n"
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
