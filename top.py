"""2.3 Топ чата + зарплата за активность из казны.

Исправления:
  - дробная зарплата копится в персистентной таблице pending_salary (в БД),
    а не только в памяти — переживает рестарты Render;
  - _flush больше не теряет батчи при рестарте;
  - flush_job БОЛЬШЕ НЕ ВЫЗЫВАЕТ дефолт (дефолт — только в
    economy.default_watchdog_job раз в час и в _check_default_after
    после +Кредит / -Утил казна) — это убирает лавину дефолтов каждые 15 сек;
  - cleanup_job для pending_salary (старше 2 дней);
  - cleanup_old_days(keep_days=1): НЕ стираем вчерашний messages_day до налога
    в 00:40 (иначе tax_active_balances всегда видел «некому платить»);
  - flush_job catch-up: платим ВСЕ накопленные дроби (без day >= today),
    иначе вчерашние долги висели мёртвым грузом;
  - _flush при ошибке add_pending_salary больше не теряет батч в памяти:
    retry + лог, а не тихий requeue.
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
SALARY_FRAC_PER_MSG = 20

_USER_CACHE: dict[int, tuple[str, str]] = {}

# _PENDING — счётчики сообщений для messages/messages_day (не критичны к потере).
# _PENDING_SALARY — in-memory буфер перед записью в pending_salary.
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


def _requeue_msg(chat_id: int, user_id: int, day: str, delta: int) -> None:
    if delta > 0:
        _PENDING[(chat_id, user_id, day)] += delta


def _requeue_sal(chat_id: int, user_id: int, day: str, delta: int) -> None:
    if delta > 0:
        _PENDING_SALARY[(chat_id, user_id, day)] += delta


async def _flush() -> None:
    async with _flush_lock:
        if not _PENDING and not _PENDING_SALARY:
            return
        snap_msg = dict(_PENDING)
        snap_sal = dict(_PENDING_SALARY)
        _PENDING.clear()
        _PENDING_SALARY.clear()

    # 1) messages (не критично: при потере — только счётчик топов)
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
            _requeue_msg(chat_id, user_id, day, delta)

    # 2) зарплата: сначала фиксируем дроби в персистентной таблице,
    #    потом пытаемся выплатить по этой таблице.
    for (chat_id, user_id, day), frac_delta in snap_sal.items():
        if frac_delta <= 0:
            continue
        saved = False
        for attempt in range(3):
            saved = await db.add_pending_salary(chat_id, user_id, day, frac_delta)
            if saved:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if not saved:
            logger.error(
                "flush salary: не удалось сохранить pending_salary "
                "(chat=%s user=%s day=%s delta=%s) — возвращаем в память для ретрая",
                chat_id, user_id, day, frac_delta,
            )
            _requeue_sal(chat_id, user_id, day, frac_delta)
            continue

        await _pay_pending_salary(chat_id, user_id, day)


async def _pay_pending_salary(chat_id: int, user_id: int, day: str) -> None:
    """Пытается выплатить накопленное из pending_salary.
    Если казна пуста — оставляет в pending_salary до следующего тика.
    """
    frac = await db.get_pending_salary(chat_id, user_id, day)
    if frac <= 0:
        return

    taken = await db.take_pending_salary(chat_id, user_id, day, frac)
    if taken <= 0:
        return

    coins = await db.pay_salary_from_treasury(chat_id, user_id, day, taken)
    if coins is None:
        # не удалось (казна пуста / ошибка) — возвращаем дроби в очередь
        logger.info(
            "salary: не выплачено, возврат в pending_salary (chat=%s user=%s frac=%s)",
            chat_id, user_id, taken,
        )
        await db.return_pending_salary(chat_id, user_id, day, taken)
    else:
        logger.info(
            "salary: выплачено coins=%s (chat=%s user=%s frac_was=%s)",
            coins, chat_id, user_id, taken,
        )


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _flush()
    except Exception as e:
        logger.warning("flush_job: %s", e)

    # Догоняем pending_salary за ВСЕ дни (если казна была пуста — до-платим).
    # cleanup_job всё равно удалит строки старше 2 дней, так что не боимся
    # бесконечного накопления.
    try:
        rs = await db._exec(
            """SELECT DISTINCT chat_id, user_id, day FROM pending_salary
               WHERE frac_delta > 0"""
        )
        for chat_id, user_id, day in rs.rows:
            await _pay_pending_salary(chat_id, user_id, day)
    except Exception as e:
        logger.warning("flush_job catch-up: %s", e)

    # ВАЖНО: дефолт здесь НЕ вызываем (см. economy.default_watchdog_job).


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # keep_days=1: не стираем вчерашний messages_day до налога в 00:40.
    try:
        deleted = await db.cleanup_old_days(keep_days=1)
        logger.info("cleanup_job: удалено строк messages_day: %s", deleted)
    except Exception as e:
        logger.warning("cleanup_job: %s", e)
    try:
        deleted_pending = await db.cleanup_pending_salary(keep_days=2)
        logger.info("cleanup_job: удалено строк pending_salary: %s", deleted_pending)
    except Exception as e:
        logger.warning("cleanup_job pending_salary: %s", e)


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
