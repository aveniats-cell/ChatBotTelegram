"""2.3 Топ чата + зарплата за активность из казны.

- Сообщения копятся в памяти, сбрасываются раз в FLUSH_INTERVAL_SEC
  идемпотентным батчем (защита от Server disconnected).
- Зарплата: 1 сообщение >= MIN_SALARY_LEN символов = +SALARY_FRAC_PER_MSG
  сотых монеты (0.20 = 20). Не команда, не от бота, не реплай на бота.
  Списывается из казны через db.pay_salary_from_treasury.
- flush_job после _flush проверяет казну: если <= 0 — дефолт.
"""
import asyncio
import os
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 15
FLUSH_MAX_KEYS = 200

MIN_SALARY_LEN = 2
SALARY_FRAC_PER_MSG = 20  # 0.20 монеты за сообщение

_USER_CACHE: dict[int, tuple[str, str]] = {}

_PENDING: dict[tuple[int, int, str], int] = defaultdict(int)
_PENDING_SALARY: dict[tuple[int, int, str], int] = defaultdict(int)
_flush_lock = asyncio.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return
    chat = update.effective_chat
    if not chat:
        return
    try:
        cur = (user.username or "", user.first_name or "")
        if _USER_CACHE.get(user.id) != cur:
            await db.upsert_user(user.id, user.username, user.first_name)
            _USER_CACHE[user.id] = cur

        day = _today()
        key = (chat.id, user.id, day)
        _PENDING[key] += 1

        text = (msg.text or "").strip()
        is_cmd = text.startswith("/")
        reply_to_bot = bool(
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == context.bot.id
        )
        if (
            not is_cmd
            and not reply_to_bot
            and len(text) >= MIN_SALARY_LEN
        ):
            _PENDING_SALARY[key] += SALARY_FRAC_PER_MSG

        if len(_PENDING) + len(_PENDING_SALARY) >= FLUSH_MAX_KEYS:
            await _flush()
    except Exception as e:
        logger.warning("track_message: %s", e)


async def _flush() -> None:
    async with _flush_lock:
        if not _PENDING and not _PENDING_SALARY:
            return
        snap_msg = dict(_PENDING)
        snap_sal = dict(_PENDING_SALARY)
        _PENDING.clear()
        _PENDING_SALARY.clear()

    # 1) Сообщения — идемпотентный батч.
    for (chat_id, user_id, day), delta in snap_msg.items():
        if delta <= 0:
            continue
        key = uuid.uuid4().hex
        ok = False
        for attempt in range(3):
            ok = await db.inc_message_batch_idempotent(
                chat_id, user_id, day, delta, key,
            )
            if ok:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if not ok:
            logger.error(
                "flush msg: потерян батч (chat=%s user=%s day=%s delta=%s)",
                chat_id, user_id, day, delta,
            )

    # 2) Зарплата — из казны.
    for (chat_id, user_id, day), frac_delta in snap_sal.items():
        if frac_delta <= 0:
            continue
        ok = False
        for attempt in range(3):
            coins = await db.pay_salary_from_treasury(
                chat_id, user_id, day, frac_delta,
            )
            if coins is not None:
                ok = True
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if not ok:
            logger.warning(
                "flush salary: казна пуста или ошибка (chat=%s user=%s)",
                chat_id, user_id,
            )


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _flush()
    except Exception as e:
        logger.warning("flush_job: %s", e)

    # После зарплаты проверяем казну. Если <= 0 — дефолт.
    try:
        import economy
        chat_id = int(os.getenv("CHAT_ID", "0") or "0")
        if chat_id:
            bal = await db.treasury_get(chat_id)
            if bal <= 0:
                await economy._run_default(context, chat_id)
    except Exception as e:
        logger.warning("flush_job default: %s", e)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        deleted = await db.cleanup_old_days(keep_days=0)
        logger.info("cleanup_job: удалено строк messages_day: %s", deleted)
    except Exception as e:
        logger.warning("cleanup_job: %s", e)


def _medal(i: int) -> str:
    if i == 1:
        return "🥇"
    if i == 2:
        return "🥈"
    if i == 3:
        return "🥉"
    return f"{i}."


async def _build_top_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 50,
    title: str = "🏆 Топ чата",
) -> str:
    rows = await db.top_messages(chat_id, limit=limit)
    if not rows:
        return "Пока никто не писал — топ пуст."
    lines = [f"<b>{escape(title)}</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        name = str(uid)
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.first_name or chat.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"{_medal(i)} {escape(name)} — <b>{cnt}</b>")
    return "\n".join(lines)


async def _build_top_day_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 50,
) -> str:
    day = _today()
    rows = await db.top_messages_day(chat_id, day, limit=limit)
    if not rows:
        return "Сегодня ещё никто не писал — топ дня пуст."
    lines = [f"<b>🏆 Топ за день ({day})</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        name = str(uid)
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.first_name or chat.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"{_medal(i)} {escape(name)} — <b>{cnt}</b>")
    return "\n".join(lines)


async def cmd_top_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    try:
        await _flush()
    except Exception:
        pass
    text = await _build_top_text(context, chat.id, limit=50, title="🏆 Топ за всё время")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    try:
        await _flush()
    except Exception:
        pass
    text = await _build_top_day_text(context, chat.id, limit=50)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_my_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    try:
        await _flush()
    except Exception:
        pass
    res = await db.user_rank(chat.id, user.id)
    if not res:
        await update.message.reply_text("Вы ещё не писали в этом чате — места нет.")
        return
    count, rank, total = res
    await update.message.reply_text(
        f"📊 Ваше место: <b>{rank}/{total}</b>\n"
        f"Сообщений: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )
