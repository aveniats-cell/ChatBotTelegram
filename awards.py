"""+Награда / -Награда: админ (1–10★) и комиссар (1–6★).

Формат:
    +Награда Название / 5 (реплай или @user)
    -Награда Название (реплай или @user)
"""
import os
import re
import logging
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

# +Награда <название> / <звёзды>  — звёзды в конце через слэш.
_AWARD_RE = re.compile(r"(?i)^\s*\+Награда\s+(.+?)\s*/\s*(\d+)\s*$")
_UNAWARD_RE = re.compile(r"(?i)^\s*-Награда\s+(.+?)\s*$")


async def _is_commissar(user_id: int) -> bool:
    try:
        return (await db.get_role(user_id)) == "commissar"
    except Exception as e:
        logger.warning("get_role(%s): %s", user_id, e)
        return False


def _clean_title(raw: str) -> str:
    """Убирает @username из названия награды."""
    t = USERNAME_RE.sub("", raw or "").strip(" ,;:-")
    return t[:100]


async def cmd_award(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    is_admin = bool(ADMIN_ID) and user.id == ADMIN_ID
    is_commissar = await _is_commissar(user.id)
    if not (is_admin or is_commissar):
        await msg.reply_text("⛔ Только админ или комиссар.")
        return

    m = _AWARD_RE.match(msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>+Награда За честь / 5</code> "
            "(звёзды: 1–10 у админа, 1–6 у комиссара).",
            parse_mode=ParseMode.HTML,
        )
        return

    raw_title = m.group(1)
    stars = int(m.group(2))

    title = _clean_title(raw_title)
    if not title:
        await msg.reply_text("Не понял название награды.")
        return

    max_stars = 10 if is_admin else 6
    if stars < 1 or stars > max_stars:
        await msg.reply_text(
            f"⛔ Звёзды: от 1 до {max_stars} (для {'админа' if is_admin else 'комиссара'})."
        )
        return

    target = await utils.resolve_target(update, context, raw_title)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    if ADMIN_ID and uid == ADMIN_ID:
        await msg.reply_text("Админу награды не выдаются.")
        return

    aid = await db.add_award(chat.id, uid, title, stars, user.id)
    if aid == 0:
        await msg.reply_text("⚠️ Не удалось сохранить награду в БД.")
        return

    star_str = "★" * stars + "☆" * (10 - stars)
    await msg.reply_text(
        f"🎖️ <b>Награда</b>\n"
        f"Кому: {escape(name)}\n"
        f"За что: {escape(title)}\n"
        f"Ценность: <b>{stars}/10</b> {star_str}\n"
        f"Выдал: {escape(user.first_name or str(user.id))}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unaward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    is_admin = bool(ADMIN_ID) and user.id == ADMIN_ID
    is_commissar = await _is_commissar(user.id)
    if not (is_admin or is_commissar):
        await msg.reply_text("⛔ Только админ или комиссар.")
        return

    m = _UNAWARD_RE.match(msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>-Награда За честь</code> (реплай или @user).",
            parse_mode=ParseMode.HTML,
        )
        return

    title = _clean_title(m.group(1))
    if not title:
        await msg.reply_text("Не понял название награды.")
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    removed = await db.remove_award_by_title(chat.id, uid, title)
    if removed == 0:
        await msg.reply_text(
            f"У {escape(name)} нет награды «{escape(title)}»."
        )
        return

    await msg.reply_text(
        f"❌ Награда «{escape(title)}» снята с {escape(name)}.",
        parse_mode=ParseMode.HTML,
    )
