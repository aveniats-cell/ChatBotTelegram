"""Экономика: кредит (+Кредит / -Кредит), выдача монет админом (+Монеты),
ежедневное начисление процентов на остаток долга.

P0: +Кредит / -Кредит — один атомарный UPDATE (db.take_credit_atomic /
db.repay_credit_atomic), проверка rows_affected.
P2: CREDIT_DAILY_LIMIT проверяет «сегодня» через users.credit_taken_day.
_resolve_target вынесен в utils.resolve_target.
"""
import os
import re
import logging
from datetime import time as dtime, datetime, timezone
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
AMOUNT_RE = re.compile(r"\b(\d+)\b")

CREDIT_DAILY_LIMIT = 100
CREDIT_PERCENT_PER_DAY = 10


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def cmd_credit_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Кредит N — взять кредит (1..100). Нельзя, если уже в минусе/есть долг."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    text = msg.text or ""
    nums = AMOUNT_RE.findall(text)
    if not nums:
        await msg.reply_text(
            "Формат: <code>+Кредит 50</code> (от 1 до 100).",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(nums[0])
    if amount < 1 or amount > CREDIT_DAILY_LIMIT:
        await msg.reply_text(f"⛔ Сумма кредита: от 1 до {CREDIT_DAILY_LIMIT}.")
        return

    row = await db.get_user(user.id)
    if not row:
        await db.upsert_user(user.id, user.username, user.first_name)
        row = await db.get_user(user.id)
    if not row:
        await msg.reply_text("⚠️ Не удалось получить профиль.")
        return

    balance = row.get("balance") or 0
    credit = row.get("credit") or 0
    taken_day = row.get("credit_taken_day")

    if balance < 0:
        await msg.reply_text(
            "⛔ У вас отрицательный баланс — новый кредит взять нельзя.\n"
            "Погасите долг: <code>-Кредит СУММА</code> или попросите админа.",
            parse_mode=ParseMode.HTML,
        )
        return
    if credit > 0:
        await msg.reply_text(
            f"⛔ У вас уже есть долг <b>{credit}</b>. Сначала погасите его.",
            parse_mode=ParseMode.HTML,
        )
        return
    if taken_day == _today_utc():
        await msg.reply_text(
            "⛔ Вы уже брали кредит сегодня. Повторите завтра.",
            parse_mode=ParseMode.HTML,
        )
        return

    ok = await db.take_credit_atomic(user.id, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось выдать кредит (ошибка БД). Попробуйте позже.")
        return
    await db.set_credit_taken_day(user.id, _today_utc())

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or (credit + amount)
    await msg.reply_text(
        f"💳 Взят кредит: <b>{amount}</b>\n"
        f"Долг: <b>{new_credit}</b>\n"
        f"Ставка: <b>{CREDIT_PERCENT_PER_DAY}%</b> в день на остаток.\n"
        f"Погасить: <code>-Кредит СУММА</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_credit_repay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Кредит N — погасить часть долга (списывает с баланса)."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    text = msg.text or ""
    nums = AMOUNT_RE.findall(text)
    if not nums:
        await msg.reply_text("Формат: <code>-Кредит 50</code>.", parse_mode=ParseMode.HTML)
        return
    amount = int(nums[0])
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    row = await db.get_user(user.id)
    if not row:
        await msg.reply_text("⚠️ Профиль не найден.")
        return
    balance = row.get("balance") or 0
    credit = row.get("credit") or 0

    if credit <= 0:
        await msg.reply_text("✅ У вас нет долга.")
        return
    if balance < amount:
        await msg.reply_text(
            f"⛔ Недостаточно монет. Баланс: <b>{balance}</b>, "
            f"погашение: <b>{amount}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if amount > credit:
        amount = credit

    ok = await db.repay_credit_atomic(user.id, amount)
    if not ok:
        await msg.reply_text(
            "⚠️ Не удалось погасить (ошибка БД или изменился баланс). "
            "Попробуйте ещё раз."
        )
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or 0
    await msg.reply_text(
        f"✅ Погашено <b>{amount}</b>.\n"
        f"Остаток долга: <b>{new_credit}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Монеты N @user — админ начисляет/списывает монеты (без лимита)."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"\+Монеты\s+(-?\d+)", text, re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>+Монеты 100 @user</code> или реплай + "
            "<code>+Монеты 100</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))

    clean = re.sub(r"\+Монеты\s+-?\d+", "", text, flags=re.IGNORECASE)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    row = await db.get_user(uid)
    if not row:
        await db.upsert_user(uid, None, name)
        row = await db.get_user(uid)

    await db.add_balance(uid, amount)
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance", 0)

    sign = "+" if amount >= 0 else ""
    await msg.reply_text(
        f"💰 {escape(name)}: <b>{sign}{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def daily_interest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        affected = await db.apply_credit_interest(CREDIT_PERCENT_PER_DAY)
        logger.info("daily_interest_job: затронуто записей: %s", affected)
    except Exception as e:
        logger.warning("daily_interest_job: %s", e)


def register_jobs(app) -> None:
    if not app.job_queue:
        logger.warning("economy: job_queue нет, проценты по кредиту не будут начисляться")
        return
    app.job_queue.run_daily(
        daily_interest_job, time=dtime(hour=0, minute=10, tzinfo=None),
    )
    logger.info("economy: ежедневное начисление %% на кредит запланировано")
