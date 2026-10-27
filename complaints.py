"""+Жалоба: любой участник жалуется на нарушителя — жалоба уходит в ЛС админу.

Админ получает кнопки: Забанить / Выдать предупреждение / Отклонить.
"""
import os
import re
import logging
from datetime import datetime, timedelta, timezone
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
import utils
import warnings as warnings_mod  # локальный модуль warnings.py

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


async def cmd_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Жалоба @user (или реплай) — доступно всем. Уходит в ЛС админу."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not ADMIN_ID:
        await msg.reply_text("⚠️ Жалобы не настроены (нет ADMIN_ID).")
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text(
            "Формат: <code>+Жалоба @user</code> или реплай на сообщение.",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, name = target

    if uid == user.id:
        await msg.reply_text("На себя жаловаться нельзя 🙂")
        return
    if ADMIN_ID and uid == ADMIN_ID:
        await msg.reply_text("На админа жаловаться нельзя.")
        return

    # message_id: сообщение нарушителя (реплай) либо сама команда.
    if msg.reply_to_message:
        message_id = msg.reply_to_message.message_id
    else:
        message_id = msg.message_id

    cid = await db.add_complaint(chat.id, uid, user.id, message_id)
    if cid == 0:
        await msg.reply_text("⚠️ Не удалось сохранить жалобу в БД.")
        return

    reporter_name = user.first_name or user.username or str(user.id)
    chat_title = chat.title or chat.username or str(chat.id)

    text = (
        f"🚨 <b>Жалоба</b> #{cid}\n"
        f"Чат: {escape(str(chat_title))} (<code>{chat.id}</code>)\n"
        f"Нарушитель: {escape(name)} (id <code>{uid}</code>)\n"
        f"От: {escape(reporter_name)} (id <code>{user.id}</code>)\n"
        f"Сообщение: <code>{message_id}</code>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔨 Забанить", callback_data=f"compl:ban:{cid}"),
        InlineKeyboardButton("⚠️ Предупреждение", callback_data=f"compl:warn:{cid}"),
        InlineKeyboardButton("✅ Отклонить", callback_data=f"compl:rej:{cid}"),
    ]])

    delivered = False
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        delivered = True
    except TelegramError as e:
        logger.warning("complaint -> admin send: %s", e)

    if delivered:
        await msg.reply_text(
            "✅ Жалоба отправлена админу. Спасибо!",
            reply_to_message_id=msg.message_id,
            allow_sending_without_reply=True,
        )
    else:
        await msg.reply_text(
            "⚠️ Жалоба сохранена, но не удалось доставить админу. "
            "Попробуйте позже или напишите админу напрямую."
        )


async def complaint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await q.answer("Только админ.", show_alert=True)
        return

    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "compl":
        await q.answer()
        return
    _, action, cid_s = parts
    try:
        cid = int(cid_s)
    except ValueError:
        await q.answer("Некорректный id.", show_alert=True)
        return

    c = await db.get_complaint(cid)
    if not c:
        await q.answer("Жалоба не найдена.", show_alert=True)
        return
    if c["status"] != "pending":
        await q.answer(f"Уже обработана: {c['status']}.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    target_id = c["target_id"]
    chat_id = c["chat_id"]

    name = str(target_id)
    try:
        tchat = await context.bot.get_chat(target_id)
        name = tchat.first_name or tchat.username or str(target_id)
    except TelegramError:
        pass

    if action == "rej":
        aff = await db.resolve_complaint(cid, "rejected", user.id)
        if aff == 0:
            await q.answer("Уже обработана.", show_alert=True)
            return
        await q.answer("Отклонено")
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n✅ <b>Админ: отклонено.</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚖️ Жалоба на {escape(name)} рассмотрена. Нарушений не найдено.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    if action == "ban":
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
            await db.add_punishment(target_id, chat_id, "ban", None, user.id)
            await db.deactivate_warnings(chat_id, target_id)
        except TelegramError as e:
            await q.answer(f"Ошибка бана: {e}", show_alert=True)
            return
        await db.resolve_complaint(cid, "banned", user.id)
        await q.answer("Забанен")
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n🔨 <b>Админ: забанить.</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔨 {escape(name)} забанен(а) по жалобе.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    if action == "warn":
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=warnings_mod.WARN_TTL_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        wid = await db.add_warning(chat_id, target_id, user.id,
                                   "по жалобе", expires_at)
        if wid == 0:
            await q.answer("Не удалось сохранить предупреждение.", show_alert=True)
            return
        await db.resolve_complaint(cid, "warned", user.id)
        await q.answer("Предупреждение выдано")

        cnt = await db.count_active_warnings(chat_id, target_id)
        try:
            await q.edit_message_text(
                (q.message.text or "")
                + f"\n\n⚠️ <b>Админ: предупреждение ({cnt}/"
                  f"{warnings_mod.WARN_LIMIT}).</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⚠️ {escape(name)} получил(а) предупреждение "
                      f"({cnt}/{warnings_mod.WARN_LIMIT})."),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        # Автобан, если лимит достигнут.
        if cnt >= warnings_mod.WARN_LIMIT:
            await warnings_mod._check_and_ban_if_needed(
                context, chat_id, target_id, name,
            )
        return

    await q.answer()
