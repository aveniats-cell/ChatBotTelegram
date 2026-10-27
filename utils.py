"""Общие утилиты: единый resolve @username -> user_id.

Используется всеми модулями (roles, wedding, sss, profile, economy,
tictactoe) — вместо шести копий _resolve_target.
"""
import re
import logging
from typing import Optional

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import resolver

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


async def resolve_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    arg_text: str,
) -> Optional[tuple[int, str]]:
    """Резолвит цель: реплай → @username (БД → Telethon → get_chat)."""
    msg = update.effective_message
    if not msg:
        return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or u.username or str(u.id)

    m = USERNAME_RE.search(arg_text or "")
    if not m:
        return None
    uname = m.group(1)

    try:
        uid = await db.get_user_by_username(uname)
        if uid:
            return uid, uname
    except Exception as e:
        logger.warning("get_user_by_username(%s): %s", uname, e)

    r = await resolver.resolve_username(uname)
    if r:
        return r

    try:
        chat = await context.bot.get_chat(f"@{uname}")
        return chat.id, chat.first_name or uname
    except TelegramError as e:
        logger.warning("get_chat @%s: %s", uname, e)
    return None


async def resolve_two(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> list[tuple[int, str]]:
    """Резолвит до двух целей: реплай + @username'ы из текста."""
    targets: list[tuple[int, str]] = []
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        targets.append((u.id, u.first_name or u.username or str(u.id)))

    for uname in USERNAME_RE.findall(text or ""):
        if len(targets) >= 2:
            break
        try:
            uid = await db.get_user_by_username(uname)
            if uid:
                targets.append((uid, uname))
                continue
        except Exception as e:
            logger.warning("get_user_by_username(%s): %s", uname, e)
        r = await resolver.resolve_username(uname)
        if r:
            targets.append(r)
            continue
        try:
            c = await context.bot.get_chat(f"@{uname}")
            targets.append((c.id, c.first_name or uname))
        except TelegramError as e:
            logger.warning("get_chat @%s: %s", uname, e)
    return targets
