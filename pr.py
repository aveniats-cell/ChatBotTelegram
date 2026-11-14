
"""Логика PR-бота: проверка подписки, удаление сообщений, приглашение."""

import os
import logging
from html import escape

from telegram import (
    Update,
    ChatMember,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ui_design import InlineKeyboardButton

import db

logger = logging.getLogger(__name__)

CANAL_ID = int(os.getenv("CANAL_ID", "0") or "0")
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/")
CANAL_TITLE = os.getenv("CANAL_TITLE", "наш канал")

_ADMIN_WORDS = {
    "топ", "шип", "сделать", "профиль", "мой", "мое",
    "список", "поймать", "хо",
    "stats", "start", "ping", "check",
    "help", "restart", "казино",
    "ии", "браин", "браини", "мозг", "brainy", "brain",
    "перевод", "казна", "кредит", "монеты",
    "дроч", "аура", "дать", "поцеловать", "обнять",
    "побить", "обоссать", "погладить",
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


def _is_normal_user_message(update: Update) -> bool:
    """Разрешаем проверку только обычных сообщений пользователей."""

    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or not chat:
        return False

    # Игнорируем ботов и служебные аккаунты Telegram.
    if user.is_bot:
        return False

    # Не проверяем каналы и личные сообщения.
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False

    # Сообщения от имени каналов не имеют обычного отправителя.
    if msg.sender_chat is not None:
        return False

    # Служебные сообщения о входе, выходе и т. п.
    if msg.new_chat_members or msg.left_chat_member:
        return False

    # Не проверяем сообщения без обычного отправителя.
    if not msg.from_user:
        return False

    return True


async def is_subscribed(bot, user_id: int) -> bool | None:
    """Проверяет подписку.

    True  — подписан;
    False — точно не подписан;
    None  — Telegram не дал проверить статус.

    Ошибка доступа к списку участников не должна превращаться ни в
    подтверждённую подписку, ни в отказ пользователю: в таком случае
    пропускаем проверку и не меняем сохранённый статус.
    """
    if not CANAL_ID:
        logger.warning("CANAL_ID не задан: проверка подписки отключена")
        return True

    try:
        member = await bot.get_chat_member(
            chat_id=CANAL_ID,
            user_id=user_id,
        )
        return member.status not in (
            ChatMember.LEFT,
            ChatMember.BANNED,
        )

    except TelegramError as e:
        logger.warning(
            "Не удалось проверить подписку user=%s canal=%s: %s. "
            "Статус не изменён; проверьте CANAL_ID и права бота.",
            user_id,
            CANAL_ID,
            e,
        )
        return None


def _subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Подписаться на канал",
                url=CANAL_URL
            )
        ]
    ])


def _subscription_text(user) -> str:
    name = escape(user.first_name or "друг")

    if user.username:
        link = f'<a href="https://t.me/{user.username}">{name}</a>'
    else:
        link = f'<a href="tg://user?id={user.id}">{name}</a>'

    return (
        f"👋 {link}, чтобы писать в чат, сначала подпишитесь на "
        f"<b>{escape(CANAL_TITLE)}</b>.\n\n"
        "После подписки возвращайтесь — и пишите. ✅"
    )


async def check_and_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:

    # Проверяем, что сообщение действительно от обычного пользователя.
    if not _is_normal_user_message(update):
        return

    msg = update.effective_message
    user = update.effective_user

    try:
        bot_id = context.bot.id
    except Exception:
        bot_id = 0

    if bot_id and _is_reply_to_bot(msg, bot_id):
        return

    if _looks_like_admin_command(msg.text or ""):
        return

    try:
        await db.upsert_user(
            user.id,
            user.username,
            user.first_name
        )
    except Exception as e:
        logger.warning("upsert_user: %s", e)

    ok = await is_subscribed(context.bot, user.id)

    # None означает, что Telegram не предоставил статус (например,
    # бот не имеет доступа к участникам канала). Не перезаписываем БД
    # и не удаляем сообщение пользователя из-за временной ошибки API.
    if ok is None:
        return

    try:
        await db.set_subscription(user.id, ok)
    except Exception as e:
        logger.warning("set_subscription: %s", e)

    if ok:
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info(
            "Не удалось удалить сообщение: %s",
            e
        )

    try:
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=_subscription_text(user),
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning(
            "Не удалось отправить напоминание: %s",
            e
        )


async def on_new_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:

    msg = update.effective_message

    if not msg or not msg.new_chat_members:
        return

    for member in msg.new_chat_members:

        if member.is_bot:
            continue

        try:
            await db.upsert_user(
                member.id,
                member.username,
                member.first_name
            )

            ok = await is_subscribed(context.bot, member.id)
            if ok is not None:
                await db.set_subscription(member.id, ok)

        except Exception as e:
            logger.warning("Ошибка обработки нового участника: %s", e)
            continue

        if ok is None or ok:
            continue

        try:
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=_subscription_text(member),
                parse_mode=ParseMode.HTML,
                reply_markup=_subscription_keyboard(),
                disable_web_page_preview=True,
            )

        except TelegramError as e:
            logger.warning(
                "Приветствие не отправлено: %s",
                e
            )


async def recheck_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:

    user = update.effective_user
    msg = update.effective_message

    if not user or user.is_bot or not msg:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    ok = await is_subscribed(context.bot, user.id)

    if ok is None:
        await msg.reply_text(
            "⚠️ Сейчас не удалось проверить подписку через Telegram. "
            "Попробуйте позже. Администратору нужно проверить, что бот "
            "добавлен в канал и имеет права администратора."
        )
        return

    await db.set_subscription(user.id, ok)

    if ok:
        await msg.reply_text(
            "✅ Подписка активна — можете писать в чат."
        )
    else:
        await msg.reply_text(
            f"❌ Вы пока не подписаны на «{escape(CANAL_TITLE)}».\n"
            "Подпишитесь и возвращайтесь — после этого сообщения "
            "будут проходить.",
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
        )
