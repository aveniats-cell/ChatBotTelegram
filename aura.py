"""Аурометр: раз в час «дроч» +1 к ауре, просмотр и топ-10.

- «дроч»     — +1 к ауре, не чаще раза в час (UTC, атомарно в UPDATE).
- «Аура»     — своя аура + место в топе.
- «Топ аура» — топ-10 в чате по ауре.
"""
import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

AURA_COOLDOWN_SEC = 3600  # 1 час


def _fmt_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


async def cmd_droch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """дроч — раз в час +1 к ауре."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    try:
        await db.upsert_user(user.id, user.username, user.first_name)
    except Exception as e:
        logger.warning("upsert_user in cmd_droch: %s", e)

    ok = await db.aura_droch(chat.id, user.id)
    if not ok:
        row = await db.aura_get(chat.id, user.id)
        last = (row or {}).get("last_droch_at")
        if last:
            # last_droch_at — «YYYY-MM-DD HH:MM:SS» в UTC (datetime('now')).
            # Остаток считаем на стороне SQLite, чтобы не парсить в Python.
            rs = await db._exec(
                "SELECT CAST((julianday(datetime('now')) - "
                "julianday(datetime(?, '+1 hour'))) * 86400 AS INTEGER)",
                [last],
            )
            remain = 0
            if rs.rows:
                remain = max(0, -(rs.rows[0][0] or 0))
            await msg.reply_text(
                f"⏳ Рано! Следующий раз через <b>{_fmt_hms(remain)}</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await msg.reply_text(
                "⚠️ Не удалось начислить ауру (ошибка БД). Попробуйте ещё раз."
            )
        return

    row = await db.aura_get(chat.id, user.id)
    value = (row or {}).get("value") or 0
    rank = await db.aura_rank(chat.id, user.id)
    rank_line = ""
    if rank:
        _, r, total = rank
        rank_line = f"\nМесто в чате: <b>{r}/{total}</b>"

    await msg.reply_text(
        f"✨ +1 аура!\n"
        f"Текущая аура: <b>{value}</b>{rank_line}\n"
        f"<i>Следующий раз — через час.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_aura(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Аура — показать свою ауру (и место в топе)."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    res = await db.aura_rank(chat.id, user.id)
    if not res:
        await msg.reply_text(
            "У вас пока нет ауры. Напишите <code>дроч</code> — "
            "и она появится.",
            parse_mode=ParseMode.HTML,
        )
        return
    value, rank, total = res
    await msg.reply_text(
        f"✨ <b>Ваша аура:</b> {value}\n"
        f"Место в чате: <b>{rank}/{total}</b>",
        parse_mode=ParseMode.HTML,
    )


def _medal(i: int) -> str:
    if i == 1:
        return "🥇"
    if i == 2:
        return "🥈"
    if i == 3:
        return "🥉"
    return f"{i}."


async def cmd_aura_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Топ аура — топ-10 по ауре в текущем чате."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    rows = await db.aura_top(chat.id, limit=10)
    if not rows:
        await msg.reply_text(
            "Топ ауры пуст. Напишите <code>дроч</code>, чтобы начать.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["✨ <b>Топ ауры</b>"]
    for i, (uid, value) in enumerate(rows, start=1):
        name = str(uid)
        try:
            c = await context.bot.get_chat(uid)
            name = c.first_name or c.username or str(uid)
        except TelegramError:
            urow = await db.get_user(uid)
            if urow and urow.get("first_name"):
                name = urow["first_name"]
        lines.append(f"{_medal(i)} {escape(name)} — <b>{value}</b>")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
