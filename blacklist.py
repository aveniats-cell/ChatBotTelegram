"""Глобальный чёрный список пользователей бота.

Команды:
    /banbot @username — заблокировать пользователя;
    /unbanbot @username — снять блокировку.

Можно также ответить командой на сообщение пользователя.
Заблокированные пользователи не проходят ни к одному обычному
обработчику сообщений или callback-кнопок.
"""
import logging
import os
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

import db

logger = logging.getLogger(__name__)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")


def _is_admin(user_id: Optional[int]) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


async def guard_blacklisted_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Останавливает обработку любого update от пользователя из списка."""
    user = update.effective_user
    if not user:
        return

    # Администратор всегда сохраняет доступ, в том числе к /unbanbot.
    if _is_admin(user.id):
        return

    try:
        if await db.is_user_blacklisted(user.id):
            logger.info("Blocked update from blacklisted user_id=%s", user.id)
            raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        # При временной ошибке БД не блокируем всех пользователей.
        logger.exception("Не удалось проверить bot blacklist для user_id=%s", user.id)


async def _resolve_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    allow_cached_username: bool = True,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    msg = update.effective_message
    if not msg:
        return None, None, None, "Не удалось прочитать сообщение."

    # Самый надёжный вариант — reply: Telegram передаёт реальный ID.
    reply = msg.reply_to_message
    if reply and reply.from_user:
        target = reply.from_user
        return target.id, target.username, target.first_name, None

    args = context.args or []
    if not args:
        return None, None, None, "Укажи @username или ответь командой на сообщение пользователя."

    raw = args[0].strip()
    if raw.startswith("@"):
        username = raw.lstrip("@")
        if not username:
            return None, None, None, "Некорректный username."
        target_id = await db.get_user_by_username(username)
        if target_id is None and allow_cached_username:
            target_id = await db.get_blacklisted_user_by_username(username)
        if target_id is None:
            return (
                None, username, None,
                "Не нашёл этого пользователя в базе бота. Он должен хотя бы раз написать боту "
                "или воспользоваться им. Либо ответь командой на его сообщение.",
            )
        user_data = await db.get_user(int(target_id))
        first_name = user_data.get("first_name") if user_data else None
        return int(target_id), username, first_name, None

    try:
        target_id = int(raw)
    except ValueError:
        return None, None, None, "Укажи @username, числовой Telegram ID или ответь на сообщение."
    if target_id <= 0:
        return None, None, None, "Некорректный Telegram ID."
    user_data = await db.get_user(target_id)
    username = user_data.get("username") if user_data else None
    first_name = user_data.get("first_name") if user_data else None
    return target_id, username, first_name, None


async def cmd_banbot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    actor = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    if not actor or not _is_admin(actor.id):
        await msg.reply_text("⛔ Только главный администратор может блокировать пользователей бота.")
        return

    target_id, username, first_name, error = await _resolve_target(update, context)
    if error:
        await msg.reply_text(error)
        return
    if target_id is None:
        await msg.reply_text("Не удалось определить пользователя.")
        return
    if target_id == ADMIN_ID:
        await msg.reply_text("Нельзя добавить главного администратора в чёрный список.")
        return

    ok = await db.blacklist_user(
        target_id,
        username=username,
        first_name=first_name,
        banned_by=actor.id,
        reason="Блокировка главным администратором",
    )
    if not ok:
        await msg.reply_text("⚠️ Не удалось сохранить блокировку в базе данных.")
        return

    label = f"@{username}" if username else (first_name or str(target_id))
    await msg.reply_text(
        f"🚫 Пользователь {label} добавлен в глобальный чёрный список.\n"
        "Его доступ к боту закрыт. Данные, баланс, токены и покупки сохранены."
    )


async def cmd_unbanbot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    actor = update.effective_user
    msg = update.effective_message
    if not msg:
        return
    if not actor or not _is_admin(actor.id):
        await msg.reply_text("⛔ Только главный администратор может снимать блокировку.")
        return

    target_id, username, first_name, error = await _resolve_target(update, context)
    if error:
        await msg.reply_text(error)
        return
    if target_id is None:
        await msg.reply_text("Не удалось определить пользователя.")
        return

    if not await db.is_user_blacklisted(target_id):
        label = f"@{username}" if username else (first_name or str(target_id))
        await msg.reply_text(f"ℹ️ Пользователь {label} не находится в чёрном списке.")
        return

    if not await db.unblacklist_user(target_id):
        await msg.reply_text("⚠️ Не удалось снять блокировку в базе данных.")
        return

    label = f"@{username}" if username else (first_name or str(target_id))
    await msg.reply_text(
        f"✅ Блокировка пользователя {label} снята. Доступ к боту восстановлен."
    )
