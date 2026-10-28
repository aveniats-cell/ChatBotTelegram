"""2.4 Шип: Шип @u1 @u2 -> шуточная фраза про их любовь."""
import random
import logging
import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from cachetools import TTLCache

import resolver
import ai

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

_PHRASE_CACHE: TTLCache = TTLCache(maxsize=2000, ttl=600)

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


def _fallback_phrase(a: str, b: str) -> str:
    return random.choice(TEMPLATES).format(a=escape(a), b=escape(b))


def _build_ai_prompt(a_name: str, b_name: str) -> str:
    return (
        f"Пара: {a_name} и {b_name}. "
        f"Это шуточный «шип» в Telegram-чате для 15–20 лет. "
        f"Напиши одну короткую фразу (1–2 предложения) про их «отношения». "
        f"Юмор дружелюбный, без оскорблений, без мата, без политики. "
        f"Можно использовать эмодзи. "
        f"Не пиши имена в кавычках, просто используй их. "
        f"Пример тона: «{a_name} уже придумал имена детям с {b_name}»."
    )


async def _generate_phrase(a_id: int, a_name: str, b_id: int, b_name: str) -> str:
    key = (min(a_id, b_id), max(a_id, b_id))
    cached = _PHRASE_CACHE.get(key)
    if cached:
        return cached

    prompt = _build_ai_prompt(a_name, b_name)
    try:
        answer = await ai.ask_ai(prompt, user_id=a_id)
    except Exception as e:
        logger.warning("ship ai: %s", e)
        answer = None

    if not answer:
        return _fallback_phrase(a_name, b_name)

    text = answer.strip().replace("\n", " ")
    if len(text) > 300:
        text = text[:300].rstrip() + "…"

    if len(text) < 10:
        return _fallback_phrase(a_name, b_name)

    _PHRASE_CACHE[key] = text
    return text


async def cmd_ship(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = msg.text or ""

    names = []
    ids = []

    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        names.append(u.first_name or (u.username or str(u.id)))
        ids.append(u.id)

    for uname in USERNAME_RE.findall(text):
        if len(names) >= 2:
            break
        r = await resolver.resolve_username(uname)
        if r:
            ids.append(r[0])
            names.append(r[1])
        else:
            ids.append(0)
            names.append(uname)

    if len(names) < 2:
        await msg.reply_text(
            "Формат: <code>Шип @user1 @user2</code> "
            "или реплай + <code>Шип @user</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    a, b = names[0], names[1]
    a_id, b_id = ids[0], ids[1]

    if a.casefold() == b.casefold():
        await msg.reply_text("Шип с самим собой? Ну ладно 🙂")
        return

    if a_id == 0 or b_id == 0:
        phrase = _fallback_phrase(a, b)
    else:
        phrase = await _generate_phrase(a_id, a, b_id, b)

    await msg.reply_text(
        f"💘 <b>Шип</b>\n{phrase}",
        parse_mode=ParseMode.HTML,
    )
