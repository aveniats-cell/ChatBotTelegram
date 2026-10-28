"""Экономика: кредит из казны, монеты, перевод, казна, налог, зарплата,
проценты в казну, дефолт (органический — процент, искусственный — всё),
топ богачи, раздача всем."""
import os
import re
import logging
from datetime import time as dtime, datetime, timezone, timedelta
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

CREDIT_DAILY_LIMIT = 100
CREDIT_PERCENT_PER_DAY = 10

TRANSFER_FEE_PCT = 3
TRANSFER_MIN = 5
TRANSFER_FEE_MIN = 1

TAX_PCT = 3
TAX_MIN_BALANCE = 50
TAX_ACTIVE_DAYS = 1
TAX_REPORT_CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_PCT = 40

FORCE_DEFAULT_CMD = "Defloult220099"

TZ = timezone.utc

_default_in_progress: set[int] = set()


def _today_utc() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _num_after(text: str, keyword: str) -> int | None:
    m = re.search(rf"(?i){re.escape(keyword)}\s+(\d+)", text)
    return int(m.group(1)) if m else None


# ---------- Кредит из казны ----------

async def cmd_credit_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    amount = _num_after(msg.text or "", "+Кредит")
    if amount is None:
        await msg.reply_text(
            "Формат: <code>+Кредит 50</code> (от 1 до 100).",
            parse_mode=ParseMode.HTML,
        )
        return
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
            "⛔ У вас отрицательный баланс — новый кредит взять нельзя.",
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
        await msg.reply_text("⛔ Вы уже брали кредит сегодня.")
        return

    treasury = await db.treasury_get(chat.id)
    if treasury < amount:
        await msg.reply_text(
            f"🏦 Банк пуст: в казне <b>{treasury}</b>, нужно <b>{amount}</b>.\n"
            f"Кредиты временно недоступны.",
            parse_mode=ParseMode.HTML,
        )
        return

    ok = await db.take_credit_from_treasury(chat.id, user.id, amount, _today_utc())
    if not ok:
        await msg.reply_text("⚠️ Не удалось выдать кредит. Попробуйте позже.")
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or amount
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"💳 Взят кредит из казны: <b>{amount}</b>\n"
        f"Долг: <b>{new_credit}</b>\n"
        f"Казна: <b>{new_treasury}</b>\n"
        f"Ставка: <b>{CREDIT_PERCENT_PER_DAY}%</b> в день (идёт в казну).",
        parse_mode=ParseMode.HTML,
    )
    await _check_default_after(context, chat.id)


async def cmd_credit_repay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    amount = _num_after(msg.text or "", "-Кредит")
    if amount is None:
        m = re.search(r"(?i)Кредит\s+(\d+)", msg.text or "")
        amount = int(m.group(1)) if m else None
    if amount is None:
        await msg.reply_text("Формат: <code>-Кредит 50</code>.", parse_mode=ParseMode.HTML)
        return
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
            f"⛔ Недостаточно монет. Баланс: <b>{balance}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    amount = min(amount, credit, balance)
    if amount <= 0:
        await msg.reply_text("⛔ Нечего погашать.")
        return

    ok = await db.repay_credit_to_treasury(chat.id, user.id, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось погасить. Попробуйте ещё раз.")
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or 0
    new_balance = (new_row or {}).get("balance") or 0
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"✅ Погашено <b>{amount}</b> в казну.\n"
        f"Остаток долга: <b>{new_credit}</b>.\n"
        f"Баланс: <b>{new_balance}</b>.\n"
        f"Казна: <b>{new_treasury}</b>.",
        parse_mode=ParseMode.HTML,
    )


# ---------- +Монеты / -Монеты ----------

async def cmd_admin_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"(?i)\+Монеты\s+(-?\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>+Монеты 100 @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount == 0:
        await msg.reply_text("⛔ Сумма не может быть нулевой.")
        return

    clean = re.sub(r"(?i)\+Монеты\s+-?\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось изменить баланс.")
        return

    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
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
    m = re.search(r"(?i)[-–−]Монеты\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Монеты 100 @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    clean = re.sub(r"(?i)[-–−]Монеты\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    taken = await db.take_balance_any(uid, amount)
    if taken == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего списывать.",
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
        note = f"\n<i>Списано меньше: было только {taken}.</i>"
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
            "Формат: <code>Перевод 100 @user</code> или реплай.\n"
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
        f"Сумма: <b>{amount}</b> · комиссия <b>{fee}</b>\n"
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
        f"Накоплено: <b>{bal}</b> монет",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)\+ВКассу\s+(\d+)", msg.text or "")
    if not m:
        await msg.reply_text("Формат: <code>+ВКассу 500</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    await db.treasury_add(chat.id, amount)
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🏦 В казну добавлено: <b>{amount}</b>\nКазна: <b>{new_treasury}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)ОбрезатьКасну\s+(\d+)", msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>ОбрезатьКасну 15000</code>",
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
        f"🔥 Сожжено: <b>{burned}</b>\nОсталось: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


# ---------- -Утил / +Утил ----------

async def cmd_burn_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)[-–−]Утил\s+(\d+)\s+казна", msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 казна</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    burned = await db.burn_treasury(chat.id, amount)
    if burned == 0:
        await msg.reply_text("⚠️ В казне нечего сжигать.")
        return
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🔥 Казна: сожжено <b>{burned}</b>\nОсталось: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )
    await _check_default_after(context, chat.id)


async def cmd_burn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    text = msg.text or ""
    m = re.search(r"(?i)[-–−]Утил\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 @user</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    clean = re.sub(r"(?i)[-–−]Утил\s+\d+", "", text).strip()
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    burned = await db.burn_balance(uid, amount)
    if burned == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего сжигать.",
            parse_mode=ParseMode.HTML,
        )
        return
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"🔥 {escape(str(name))}: <b>-{burned}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)\+Утил\s+(\d+)\s+казна", msg.text or "")
    if not m:
        await msg.reply_text("Формат: <code>+Утил 100 казна</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    await db.treasury_add(chat.id, amount)
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"✨ Казна: +{amount}\nКазна: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    text = msg.text or ""
    m = re.search(r"(?i)\+Утил\s+(\d+)", text)
    if not m:
        await msg.reply_text("Формат: <code>+Утил 100 @user</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    clean = re.sub(r"(?i)\+Утил\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось начислить.")
        return
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"✨ {escape(str(name))}: <b>+{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
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


# ---------- Топ богачи ----------

async def cmd_top_rich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    rows = await db.top_balances(20)
    if not rows:
        await msg.reply_text("Топ богачей пуст.")
        return
    lines = ["💰 <b>Топ богачи</b>"]
    for i, (uid, bal) in enumerate(rows, start=1):
        name = str(uid)
        try:
            c = await context.bot.get_chat(uid)
            name = c.first_name or c.username or str(uid)
        except TelegramError:
            pass
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(f"{medal} {escape(name)} — <b>{bal}</b>")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------- Сбор балансов ----------

async def _collect_balances(chat_id: int, pct: int) -> tuple[int, int]:
    if pct <= 0:
        return 0, 0
    if pct > 100:
        pct = 100

    rs = await db._exec(
        "SELECT COALESCE(SUM(balance), 0), COUNT(*) "
        "FROM users WHERE balance > 0",
    )
    if not rs.rows:
        return 0, 0
    total_before = rs.rows[0][0] or 0
    users_count = rs.rows[0][1] or 0
    if total_before <= 0:
        return 0, 0

    if pct == 100:
        rs2 = await db._exec(
            "UPDATE users SET balance = 0 WHERE balance > 0",
        )
    else:
        rs2 = await db._exec(
            f"""UPDATE users
                SET balance = balance - CAST(balance * {int(pct)} / 100 AS INTEGER)
                WHERE balance > 0""",
        )
    if getattr(rs2, "error", False):
        logger.error("_collect_balances: UPDATE failed")
        return 0, 0

    rs3 = await db._exec(
        "SELECT COALESCE(SUM(balance), 0) FROM users WHERE balance > 0",
    )
    total_after = (rs3.rows[0][0] if rs3.rows else 0) or 0
    real = total_before - total_after
    if real <= 0:
        logger.warning(
            "_collect_balances: расхождение before=%s after=%s",
            total_before, total_after,
        )
        return 0, users_count

    logger.info(
        "_collect_balances: списано %s у %s юзеров (pct=%s)",
        real, users_count, pct,
    )
    return real, users_count


# ---------- Дефолт ----------

async def _run_default(context, chat_id: int, force: bool = False) -> bool:
    if chat_id in _default_in_progress:
        logger.info("default: уже выполняется для chat=%s, skip", chat_id)
        return False
    _default_in_progress.add(chat_id)
    try:
        logger.info("default: start chat=%s force=%s", chat_id, force)

        if not force:
            last = await db.get_last_default(chat_id)
            logger.info("default: last=%s", last)
            if last:
                try:
                    last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    if (datetime.now(TZ) - last_dt) < timedelta(hours=DEFAULT_COOLDOWN_HOURS):
                        logger.info("default: cooldown, skip")
                        return False
                except Exception as e:
                    logger.warning("default: parse last: %s", e)

        pct = 100 if force else DEFAULT_PCT
        total, users_count = await _collect_balances(chat_id, pct)
        logger.info("default: collected=%s users=%s pct=%s", total, users_count, pct)
        if total <= 0:
            logger.info("default: собирать нечего")
            return False

        await db.treasury_add(chat_id, total)
        await db.set_last_default(chat_id, datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))

        if force:
            text = (
                "⚠️ <b>ВНИМАНИЕ — ДЕФОЛТ</b> ⚠️\n\n"
                "🏦 <b>ЭКСТРЕМАЛЬНЫЙ ДЕФОЛТ</b>\n"
                "Правительство объявило дефолт.\n"
                "Все балансы <b>обнулены</b>.\n"
                f"Затронуто юзеров: <b>{users_count}</b>\n"
                f"Конфисковано в казну: <b>{total}</b> монет."
            )
        else:
            text = (
                "⚠️ <b>ВНИМАНИЕ — ДЕФОЛТ</b> ⚠️\n\n"
                "🏦 <b>ДЕФОЛТ</b>\n"
                "Казна пуста. Объявлен дефолт.\n"
                f"Списано <b>{DEFAULT_PCT}%</b> с балансов "
                f"<b>{users_count}</b> юзеров.\n"
                f"Собрано в казну: <b>{total}</b> монет."
            )

        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("default send: %s", e)

        return True
    finally:
        _default_in_progress.discard(chat_id)


async def _check_default_after(context, chat_id: int) -> None:
    try:
        bal = await db.treasury_get(chat_id)
    except Exception as e:
        logger.warning("_check_default_after: %s", e)
        return
    logger.info("_check_default_after: bal=%s", bal)
    if bal > 0:
        return
    await _run_default(context, chat_id, force=False)


async def default_watchdog_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = int(os.getenv("CHAT_ID", "0") or "0")
    if not chat_id:
        return
    try:
        bal = await db.treasury_get(chat_id)
    except Exception as e:
        logger.warning("default_watchdog: %s", e)
        return
    if bal <= 0:
        await _run_default(context, chat_id, force=False)


# ---------- Искусственный дефолт ----------

async def cmd_force_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("cmd_force_default: не удалось удалить команду: %s", e)

    ok = await _run_default(context, chat.id, force=True)
    if not ok:
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Экстремальный дефолт не удался: собирать нечего "
                     "(у юзеров нулевые балансы) или ошибка БД.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


# ---------- /ALLMONEY — раздача всем ----------

async def cmd_all_money(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/ALLMONEY 100` — начисляет ВСЕМ юзерам в БД по 100 монет.

    Не списывается из казны (mint). Бот удаляет команду из чата.
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"(?i)^/?ALLMONEY\s+(\d+)\s*$", text.strip())
    if not m:
        await msg.reply_text(
            "Формат: <code>/ALLMONEY 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    if amount > 1_000_000:
        await msg.reply_text("⛔ Слишком большая сумма (макс. 1 000 000).")
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("cmd_all_money: не удалось удалить команду: %s", e)

    try:
        rs = await db._exec(
            "SELECT COUNT(*) FROM users",
        )
        total_users = (rs.rows[0][0] if rs.rows else 0) or 0
    except Exception as e:
        logger.warning("cmd_all_money count: %s", e)
        total_users = 0

    if total_users == 0:
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ В БД нет ни одного юзера — раздавать некому.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        rs2 = await db._exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ?",
            [amount],
        )
    except Exception as e:
        logger.exception("cmd_all_money update: %s", e)
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Не удалось начислить монеты. Ошибка БД.",
            parse_mode=ParseMode.HTML,
        )
        return

    if getattr(rs2, "error", False):
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Не удалось начислить монеты. Ошибка БД.",
            parse_mode=ParseMode.HTML,
        )
        return

    total_minted = amount * total_users
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "⚠️ <b>ВНИМАНИЕ — РАЗДАЧА</b> ⚠️\n\n"
                "💰 <b>ВСЕМ НАЧИСЛЕНО</b>\n"
                f"По <b>{amount}</b> монет каждому юзеру в БД.\n"
                f"Затронуто: <b>{total_users}</b> юзеров.\n"
                f"Всего начислено: <b>{total_minted}</b> монет."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("cmd_all_money send: %s", e)


# ---------- Налог ----------

async def tax_job(context: ContextTypes.DEFAULT_TYPE) -> None:
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


# ---------- Проценты по кредиту ----------

async def daily_interest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = int(os.getenv("CHAT_ID", "0") or "0")
    if not chat_id:
        return
    try:
        affected = await db.apply_credit_interest_to_treasury(
            chat_id, CREDIT_PERCENT_PER_DAY,
        )
        logger.info("daily_interest_job: затронуто %s, казна пополнена", affected)
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
    app.job_queue.run_repeating(
        default_watchdog_job, interval=3600, first=60,
    )
    logger.info(
        "economy: проценты (00:10), налог (00:40), "
        "watchdog (раз в час), cooldown дефолта %sч, "
        "органический pct=%s%%, искусственный pct=100%% запланированы",
        DEFAULT_COOLDOWN_HOURS, DEFAULT_PCT,
    )
