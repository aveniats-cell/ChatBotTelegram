"""Экономика: кредит (+Кредит / -Кредит), выдача/списание монет админом,
перевод с комиссией, казна, ежедневное начисление процентов на долг."""
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

TRANSFER_FEE_PCT = 3
TRANSFER_MIN = 5
TRANSFER_FEE_MIN = 1

TZ = timezone.utc


def _today_utc() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ---------- +Кредит / -Кредит ----------

async def cmd_credit_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    ok = await db.take_credit_daily_atomic(
        user.id, amount, _today_utc(), balance_floor=0,
    )
    if not ok:
        fresh = await db.get_user(user.id)
        f_balance = (fresh or {}).get("balance") or 0
        f_credit = (fresh or {}).get("credit") or 0
        f_day = (fresh or {}).get("credit_taken_day")
        if f_credit > 0:
            reason = f"У вас уже есть долг <b>{f_credit}</b>."
        elif f_day == _today_utc():
            reason = "Вы уже брали кредит сегодня."
        elif f_balance < 0:
            reason = "У вас отрицательный баланс."
        else:
            reason = "Не удалось выдать кредит (ошибка БД). Попробуйте позже."
        await msg.reply_text(
            f"⛔ Кредит не выдан. {reason}",
            parse_mode=ParseMode.HTML,
        )
        return

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
    if balance <= 0:
        await msg.reply_text(
            f"⛔ Недостаточно монет для погашения. Баланс: <b>{balance}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    amount = min(amount, credit, balance)
    if amount <= 0:
        await msg.reply_text("⛔ Нечего погашать.")
        return

    ok = await db.repay_credit_atomic(user.id, amount)
    if not ok:
        await msg.reply_text(
            "⚠️ Не удалось погасить (ошибка БД или изменился баланс). "
            "Попробуйте ещё раз."
        )
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or 0
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"✅ Погашено <b>{amount}</b>.\n"
        f"Остаток долга: <b>{new_credit}</b>.\n"
        f"Баланс: <b>{new_balance}</b>.",
        parse_mode=ParseMode.HTML,
    )


# ---------- +Монеты / -Монеты (админ) ----------

async def cmd_admin_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Монеты N @user — админ начисляет монеты (без лимита)."""
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
    if amount == 0:
        await msg.reply_text("⛔ Сумма не может быть нулевой.")
        return

    clean = re.sub(r"\+Монеты\s+-?\d+", "", text, flags=re.IGNORECASE)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text(
            "⚠️ Не удалось изменить баланс (ошибка БД). Попробуйте ещё раз."
        )
        return

    new_row = await db.get_user(uid)
    if not new_row:
        await msg.reply_text(
            "⚠️ Баланс изменён, но профиль не читается. Проверьте «Профиль»."
        )
        return
    new_balance = new_row.get("balance") or 0

    sign = "+" if amount >= 0 else ""
    await msg.reply_text(
        f"💰 {escape(str(name))}: <b>{sign}{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin_coins_minus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Монеты N @user — админ списывает монеты. Списание уходит в казну."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"[-–−]Монеты\s+(\d+)", text, re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Монеты 100 @user</code> или реплай + "
            "<code>-Монеты 100</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    clean = re.sub(r"[-–−]Монеты\s+\d+", "", text, flags=re.IGNORECASE)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    taken = await db.take_balance_any(uid, amount)
    if taken == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего списывать (баланс 0 или меньше).",
            parse_mode=ParseMode.HTML,
        )
        return

    chat = update.effective_chat
    if chat:
        try:
            await db.treasury_add(chat.id, taken)
        except Exception as e:
            logger.warning("cmd_admin_coins_minus treasury: %s", e)

    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    note = ""
    if taken < amount:
        note = f"\n<i>Списано меньше запрошенного: было только {taken}.</i>"
    await msg.reply_text(
        f"💸 {escape(str(name))}: <b>-{taken}</b>\n"
        f"Баланс: <b>{new_balance}</b>{note}",
        parse_mode=ParseMode.HTML,
    )


# ---------- Перевод ----------

async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перевод N @user — перевод монет с комиссией 3% (мин. 1)."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    text = msg.text or ""
    m = re.search(r"(?i)^Перевод\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>Перевод 100 @user</code> или реплай + "
            "<code>Перевод 100</code>.\n"
            f"Комиссия: {TRANSFER_FEE_PCT}% (мин. {TRANSFER_FEE_MIN}), "
            f"минимум перевода — {TRANSFER_MIN}.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount < TRANSFER_MIN:
        await msg.reply_text(f"⛔ Минимум перевода — {TRANSFER_MIN} монет.")
        return

    clean = re.sub(r"(?i)^Перевод\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == user.id:
        await msg.reply_text("Себе переводить нельзя 🙂")
        return

    row = await db.get_user(uid)
    if not row:
        await db.upsert_user(uid, None, name)

    fee = max(TRANSFER_FEE_MIN, int(amount * TRANSFER_FEE_PCT / 100))
    if fee >= amount:
        await msg.reply_text("⛔ Сумма слишком мала для комиссии.")
        return

    ok = await db.transfer_atomic(user.id, uid, amount, fee)
    if not ok:
        me_row = await db.get_user(user.id)
        bal = (me_row or {}).get("balance") or 0
        await msg.reply_text(
            f"⛔ Недостаточно монет. Баланс: <b>{bal}</b>, нужно: <b>{amount}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await db.treasury_add(chat.id, fee)
    except Exception as e:
        logger.warning("cmd_transfer treasury: %s", e)

    me_row = await db.get_user(user.id)
    new_bal = (me_row or {}).get("balance") or 0
    net = amount - fee
    await msg.reply_text(
        f"💸 <b>Перевод</b>\n"
        f"Кому: {escape(str(name))}\n"
        f"Сумма: <b>{amount}</b> · комиссия <b>{fee}</b> "
        f"({TRANSFER_FEE_PCT}%, мин. {TRANSFER_FEE_MIN})\n"
        f"Получатель получил: <b>{net}</b>\n"
        f"Ваш баланс: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


# ---------- Казна ----------

async def cmd_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Казна — админ смотрит накопления комиссий в чате."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🏦 <b>Казна чата</b>\n"
        f"Накоплено: <b>{bal}</b> монет\n\n"
        f"<i>Сюда идут комиссии казино, крестиков-ноликов, переводов "
        f"и списаний через -Монеты.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- Ежедневные проценты ----------

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
        daily_interest_job,
        time=dtime(hour=0, minute=10, tzinfo=TZ),
    )
    logger.info("economy: ежедневное начисление %% на кредит запланировано (UTC)")
