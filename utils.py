"""Общие утилиты: resolve @username -> user_id, парсер длительности."""
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

_DUR_RE = re.compile(
    r"(?i)(\d+)\s*"
    r"(ч(?:ас(?:а|ов)?)?|h|м(?:ин(?:ут(?:а|ы)?)?)?|m|с(?:ек(?:унд(?:а|ы)?)?)?|s)"
)


def _unit_to_sec(unit: str) -> int:
    u = unit.lower()
    if u.startswith("ч") or u == "h":
        return 3600
    if u.startswith("м") or u == "m":
        return 60
    return 1


def parse_duration(text: str) -> tuple[int, str]:
    """'5с' -> (5,''), '5ч 3с' -> (18003,''), '5ч Богохульство' -> (18000,'Богохульство').

    ВНИМАНИЕ: если в причине есть число + единица времени (например
    «оскорблял 5 минут»), оно попадёт в длительность. Это осознанный
    компромисс — иначе нельзя отличить «5ч» от «5 часов в причине».
    """
    if not text:
        return 0, text

    total = 0
    last_end = 0
    found = False

    for m in _DUR_RE.finditer(text):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        total += num * _unit_to_sec(m.group(2))
        last_end = m.end()
        found = True

    if not found:
        return 0, text

    rest = text[last_end:].strip(" ,;:-")
    return total, rest


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}с")
    return " ".join(parts)


async def _name_for_uid(uid: int, fallback: str) -> str:
    try:
        row = await db.get_user(uid)
        if row and row.get("first_name"):
            return row["first_name"]
    except Exception:
        pass
    return fallback


async def resolve_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    arg_text: str,
) -> Optional[tuple[int, str]]:
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
            name = await _name_for_uid(uid, uname)
            return uid, name
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
                name = await _name_for_uid(uid, uname)
                targets.append((uid, name))
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
