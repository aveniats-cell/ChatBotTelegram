"""Экономика: кредит, монеты, перевод, казна, налог, зарплата, проценты.

- +Кредит / -Кредит — кредит (1–100, раз в день), погашение.
- +Монеты / -Монеты — админ начисляет/списывает.
- Перевод — перевод с комиссией 3%.
- Казна — баланс казны чата (админ).
- +ВКассу — админ пополняет казну.
- Налог дня — раз в день списывает 3% с баланса > 50 только у активных,
  кладёт в казну, пишет отчёт в чат.
- Зарплата за активность (0.2 монеты за сообщение) — считается в top.py.
- -Утил N @user — сжечь монеты у юзера (в никуда).
- -Утил N казна — сжечь монеты из казны (в никуда).
- +Утил N @user — начислить монеты (из воздуха).
- +Утил N казна — начислить монеты в казну (из воздуха).
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

TRANSFER_FEE_PCT = 3
TRANSFER_MIN = 5
TRANSFER_FEE_MIN = 1

TAX_PCT = 3
TAX_MIN_BALANCE = 50
TAX_ACTIVE_DAYS = 1
TAX_REPORT_CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

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
        f"<i>Сюда идут комиссии казино, крестиков-ноликов, переводов, "
        f"налог и списания через -Монеты.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+ВКассу N — админ пополняет казну."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"\+ВКассу\s+(\d+)", msg.text or "", re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>+ВКассу 500</code>", parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    admin_row = await db.get_user(user.id)
    admin_bal = (admin_row or {}).get("balance") or 0
    if admin_bal >= amount:
        ok = await db.take_balance_atomic(user.id, amount)
        if not ok:
            await msg.reply_text("⚠️ Не удалось списать с баланса.")
            return

    await db.treasury_add(chat.id, amount)
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🏦 В казну добавлено: <b>{amount}</b>\n"
        f"Казна: <b>{new_treasury}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ОбрезатьКасну N — оставить N монет, остальное сжечь. Только админ."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    m = re.search(r"ОбрезатьКасну\s+(\d+)", msg.text or "", re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>ОбрезатьКасну 5000</code> — оставить 5000, "
            "остальное сжечь.\n\n"
            "⚠️ Необратимо.",
            parse_mode=ParseMode.HTML,
        )
        return
    keep = int(m.group(1))
    if keep < 0:
        await msg.reply_text("⛔ Не может быть отрицательным.")
        return

    burned = await db.treasury_trim(chat.id, keep)
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🔥 <b>Обрезка казны</b>\n"
        f"Сожжено: <b>{burned}</b>\n"
        f"Осталось: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


# ---------- Зарплата (инфо) ----------

async def cmd_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    day = _today_utc()
    coins = await db.get_salary_today(user.id, day)
    frac = await db.get_salary_frac(user.id, day)
    await msg.reply_text(
        f"💼 <b>Зарплата за сегодня</b>\n"
        f"Начислено: <b>{coins}.{frac:02d}</b> монет\n\n"
        f"<i>1 сообщение ≥ 2 символов = 0.20 монеты. "
        f"Дроби копятся, в баланс уходят целыми.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- -Утил / +Утил ----------

async def cmd_burn_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Утил N казна — сжечь N монет из казны. Только админ."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"[-–−]Утил\s+(\d+)\s+казна", msg.text or "", re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 казна</code>\n"
            "<i>Сжигает монеты из казны (в никуда).</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _burn_treasury(update, context, int(m.group(1)))


async def _burn_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         amount: int) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    burned = await db.burn_treasury(chat.id, amount)
    if burned == 0:
        await msg.reply_text("⚠️ В казне нечего сжигать.")
        return
    new_bal = await db.treasury_get(chat.id)
    note = ""
    if burned < amount:
        note = f"\n<i>Сожжено меньше запрошенного: в казне было только {burned}.</i>"
    await msg.reply_text(
        f"🔥 <b>Казна</b>: сожжено <b>{burned}</b>\n"
        f"Осталось: <b>{new_bal}</b>{note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_burn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Утил N @user (или реплай) — сжечь N монет у юзера. Только админ."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"[-–−]Утил\s+(\d+)", text, re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 @user</code> или реплай + "
            "<code>-Утил 100</code>.\n"
            "<i>Сжигает монеты (в никуда).</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    clean = re.sub(r"[-–−]Утил\s+\d+", "", text, flags=re.IGNORECASE).strip()

    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username, «казна» или сделайте реплай.")
        return
    uid, name = target

    burned = await db.burn_balance(uid, amount)
    if burned == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего сжигать (баланс 0 или меньше).",
            parse_mode=ParseMode.HTML,
        )
        return

    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    note = ""
    if burned < amount:
        note = f"\n<i>Сожжено меньше запрошенного: было только {burned}.</i>"
    await msg.reply_text(
        f"🔥 {escape(str(name))}: <b>-{burned}</b>\n"
        f"Баланс: <b>{new_balance}</b>{note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Утил N казна — начислить N монет в казну (из воздуха). Только админ."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"\+Утил\s+(\d+)\s+казна", msg.text or "", re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>+Утил 100 казна</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    await db.treasury_add(chat.id, amount)
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"✨ <b>Казна</b>: +{amount}\n"
        f"Казна: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Утил N @user — начислить N монет (из воздуха). Только админ."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    text = msg.text or ""
    m = re.search(r"\+Утил\s+(\d+)", text, re.IGNORECASE)
    if not m:
        await msg.reply_text(
            "Формат: <code>+Утил 100 @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    clean = re.sub(r"\+Утил\s+\d+", "", text, flags=re.IGNORECASE)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось начислить (ошибка БД).")
        return
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"✨ {escape(str(name))}: <b>+{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


# ---------- Ежедневный налог ----------

async def tax_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: списывает налог только с активных, кладёт в казну,
    пишет отчёт в общий чат."""
    try:
        total = await db.tax_active_balances(
            TAX_MIN_BALANCE, TAX_PCT, days=TAX_ACTIVE_DAYS,
        )
    except Exception as e:
        logger.warning("tax_job: %s", e)
        return

    chat_id = TAX_REPORT_CHAT_ID

    if total > 0 and chat_id:
        try:
            await db.treasury_add(chat_id, total)
        except Exception as e:
            logger.warning("tax_job treasury: %s", e)

    new_treasury = await db.treasury_get(chat_id) if chat_id else 0

    if not chat_id:
        logger.warning("tax_job: CHAT_ID не задан, отчёт не отправлен")
        return

    if total <= 0:
        text = (
            f"🏦 <b>Налог дня</b>\n"
            f"Сегодня платить некому: у активных баланс ≤ {TAX_MIN_BALANCE}.\n"
            f"Казна: <b>{new_treasury}</b>"
        )
    else:
        text = (
            f"🏦 <b>Налог дня</b>\n"
            f"Ставка: <b>{TAX_PCT}%</b> с баланса > {TAX_MIN_BALANCE}\n"
            f"Платят только активные (писали за {TAX_ACTIVE_DAYS} дн.)\n"
            f"Собрано в казну: <b>{total}</b> монет\n"
            f"Казна: <b>{new_treasury}</b>\n\n"
            f"<i>Неактивные не платят. Вернулся — баланс на месте.</i>"
        )
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("tax_job send: %s", e)

    logger.info(
        "tax_job: списано %s, казна %s, чат %s",
        total, new_treasury, chat_id,
    )


# ---------- Ежедневные проценты по кредиту ----------

async def daily_interest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        affected = await db.apply_credit_interest(CREDIT_PERCENT_PER_DAY)
        logger.info("daily_interest_job: затронуто записей: %s", affected)
    except Exception as e:
        logger.warning("daily_interest_job: %s", e)


def register_jobs(app) -> None:
    if not app.job_queue:
        logger.warning("economy: job_queue нет, джобы не будут начисляться")
        return
    app.job_queue.run_daily(
        daily_interest_job,
        time=dtime(hour=0, minute=10, tzinfo=TZ),
    )
    app.job_queue.run_daily(
        tax_job,
        time=dtime(hour=0, minute=40, tzinfo=TZ),
    )
    logger.info(
        "economy: ежедневные проценты (UTC 00:10) и налог (UTC 00:40) запланированы"
    )
