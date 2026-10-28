"""Роли (Шериф/Комиссар), муты, баны, «Поймать»."""
import os
import re
import time
import logging
from html import escape

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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


async def _require_role(update: Update, roles: tuple[str, ...]) -> bool:
    user = update.effective_user
    if not user:
        return False
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
    parts = (q.data or "").split(":")
    if len(parts) != 4 or parts[0] != "catch":
        return
    _, action, uid_s, chat_s = parts
    try:
        uid, chat_id = int(uid_s), int(chat_s)
    except ValueError:
        await q.answer("Некорректные данные.", show_alert=True)
        return

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
    parts = raw.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""

    # Отделяем @username, но время может быть и до, и после него.
    uname_m = USERNAME_RE.search(rest)
    if uname_m:
        before = rest[:uname_m.start()].strip()
        after = rest[uname_m.end():].strip()
        # Сначала пробуем время из after, потом из before.
        seconds, time_rest = utils.parse_duration(after)
        reason = (time_rest or "").strip() or None
        if not seconds:
            seconds, time_rest2 = utils.parse_duration(before)
            reason = (time_rest2 or after or "").strip() or None
    else:
        seconds, time_rest = utils.parse_duration(rest)
        reason = (time_rest or "").strip() or None

    if not seconds or seconds <= 0:
        await update.message.reply_text(
            "Не понял время. Пример: <code>+Мут 1ч 30м</code>, "
            "<code>+Мут @user 5ч Богохульство</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    target = await utils.resolve_target(update, context, rest)
    if not target:
        await update.message.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    until = int(time.time()) + seconds
    perms = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=uid,
            permissions=perms, until_date=until,
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Не удалось замутить: {e}")
        return
    await db.add_punishment(uid, update.effective_chat.id, "mute", until,
                            update.effective_user.id)
    try:
        await db.inc_user_counter(update.effective_user.id, "mutes_issued", 1)
    except Exception:
        pass

    reason_line = f"\nПричина: {escape(reason)}" if reason else ""
    await update.message.reply_text(
        f"🔇 {escape(name)} замучен на {utils.fmt_duration(seconds)}."
        f"{reason_line}",
        parse_mode=ParseMode.HTML,
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
