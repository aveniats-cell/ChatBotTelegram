"""Роли (Шериф/Комиссар), муты, баны, «Поймать».

P2: _require_role проверяет `not ADMIN_ID` — при ADMIN_ID=0 никто не админ.
P2: unmute_user даёт явный набор прав, а не all_permissions().
P2: _resolve_target вынесен в utils.resolve_target.
"""
import os
import re
import time
import logging
from html import escape
from typing import Optional

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

TIME_RE = re.compile(r"(\d+)\s*([чhmsс])", re.IGNORECASE)
TIME_UNITS = {"ч": 3600, "h": 3600, "m": 60, "м": 60, "s": 1, "с": 1}

# Права «обычного участника» — для unmute (не даём админские).
_UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)


def parse_duration(text: str) -> Optional[int]:
    total = 0
    found = False
    for amount, unit in TIME_RE.findall(text):
        total += int(amount) * TIME_UNITS[unit.lower()]
        found = True
    return total if found else None


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0с"
    parts = []
    for label, size in (("ч", 3600), ("м", 60), ("с", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts)


def _strip_duration(text: str) -> str:
    return TIME_RE.sub("", text or "").strip()


async def _require_role(update: Update, roles: tuple[str, ...]) -> bool:
    user = update.effective_user
    if not user:
        return False
    # P2: при ADMIN_ID=0 никто не админ.
    if ADMIN_ID and user.id == ADMIN_ID:
        return True
    try:
        role = await db.get_role(user.id)
    except Exception as e:
        logger.warning("get_role(%s): %s", user.id, e)
        return False
    return role in roles


async def _deny(update: Update, text: str = "⛔ Недостаточно прав.") -> None:
    await update.effective_message.reply_text(text)


# ---------- +Шериф / -Шериф / +Комиссар / -Комиссар ----------

async def grant_sheriff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только админ или комиссар.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.set_role(uid, "sheriff", update.effective_user.id)
    await update.message.reply_text(f"✅ {escape(name)} назначен шерифом.")


async def revoke_sheriff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только админ или комиссар.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.remove_role(uid)
    await update.message.reply_text(f"✅ {escape(name)} больше не шериф.")


async def grant_commissar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or update.effective_user.id != ADMIN_ID:
        await _deny(update, "⛔ Только админ.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.set_role(uid, "commissar", update.effective_user.id)
    await update.message.reply_text(f"✅ {escape(name)} назначен комиссаром.")


async def revoke_commissar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or update.effective_user.id != ADMIN_ID:
        await _deny(update, "⛔ Только админ.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.remove_role(uid)
    await update.message.reply_text(f"✅ {escape(name)} больше не комиссар.")


async def list_sheriffs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ids = await db.list_roles("sheriff")
    if not ids:
        await update.message.reply_text("Шерифов пока нет.")
        return
    lines = []
    for uid in ids:
        try:
            chat = await context.bot.get_chat(uid)
            lines.append(f"• {escape(chat.first_name or str(uid))}")
        except TelegramError:
            lines.append(f"• {uid}")
    await update.message.reply_text("👮 Шерифы:\n" + "\n".join(lines))


async def list_commissars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ids = await db.list_roles("commissar")
    if not ids:
        await update.message.reply_text("Комиссаров пока нет.")
        return
    lines = []
    for uid in ids:
        try:
            chat = await context.bot.get_chat(uid)
            lines.append(f"• {escape(chat.first_name or str(uid))}")
        except TelegramError:
            lines.append(f"• {uid}")
    await update.message.reply_text("🎖 Комиссары:\n" + "\n".join(lines))


# ---------- Поймать ----------

async def sheriff_catch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("sheriff",)):
        await _deny(update, "⛔ Только шериф.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    sheriff = update.effective_user
    chat = update.effective_chat

    text = (
        f"🚨 <b>Подозрение</b>\n"
        f"Шериф: {escape(sheriff.first_name or str(sheriff.id))}\n"
        f"На пользователя: {escape(name)} (id <code>{uid}</code>)\n\n"
        f"Принять — забанить в чате. Отклонить — оставить."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять (бан)", callback_data=f"catch:accept:{uid}:{chat.id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"catch:reject:{uid}:{chat.id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except TelegramError as e:
        logger.warning("Не смог отправить запрос админу: %s", e)
        await update.message.reply_text("⚠️ Не смог связаться с админом.")
        return
    await update.message.reply_text("📨 Запрос отправлен админу.")


async def catch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_user:
        return
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await q.answer("Только админ.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 4 or parts[0] != "catch":
        return
    _, action, uid_s, chat_s = parts
    uid, chat_id = int(uid_s), int(chat_s)

    if action == "reject":
        await q.edit_message_text(
            (q.message.text or "") + "\n\n❌ <b>Отклонено</b> админом.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=uid)
        await db.add_punishment(uid, chat_id, "ban", None, ADMIN_ID)
        await q.edit_message_text(
            (q.message.text or "") + "\n\n✅ <b>Принято</b>: пользователь забанен.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await q.edit_message_text(
            (q.message.text or "") + f"\n\n⚠️ Не удалось забанить: {escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


# ---------- Мут / Бан ----------

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return
    raw = (update.message.text or "")
    rest = raw.split(maxsplit=1)
    rest = rest[1] if len(rest) > 1 else ""
    duration = parse_duration(rest)
    if not duration:
        await update.message.reply_text("Не понял время. Пример: +Мут @user 1ч 30м")
        return

    clean = _strip_duration(rest)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    until = int(time.time()) + duration
    perms = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=uid,
            permissions=perms, until_date=until,
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Не удалось замутить: {e}")
        return
    await db.add_punishment(uid, update.effective_chat.id, "mute", until, update.effective_user.id)
    try:
        await db.inc_user_counter(update.effective_user.id, "mutes_issued", 1)
    except Exception:
        pass
    await update.message.reply_text(
        f"🔇 {escape(name)} замучен на {format_duration(duration)}."
    )


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=uid, permissions=_UNMUTE_PERMS,
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Не удалось размутить: {e}")
        return
    await db.deactivate_punishment(uid, update.effective_chat.id, "mute")
    await update.message.reply_text(f"🔊 {escape(name)} размучен.")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=uid)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Не удалось забанить: {e}")
        return
    await db.add_punishment(uid, update.effective_chat.id, "ban", None, update.effective_user.id)
    await update.message.reply_text(f"🚫 {escape(name)} забанен.")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return
    target = await utils.resolve_target(update, context, update.message.text or "")
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    try:
        await context.bot.unban_chat_member(
            chat_id=update.effective_chat.id, user_id=uid, only_if_banned=True,
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Не удалось разбанить: {e}")
        return
    await db.deactivate_punishment(uid, update.effective_chat.id, "ban")
    await update.message.reply_text(f"✅ {escape(name)} разбанен.")
