"""2.4 Шип: Шип @u1 @u2 -> шуточная фраза про их любовь."""
import random
import logging
import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

TEMPLATES = [
    "💘 {a} и {b} — уже не просто друзья. Свадьба весной?",
    "💞 {a} тайно вздыхает по {b}. Это видно по эмодзи.",
    "💕 {a} и {b} вместе смотрятся лучше, чем половина пар в чате.",
    "💓 {a}, признайся: {b} тебе нравится. Мы всё видели.",
    "💗 Сегодня в меню: {a} + {b} = любовь.",
    "🥰 {a} и {b} — идеальная пара. Не спорьте с ботом.",
    "💖 {a} уже придумал(а) имена детям с {b}.",
    "😍 {a}, {b} — твой(я) crush. Мы знаем.",
]


async def cmd_ship(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = msg.text or ""

    # Собираем цели: реплай + @username
    names = []
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        names.append(u.first_name or (u.username or str(u.id)))
    for uname in USERNAME_RE.findall(text):
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            names.append(chat.first_name or uname)
        except TelegramError:
            names.append(uname)
        if len(names) >= 2:
            break

    if len(names) < 2:
        await msg.reply_text(
            "Формат: <code>Шип @user1 @user2</code> "
            "или реплай + <code>Шип @user</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    a, b = names[0], names[1]
    template = random.choice(TEMPLATES)
    phrase = template.format(a=escape(a), b=escape(b))
    await msg.reply_text(f"💘 <b>Шип</b>\n{phrase}", parse_mode=ParseMode.HTML)
