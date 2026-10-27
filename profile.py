"""Профиль: базовый экран + разделы на inline-кнопках.

- «Профиль» / «Профиль @user» / реплай → базовый экран.
- Кнопки: Жалобы/Муты/Предупреждения, Игры, Аура, Счётчики, Назад.
- callback_data: prof:<section>:<target_uid>
  section: base | safety | games | aura | counters
"""
import os
import re
import logging
from html import escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import resolver
import sss

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


# ---------- resolve ----------

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


# ---------- keyboards ----------

def _kb(target_uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🚨 Жалобы/Муты/Предупреждения",
                                 callback_data=f"prof:safety:{target_uid}"),
        ],
        [
            InlineKeyboardButton("🎮 Игры",
                                 callback_data=f"prof:games:{target_uid}"),
            InlineKeyboardButton("✨ Аура",
                                 callback_data=f"prof:aura:{target_uid}"),
        ],
        [
            InlineKeyboardButton("📊 Счётчики",
                                 callback_data=f"prof:counters:{target_uid}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _kb_section(target_uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⬅️ Назад",
                                 callback_data=f"prof:base:{target_uid}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


# ---------- render base ----------

async def _render_base(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_uid: int,
) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        try:
            c = await context.bot.get_chat(target_uid)
            await db.upsert_user(target_uid, c.username, c.first_name or "")
            user_row = await db.get_user(target_uid)
        except TelegramError:
            pass
    if not user_row:
        return "Не нашёл этого пользователя."

    name = user_row.get("first_name") or str(target_uid)

    rank = await db.user_rank(chat_id, target_uid)
    total_msgs = rank[0] if rank else 0

    m = await db.find_active_marriage(chat_id, target_uid)
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
        divorces = await db.count_divorces(chat_id, target_uid)
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

    return (
        f"👤 <b>Профиль</b>\n"
        f"Имя: <b>{escape(name)}</b>\n"
        f"Всего смс: <b>{total_msgs}</b>\n"
        f"Брак: {marriage_line}\n"
        f"Монеты: <b>{balance}</b>\n"
        f"Кредит: <b>{credit}</b>\n"
        f"ССС: <b>{sss_label}</b>\n"
        f"Роль: <b>{role_str}</b>"
    )


# ---------- render sections ----------

async def _render_safety(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_uid: int,
) -> str:
    user_row = await db.get_user(target_uid)
    name = (user_row or {}).get("first_name") or str(target_uid)

    complaints_by = 0
    complaints_against = 0
    warnings_active = 0
    warnings_total = 0
    mutes = 0
    try:
        complaints_by = await db.count_complaints_by(chat_id, target_uid)
        complaints_against = await db.count_complaints_against(chat_id, target_uid)
        warnings_active = await db.count_active_warnings(chat_id, target_uid)
        warnings_total = await db.count_warnings_total(chat_id, target_uid)
        urow = await db.get_user(target_uid)
        mutes = (urow or {}).get("mutes_issued") or 0
    except Exception as e:
        logger.warning("profile safety: %s", e)

    return (
        f"🚨 <b>Жалобы / Муты / Предупреждения</b>\n"
        f"Имя: <b>{escape(name)}</b>\n\n"
        f"🚨 Жалобы:\n"
        f"  Подано им: <b>{complaints_by}</b>\n"
        f"  На него: <b>{complaints_against}</b>\n"
        f"⚠️ Предупреждения:\n"
        f"  Активных: <b>{warnings_active}</b>\n"
        f"  Всего: <b>{warnings_total}</b>\n"
        f"🔇 Выдано мутов: <b>{mutes}</b>"
    )


async def _render_games(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_uid: int,
) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        return "Не нашёл этого пользователя."
    name = user_row.get("first_name") or str(target_uid)
    wins = user_row.get("tictactoe_wins") or 0
    losses = user_row.get("tictactoe_losses") or 0
    draws = user_row.get("tictactoe_draws") or 0
    played = wins + losses + draws
    return (
        f"🎮 <b>Игры</b>\n"
        f"Имя: <b>{escape(name)}</b>\n\n"
        f"Крестики-нолики:\n"
        f"  Побед: <b>{wins}</b>\n"
        f"  Поражений: <b>{losses}</b>\n"
        f"  Ничьих: <b>{draws}</b>\n"
        f"  Сыграно: <b>{played}</b>"
    )


async def _render_aura(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_uid: int,
) -> str:
    user_row = await db.get_user(target_uid)
    name = (user_row or {}).get("first_name") or str(target_uid)
    value = 0
    rank_line = ""
    try:
        row = await db.aura_get(chat_id, target_uid)
        value = (row or {}).get("value") or 0
        if value > 0:
            r = await db.aura_rank(chat_id, target_uid)
            if r:
                _, arank, atotal = r
                rank_line = f"\nМесто в чате: <b>{arank}/{atotal}</b>"
    except Exception as e:
        logger.warning("profile aura: %s", e)
    return (
        f"✨ <b>Аура</b>\n"
        f"Имя: <b>{escape(name)}</b>\n"
        f"Значение: <b>{value}</b>{rank_line}"
    )


async def _render_counters(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_uid: int,
) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        return "Не нашёл этого пользователя."
    name = user_row.get("first_name") or str(target_uid)
    mutes = user_row.get("mutes_issued") or 0
    reports = user_row.get("reports_made") or 0
    courts = user_row.get("courts_held") or 0
    return (
        f"📊 <b>Счётчики</b>\n"
        f"Имя: <b>{escape(name)}</b>\n\n"
        f"  Выдано мутов: <b>{mutes}</b>\n"
        f"  Жалоб подано: <b>{reports}</b>\n"
        f"  Судов проведено: <b>{courts}</b>"
    )


_RENDERERS = {
    "safety": _render_safety,
    "games": _render_games,
    "aura": _render_aura,
    "counters": _render_counters,
}


# ---------- handlers ----------

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

    text = await _render_base(context, chat.id, target_uid)
    if text.startswith("Не нашёл"):
        await msg.reply_text(text)
        return

    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb(target_uid),
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return

    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "prof":
        await q.answer()
        return
    _, section, uid_s = parts
    try:
        target_uid = int(uid_s)
    except ValueError:
        await q.answer("Некорректный id.", show_alert=True)
        return

    chat = q.message.chat if q.message else None
    if not chat:
        await q.answer()
        return

    try:
        if section == "base":
            text = await _render_base(context, chat.id, target_uid)
            kb = _kb(target_uid)
        else:
            renderer = _RENDERERS.get(section)
            if not renderer:
                await q.answer("Раздел не найден.", show_alert=True)
                return
            text = await renderer(context, chat.id, target_uid)
            kb = _kb_section(target_uid)

        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        await q.answer()
    except TelegramError as e:
        # «message is not modified» — нормально, если нажали ту же кнопку.
        if "not modified" in str(e).lower():
            await q.answer()
            return
        logger.warning("profile_callback: %s", e)
        try:
            await q.answer("Не удалось обновить профиль.", show_alert=True)
        except TelegramError:
            pass
