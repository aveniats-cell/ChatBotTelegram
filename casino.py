"""Мини-казино на монеты с положительным house edge.

Выплаты:
- Монетка: x1.92 (edge 4%)
- Кубик: x5.5 (edge 8.3%)
- Слоты: тройка x13, пара x1.4 (edge 5.6%)

Ставка списывается атомарно. Проигрыш уходит в казну чата (treasury).
"""
import re
import secrets
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

MIN_BET = 1
MAX_BET = 100

COIN_MULT = 1.92
DICE_MULT = 5.5
SLOT_TRIPLE_MULT = 13
SLOT_PAIR_MULT = 1.4

_RE_COIN = re.compile(r"(?i)^казино\s+монетка\s+(\d+)\s*$")
_RE_DICE = re.compile(r"(?i)^казино\s+кубик\s+([1-6])\s+(\d+)\s*$")
_RE_SLOTS = re.compile(r"(?i)^казино\s+слоты\s+(\d+)\s*$")

_SLOT = ["🍒", "🍋", "🍇", "⭐", "💎", "7️⃣"]


async def _ensure_user(u) -> None:
    try:
        row = await db.get_user(u.id)
        if not row:
            await db.upsert_user(u.id, u.username, u.first_name)
    except Exception as e:
        logger.warning("casino _ensure_user: %s", e)


async def cmd_casino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    text = (msg.text or "").strip()
    await _ensure_user(user)

    low = text.casefold()
    if low == "казино" or low == "/casino":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🪙 Монетка (x1.92)", callback_data="cas:help:coin")],
            [InlineKeyboardButton("🎲 Кубик (x5.5)", callback_data="cas:help:dice")],
            [InlineKeyboardButton("🎰 Слоты (x13/x1.4)", callback_data="cas:help:slots")],
        ])
        await msg.reply_text(
            "🎰 <b>Казино</b>\n"
            f"Ставка от {MIN_BET} до {MAX_BET} монет.\n\n"
            "Форматы:\n"
            "<code>Казино монетка 50</code>\n"
            "<code>Казино кубик 4 50</code> — ставка на число 1–6\n"
            "<code>Казино слоты 50</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return

    m = _RE_COIN.match(text)
    if m:
        await _play_coin(user, msg, int(m.group(1)))
        return
    m = _RE_DICE.match(text)
    if m:
        await _play_dice(user, msg, int(m.group(1)), int(m.group(2)))
        return
    m = _RE_SLOTS.match(text)
    if m:
        await _play_slots(user, msg, int(m.group(1)))
        return

    await msg.reply_text(
        "Формат: <code>Казино монетка 50</code> / "
        "<code>Казино кубик 4 50</code> / <code>Казино слоты 50</code>.",
        parse_mode=ParseMode.HTML,
    )


async def casino_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "cas" or parts[1] != "help":
        await q.answer()
        return
    kind = parts[2]
    if kind == "coin":
        text = "🪙 <code>Казино монетка 50</code> — 50/50, выигрыш x1.92."
    elif kind == "dice":
        text = "🎲 <code>Казино кубик 4 50</code> — ставка на число 1–6, выигрыш x5.5."
    elif kind == "slots":
        text = "🎰 <code>Казино слоты 50</code> — три одинаковых x13, пара x1.4."
    else:
        await q.answer()
        return
    await q.answer(text, show_alert=True)


async def _take_bet(user, msg, bet: int) -> bool:
    if bet < MIN_BET or bet > MAX_BET:
        await msg.reply_text(f"⛔ Ставка: от {MIN_BET} до {MAX_BET}.")
        return False
    if not await db.take_balance_atomic(user.id, bet):
        row = await db.get_user(user.id)
        bal = (row or {}).get("balance") or 0
        await msg.reply_text(
            f"⛔ Недостаточно монет. Баланс: <b>{bal}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return False
    return True


async def _finish(user, msg, chat_id: int, bet: int, win: int, detail: str) -> None:
    """Начисляет выигрыш, а не возвращённую ставку кладёт в казну."""
    if win > 0:
        await db.add_balance(user.id, win)
    profit_for_house = bet - win
    if profit_for_house > 0:
        try:
            await db.treasury_add(chat_id, profit_for_house)
        except Exception as e:
            logger.warning("casino treasury_add: %s", e)

    row = await db.get_user(user.id)
    bal = (row or {}).get("balance") or 0
    if win > bet:
        sign = "🏆"
    elif win == bet:
        sign = "🤝"
    elif win > 0:
        sign = "🪙"
    else:
        sign = "💸"
    await msg.reply_text(
        f"{sign} <b>Казино</b>\n{detail}\n"
        f"Ставка: <b>{bet}</b> | Выигрыш: <b>{win}</b>\n"
        f"Баланс: <b>{bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _play_coin(user, msg, bet: int) -> None:
    if not await _take_bet(user, msg, bet):
        return
    result = secrets.choice(["орёл", "решка"])
    if result == "орёл":
        win = int(bet * COIN_MULT)
        await _finish(user, msg, msg.chat_id, bet, win,
                      f"Монетка: <b>{result}</b>. Ты угадал!")
    else:
        await _finish(user, msg, msg.chat_id, bet, 0,
                      f"Монетка: <b>{result}</b>. Не угадал.")


async def _play_dice(user, msg, number: int, bet: int) -> None:
    if number < 1 or number > 6:
        await msg.reply_text("⛔ Число: от 1 до 6.")
        return
    if not await _take_bet(user, msg, bet):
        return
    roll = secrets.randbelow(6) + 1
    if roll == number:
        win = int(bet * DICE_MULT)
        await _finish(user, msg, msg.chat_id, bet, win,
                      f"Кубик: <b>{roll}</b>. Точное попадание! x{DICE_MULT}")
    else:
        await _finish(user, msg, msg.chat_id, bet, 0,
                      f"Кубик: <b>{roll}</b>. Мимо (ставил на {number}).")


async def _play_slots(user, msg, bet: int) -> None:
    if not await _take_bet(user, msg, bet):
        return
    a, b, c = (secrets.choice(_SLOT) for _ in range(3))
    line = f"{a} {b} {c}"
    if a == b == c:
        win = int(bet * SLOT_TRIPLE_MULT)
        await _finish(user, msg, msg.chat_id, bet, win,
                      f"Слоты: {line}. ДЖЕКПОТ! x{SLOT_TRIPLE_MULT}")
    elif a == b or b == c or a == c:
        win = int(bet * SLOT_PAIR_MULT)
        await _finish(user, msg, msg.chat_id, bet, win,
                      f"Слоты: {line}. Пара! x{SLOT_PAIR_MULT}")
    else:
        await _finish(user, msg, msg.chat_id, bet, 0,
                      f"Слоты: {line}. Пусто.")
