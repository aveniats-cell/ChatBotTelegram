"""+Предупреждение / -Предупреждение (снятие).

Алиасы: +пред, +варн, -пред, -варн.
3 активных за 5 дней → автобан.
"""
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

_CMD_RE = re.compile(r"(?i)^\s*\+(Предупреждение|Пред|Варн)\s*")

WARN_TTL_DAYS = 5
WARN_LIMIT = 3


async def _is_staff(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    try:
        role = await db.get_role(user_id)
    except Exception as e:
        logger.warning("get_role(%s): %s", user_id, e)
        return False
    return role in ("sheriff", "commissar")


async def _is_bot(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    try:
        chat = await context.bot.get_chat(uid)
        return bool(getattr(chat, "is_bot", False))
    except TelegramError:
        return False


async def _check_and_ban_if_needed(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, name: str,
) -> bool:
    cnt = await db.count_active_warnings(chat_id, user_id)
    if cnt < WARN_LIMIT:
        return False
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        logger.error("auto-ban after warnings failed: %s", e)
        return False
    await db.add_punishment(user_id, chat_id, "ban", None, ADMIN_ID)
    await db.deactivate_warnings(chat_id, user_id)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔨 {escape(name)} забанен(а): <b>{cnt}</b> предупреждений.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass
    return True


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not await _is_staff(context, user.id):
        await msg.reply_text(
            "⛔ Предупреждения может выдавать только шериф, комиссар или админ."
        )
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text(
            "Формат: <code>+Предупреждение @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, name = target

    if uid == user.id:
        await msg.reply_text("Себе предупреждение выдать нельзя 🙂")
        return
    if ADMIN_ID and uid == ADMIN_ID:
        await msg.reply_text("Админу предупреждение выдать нельзя.")
        return
    if await _is_staff(context, uid):
        await msg.reply_text("Нельзя выдать предупреждение сотруднику.")
        return
    if await _is_bot(context, uid):
        await msg.reply_text("Ботам предупреждение выдать нельзя 🤖")
        return

    raw = (msg.text or "").strip()
    reason = None
    cleaned = _CMD_RE.sub("", raw)
    cleaned = USERNAME_RE.sub("", cleaned).strip()
    if cleaned:
        reason = cleaned[:200]

    expires_at = (datetime.now(timezone.utc) + timedelta(days=WARN_TTL_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")

    wid = await db.add_warning(chat.id, uid, user.id, reason, expires_at)
    if wid == 0:
        await msg.reply_text("⚠️ Не удалось сохранить предупреждение в БД.")
        return

    cnt = await db.count_active_warnings(chat.id, uid)
    reason_line = f"\nПричина: {escape(reason)}" if reason else ""

    await msg.reply_text(
        f"⚠️ <b>Предупреждение</b>\n"
        f"Кому: {escape(name)}\n"
        f"Выдал: {escape(user.first_name or str(user.id))}"
        f"{reason_line}\n"
        f"Активных: <b>{cnt}/{WARN_LIMIT}</b> (истекают за {WARN_TTL_DAYS} дней).",
        parse_mode=ParseMode.HTML,
    )

    if cnt >= WARN_LIMIT:
        await _check_and_ban_if_needed(context, chat.id, uid, name)
    else:
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"👀 {escape(name)}, у вас предупреждение ({cnt}/{WARN_LIMIT}).",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Пред / -Предупреждение / -Варн @user — снимает последнее активное."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not await _is_staff(context, user.id):
        await msg.reply_text(
            "⛔ Снимать предупреждения может только шериф, комиссар или админ."
        )
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text(
            "Формат: <code>-Пред @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, name = target

    cnt_before = await db.count_active_warnings(chat.id, uid)
    if cnt_before <= 0:
        await msg.reply_text(f"У {escape(name)} нет активных предупреждений.")
        return

    removed = await db.remove_last_active_warning(chat.id, uid)
    if removed == 0:
        await msg.reply_text("⚠️ Не удалось снять предупреждение (ошибка БД).")
        return

    cnt_after = await db.count_active_warnings(chat.id, uid)
    await msg.reply_text(
        f"✅ Снято предупреждение с {escape(name)}.\n"
        f"Активных: <b>{cnt_after}/{WARN_LIMIT}</b>",
        parse_mode=ParseMode.HTML,
    )
