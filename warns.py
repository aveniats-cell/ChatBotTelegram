"""+Предупреждение: шериф/комиссар/админ выписывает предупреждение.

3 активных предупреждения за 5 дней → автоматический бан.
Предупреждение истекает через WARN_TTL_DAYS дней (по умолчанию 5).
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

WARN_TTL_DAYS = 5
WARN_LIMIT = 3


async def _is_staff(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Шериф / комиссар / админ. Участник — нет."""
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    try:
        role = await db.get_role(user_id)
    except Exception as e:
        logger.warning("get_role(%s): %s", user_id, e)
        return False
    return role in ("sheriff", "commissar")


async def _check_and_ban_if_needed(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
    name: str,
) -> bool:
    """Если у юзера >= WARN_LIMIT активных предупреждений — банит.

    Возвращает True, если бан произошёл.
    """
    cnt = await db.count_active_warnings(chat_id, user_id)
    if cnt < WARN_LIMIT:
        return False
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        logger.warning("auto-ban after warnings: %s", e)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⚠️ У {escape(name)} <b>{cnt}</b> предупреждений, "
                      f"но Telegram не дал забанить: {escape(str(e))}"),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return False
    await db.add_punishment(user_id, chat_id, "ban", None, ADMIN_ID)
    await db.deactivate_warnings(chat_id, user_id)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🔨 {escape(name)} забанен(а): "
                  f"<b>{cnt}</b> предупреждений за {WARN_TTL_DAYS} дней."),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass
    return True


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Предупреждение @user [причина] — доступно шерифу/комиссару/админу.

    Участнику (без роли) — отказ. Причина опциональна (текст после цели).
    """
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

    # Нельзя выдавать предупреждение другому сотруднику (шериф/комиссар/админ).
    if await _is_staff(context, uid):
        await msg.reply_text("Нельзя выдать предупреждение сотруднику.")
        return

    # Причина: всё, что осталось после удаления команды и @username.
    raw = (msg.text or "").strip()
    reason = None
    cleaned = re.sub(r"(?i)^\s*\+Предупреждение\s*", "", raw)
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
        f"Активных предупреждений: <b>{cnt}/{WARN_LIMIT}</b> "
        f"(истекают за {WARN_TTL_DAYS} дней).",
        parse_mode=ParseMode.HTML,
    )

    # Автобан при достижении лимита.
    if cnt >= WARN_LIMIT:
        await _check_and_ban_if_needed(context, chat.id, uid, name)
    else:
        # мягкое напоминание нарушителю в чат
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(f"👀 {escape(name)}, у вас предупреждение "
                      f"({cnt}/{WARN_LIMIT}). Ещё немного — и бан."),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
