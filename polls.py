"""Опросы: Опрос ССС и Опрос (простой).

Форматы:
  Опрос ССС / описание /1/ Вариант1 / Вариант2 / ...
  Опрос      / описание /1/ Вариант1 / Вариант2 / ...

/1/ — маркер начала вариантов (цифра не важна).
Опрос живёт 5 минут, потом удаляется из памяти, кнопки убираются.
Голосовать один раз; можно переголосовать (старый голос снимается).
Итог "кто за что" — под сообщением, обновляется при каждом голосе.
"""
import os
import re
import uuid
import time
import logging
from html import escape
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# 5 минут
POLL_TIMEOUT_SEC = 300
MAX_OPTIONS = 10
MIN_OPTIONS = 2
MAX_OPTION_LEN = 60
MAX_DESC_LEN = 200

# poll_id -> dict
POLLS: dict[str, dict] = {}

# Маркер: /1/, /2/, ... (одна или несколько цифр)
_OPTIONS_MARKER = re.compile(r"\s*/\s*\d+\s*/")


def _sss_all() -> set[int]:
    """Ленивый импорт, чтобы не было циклического импорта."""
    import sss
    return sss._sss_all()


def _parse_poll(text: str, trigger: str) -> tuple[Optional[str], list[str]]:
    """Возвращает (описание, [варианты]) или (None, []).

    trigger — regexp команды, например r"Опрос\\s+ССС" или r"Опрос".
    """
    s = (text or "").strip()
    s = re.sub(rf"(?i)^(?:{trigger})\s*", "", s).strip()

    if s.startswith("/"):
        s = s[1:].strip()

    m = _OPTIONS_MARKER.search(s)
    if not m:
        return None, []

    desc = s[:m.start()].strip()
    options_raw = s[m.end():]
    options = [o.strip() for o in options_raw.split("/") if o.strip()]

    if not desc:
        return None, []
    if len(desc) > MAX_DESC_LEN:
        desc = desc[:MAX_DESC_LEN].rstrip() + "…"
    options = [o[:MAX_OPTION_LEN] for o in options]

    return desc, options


def _poll_kb(poll_id: str, options: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([
            InlineKeyboardButton(opt, callback_data=f"poll:{poll_id}:{i}"),
        ])
    return InlineKeyboardMarkup(rows)


def _format_poll_text(t: dict) -> str:
    lines = [f"🗳 <b>{escape(t['title'])}</b>", ""]
    if t.get("desc"):
        lines.append(escape(t["desc"]))
        lines.append("")

    options = t["options"]
    votes_by_opt: dict[int, list[str]] = {i: [] for i in range(len(options))}
    for voter_id, opt_idx in t["votes"].items():
        if 0 <= opt_idx < len(options):
            votes_by_opt[opt_idx].append(t["voter_names"].get(voter_id, str(voter_id)))

    for i, opt in enumerate(options):
        voters = votes_by_opt[i]
        if voters:
            names = ", ".join(escape(n) for n in voters)
            lines.append(f"• <b>{escape(opt)}</b> — {names}")
        else:
            lines.append(f"• <b>{escape(opt)}</b> — <i>нет голосов</i>")

    lines.append("")
    lines.append(f"Всего голосов: <b>{len(t['votes'])}</b>")
    if t.get("sss_only"):
        lines.append("<i>Голосуют только члены ССС.</i>")
    else:
        lines.append("<i>Голосовать может любой.</i>")

    if t.get("finalized"):
        lines.append("<i>Голосование завершено. Опрос удалён.</i>")
    else:
        remaining = max(0, int(t["expires_at"] - time.time()))
        mins, secs = divmod(remaining, 60)
        lines.append(f"<i>Осталось: {mins}:{secs:02d}</i>")
    return "\n".join(lines)


async def _edit_poll_message(
    context: ContextTypes.DEFAULT_TYPE, t: dict, remove_kb: bool = False,
) -> None:
    if not t.get("message_id"):
        return
    try:
        await context.bot.edit_message_text(
            chat_id=t["chat_id"],
            message_id=t["message_id"],
            text=_format_poll_text(t),
            parse_mode=ParseMode.HTML,
            reply_markup=None if remove_kb else _poll_kb(t["poll_id"], t["options"]),
        )
    except TelegramError as e:
        logger.warning("edit poll message: %s", e)


async def _create_poll(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    trigger: str, sss_only: bool,
) -> None:
    user = update.effective_user
    msg = update.effective_message
    chat = update.effective_chat
    if not user or not msg or not chat:
        return

    if sss_only and user.id not in _sss_all():
        await msg.reply_text("⛔ Только участники ССС могут создавать опрос ССС.")
        return

    desc, options = _parse_poll(msg.text or "", trigger)
    if not desc or len(options) < MIN_OPTIONS:
        example = (
            "Опрос ССС / Как правильно есть торт /1/ Ложкой / Вилкой / Жопой"
            if sss_only else
            "Опрос / Как правильно есть торт /1/ Ложкой / Вилкой / Жопой"
        )
        await msg.reply_text(
            f"Формат:\n<code>{escape(example)}</code>\n\n"
            f"Нужно: описание и минимум {MIN_OPTIONS} варианта после <code>/1/</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if len(options) > MAX_OPTIONS:
        await msg.reply_text(
            f"⛔ Слишком много вариантов: {len(options)} (максимум {MAX_OPTIONS})."
        )
        return

    poll_id = uuid.uuid4().hex[:10]
    now = time.time()
    t = {
        "poll_id": poll_id,
        "chat_id": chat.id,
        "title": "Опрос ССС" if sss_only else "Опрос",
        "desc": desc,
        "options": options,
        "votes": {},
        "voter_names": {},
        "sss_only": sss_only,
        "by_user_id": user.id,
        "by_name": user.first_name or user.username or str(user.id),
        "finalized": False,
        "message_id": None,
        "expires_at": now + POLL_TIMEOUT_SEC,
    }
    POLLS[poll_id] = t

    sent = await msg.reply_text(
        _format_poll_text(t),
        parse_mode=ParseMode.HTML,
        reply_markup=_poll_kb(poll_id, options),
    )
    t["message_id"] = sent.message_id

    if context.job_queue:
        context.job_queue.run_once(
            _poll_timeout_job,
            when=POLL_TIMEOUT_SEC,
            data=poll_id,
        )
    else:
        logger.warning("polls: job_queue нет, таймаут не сработает для %s", poll_id)


async def cmd_poll_sss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _create_poll(update, context, r"Опрос\s+ССС", sss_only=True)


async def cmd_poll_public(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _create_poll(update, context, r"Опрос(?!\s+ССС)", sss_only=False)


async def poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "poll":
        await q.answer()
        return
    _, poll_id, opt_str = parts
    t = POLLS.get(poll_id)
    if not t:
        await q.answer("Этот опрос уже завершён и удалён.", show_alert=True)
        return
    if t.get("finalized"):
        await q.answer("Голосование уже закрыто.", show_alert=True)
        return
    if time.time() >= t["expires_at"]:
        await _finalize_poll(context, poll_id)
        await q.answer("Опрос уже завершён.", show_alert=True)
        return

    try:
        opt_idx = int(opt_str)
    except ValueError:
        await q.answer("Ошибка варианта.", show_alert=True)
        return
    if opt_idx < 0 or opt_idx >= len(t["options"]):
        await q.answer("Вариант не найден.", show_alert=True)
        return

    if t["sss_only"] and user.id not in _sss_all():
        await q.answer("Голосуют только члены ССС.", show_alert=True)
        return

    t["votes"][user.id] = opt_idx
    t["voter_names"][user.id] = user.first_name or user.username or str(user.id)

    await q.answer("Голос учтён")
    await _edit_poll_message(context, t)


async def _finalize_poll(context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    t = POLLS.get(poll_id)
    if not t or t.get("finalized"):
        return
    t["finalized"] = True
    await _edit_poll_message(context, t, remove_kb=True)
    POLLS.pop(poll_id, None)
    logger.info("poll %s finalized and removed", poll_id)


async def _poll_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    poll_id = job.data if job else None
    if not poll_id:
        return
    await _finalize_poll(context, poll_id)


def cleanup_expired_polls() -> int:
    """Удаляет просроченные опросы из памяти (страховка)."""
    now = time.time()
    expired = [pid for pid, t in POLLS.items() if now >= t["expires_at"]]
    for pid in expired:
        POLLS.pop(pid, None)
    if expired:
        logger.info("polls: cleanup removed %d expired", len(expired))
    return len(expired)
