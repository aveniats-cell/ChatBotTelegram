"""Логика PR-бота: проверка подписки, удаление сообщений, приглашение.

P1: _looks_like_admin_command — белый список «команд, которые должны
работать без подписки» (первое слово совпадает полностью, а не по
префиксу).

P2: сообщения-реплаи на сообщения бота (для чата с ИИ) не удаляются
и не требуют подписки — иначе бот режет свой же диалог.
"""
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

CANAL_ID = int(os.getenv("CANAL_ID", "0") or "0")
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/")
CANAL_TITLE = os.getenv("CANAL_TITLE", "наш канал")

_ADMIN_WORDS = {
    "топ", "шип", "сделать", "профиль", "мой", "мое",
    "список", "поймать", "хо",
    "статы", "stats", "start", "ping", "check",
    "казино", "казик", "ии", "браин", "браини", "мозг", "brainy", "brain",
    "help", "restart",
}


def _looks_like_admin_command(text: str) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped[0] in "+-–−":
        return True
    first = stripped.split(maxsplit=1)[0].casefold().strip(".,!?:;")
    return first in _ADMIN_WORDS


def _is_reply_to_bot(msg: Message, bot_id: int) -> bool:
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return False
    return msg.reply_to_message.from_user.id == bot_id


async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CANAL_ID, user_id=user_id)
        return member.status not in (ChatMember.LEFT, ChatMember.BANNED)
    except TelegramError as e:
        logger.warning("Не удалось проверить подписку %s: %s", user_id, e)
        return True


def _subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Подписаться на канал", url=CANAL_URL)]]
    )


def _subscription_text(user) -> str:
    name = escape(user.first_name or "друг")
    if user.username:
        link = f'<a href="https://t.me/{user.username}">{name}</a>'
    else:
        link = f'<a href="tg://user?id={user.id}">{name}</a>'
    return (
        f"👋 {link}, чтобы писать в чат, сначала подпишитесь на "
        f"<b>{escape(CANAL_TITLE)}</b>.\n\n"
        f"После подписки возвращайтесь — и пишите. ✅"
    )


async def check_and_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return

    # Реплаи на сообщения бота — не трогаем (диалог с ИИ).
    try:
        bot_id = context.bot.id
    except Exception:
        bot_id = 0
    if bot_id and _is_reply_to_bot(msg, bot_id):
        return

    if _looks_like_admin_command(msg.text or ""):
        return

    try:
        await db.upsert_user(user.id, user.username, user.first_name)
    except Exception as e:
        logger.warning("upsert_user: %s", e)

    ok = await is_subscribed(context.bot, user.id)
    await db.set_subscription(user.id, ok)

    if ok:
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("Не удалось удалить сообщение (нет прав?): %s", e)

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
    if not update.message or not update.message.new_chat_members:
        return
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


async def recheck_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    ok = await is_subscribed(context.bot, user.id)
    await db.set_subscription(user.id, ok)
    if ok:
        await update.message.reply_text("✅ Подписка активна — можете писать в чат.")
    else:
        await update.message.reply_text(
            f"❌ Вы пока не подписаны на «{CANAL_TITLE}».\n"
            f"Подпишитесь и возвращайтесь — после этого сообщения будут проходить.",
            reply_markup=_subscription_keyboard(),
        )
