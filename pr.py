"""Логика PR-бота: проверка подписки, удаление сообщений, приглашение."""
import os
import logging
from html import escape

from telegram import (
    Update,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

# ID канала, на который нужно подписаться
CANAL_ID = int(os.getenv("CANAL_ID", "0"))
# Ссылка на канал для кнопки (публичная или приватная)
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/")
# Название канала в тексте
CANAL_TITLE = os.getenv("CANAL_TITLE", "наш канал")


async def is_subscribed(bot, user_id: int) -> bool:
    """Проверка подписки пользователя на канал."""
    try:
        member = await bot.get_chat_member(chat_id=CANAL_ID, user_id=user_id)
        return member.status not in (
            ChatMember.LEFT,
            ChatMember.BANNED,
        )
    except TelegramError as e:
        logger.warning("Не удалось проверить подписку %s: %s", user_id, e)
        # Если не смогли проверить — не блокируем пользователя
        return True


def _subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Перейти в канал", url=CANAL_URL)]]
    )


def _subscription_text(user) -> str:
    """HTML-текст: имя со скрытой ссылкой на профиль."""
    name = escape(user.first_name or "друг")
    if user.username:
        link = f'<a href="https://t.me/{user.username}">{name}</a>'
        uname = f"(@{escape(user.username)})"
    else:
        link = f'<a href="tg://user?id={user.id}">{name}</a>'
        uname = ""
    return (
        f"👋 {link} {uname}, чтобы писать в чат, подпишитесь на "
        f"<b>{escape(CANAL_TITLE)}</b> и нажмите кнопку ниже."
    )


async def check_and_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обработчик всех сообщений в группе: если не подписан — удалить и напомнить."""
    msg: Message | None = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return

    ok = await is_subscribed(context.bot, user.id)
    await db.set_subscription(user.id, ok)

    if ok:
        return

    # Пытаемся удалить сообщение
    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("Не удалось удалить сообщение (нет прав?): %s", e)

    # Отправляем напоминание с кнопкой
    try:
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=_subscription_text(user),
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning("Не удалось отправить напоминание: %s", e)


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие новых участников группы (по желанию)."""
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await db.upsert_user(member.id, member.username, member.first_name)
        ok = await is_subscribed(context.bot, member.id)
        await db.set_subscription(member.id, ok)
        if ok:
            continue
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=_subscription_text(member),
                parse_mode=ParseMode.HTML,
                reply_markup=_subscription_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            logger.warning("Приветствие не отправлено: %s", e)


async def recheck_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Команда /check — пользователь может проверить статус и получить фидбек."""
    user = update.effective_user
    if not user:
        return
    ok = await is_subscribed(context.bot, user.id)
    await db.set_subscription(user.id, ok)
    if ok:
        await update.message.reply_text("✅ Подписка активна, можете писать в чат.")
    else:
        await update.message.reply_text(
            "❌ Вы пока не подписаны на канал.",
            reply_markup=_subscription_keyboard(),
        )
