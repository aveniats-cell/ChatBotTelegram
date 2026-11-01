
"""Мини-казино на монеты.

Игры:
- Монетка: x1.92, вероятность 50%, преимущество казино около 4%.
- Кубик: x5.5, вероятность 1/6, преимущество казино около 8.33%.
- Слоты: тройка x13, пара x1.4.

Экономика:
- Ставка атомарно списывается с пользователя.
- Ставка поступает в казну.
- Выигрыш выплачивается только из казны.
- Если казне не хватает денег, ставка возвращается.
- Казино не создаёт монеты из воздуха.
"""

import re
import secrets
import logging
import asyncio
from html import escape

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

MIN_BET = 5
MAX_BET = 100

COIN_MULT = 1.92
DICE_MULT = 5.5
SLOT_TRIPLE_MULT = 13
SLOT_PAIR_MULT = 1.4

BET_OPTIONS = [5, 10, 25, 50, 100]

_SLOT = ["🍒", "🍋", "🍇", "⭐", "💎", "7️⃣"]

_RE_COIN = re.compile(
    r"(?i)^казино\s+монетка\s+(орёл|орел|решка)\s+(\d+)\s*$"
)
_RE_DICE = re.compile(
    r"(?i)^казино\s+кубик\s+([1-6])\s+(\d+)\s*$"
)
_RE_SLOTS = re.compile(
    r"(?i)^казино\s+слоты\s+(\d+)\s*$"
)

# Защита меню от чужих нажатий.
_CASINO_OWNERS: dict[tuple[int, int], int] = {}

# Не даём двум играм одновременно менять казну в одном процессе.
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _CHAT_LOCKS:
        _CHAT_LOCKS[chat_id] = asyncio.Lock()
    return _CHAT_LOCKS[chat_id]


async def _ensure_user(user) -> None:
    try:
        row = await db.get_user(user.id)
        if not row:
            await db.upsert_user(
                user.id,
                user.username,
                user.first_name,
            )
    except Exception:
        logger.exception("casino ensure user")


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

def _main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🪙 Монетка",
                callback_data="cas:game:coin",
            ),
            InlineKeyboardButton(
                "🎲 Кубик",
                callback_data="cas:game:dice",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎰 Слоты",
                callback_data="cas:game:slots",
            ),
        ],
        [
            InlineKeyboardButton(
                "📖 Правила и выплаты",
                callback_data="cas:help",
            ),
        ],
    ])


async def cmd_casino(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg:
        return

    await _ensure_user(user)

    text = (msg.text or "").strip()

    # Вызов главного меню.
    if text.casefold() in ("казино", "/casino"):
        await msg.reply_text(
            "╔══════════════════════╗\n"
            "       🎰 <b>МИНИ-КАЗИНО</b>\n"
            "╚══════════════════════╝\n\n"
            "Добро пожаловать за игровой стол!\n\n"
            "🪙 <b>Монетка</b> — угадай сторону.\n"
            "🎲 <b>Кубик</b> — угадай число от 1 до 6.\n"
            "🎰 <b>Слоты</b> — собери комбинацию.\n\n"
            f"💰 Ставка: {MIN_BET}–{MAX_BET} монет.\n\n"
            "👇 <b>Выбирай игру:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )
        return

    # Старые текстовые команды.
    m = _RE_COIN.match(text)
    if m:
        side = m.group(1).casefold().replace("ё", "е")
        await _play_coin(user, msg, side, int(m.group(2)))
        return

    m = _RE_DICE.match(text)
    if m:
        await _play_dice(
            user,
            msg,
            int(m.group(1)),
            int(m.group(2)),
        )
        return

    m = _RE_SLOTS.match(text)
    if m:
        await _play_slots(user, msg, int(m.group(1)))
        return

    await msg.reply_text(
        "🎰 Напиши <b>Казино</b>, чтобы открыть меню.\n\n"
        "Или используй:\n"
        "<code>Казино монетка орёл 50</code>\n"
        "<code>Казино кубик 4 50</code>\n"
        "<code>Казино слоты 50</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# КНОПКИ
# ============================================================

async def casino_help_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    q = update.callback_query
    user = update.effective_user

    if not q or not user or not q.message:
        return

    data = (q.data or "").split(":")

    # Проверяем, что кнопки нажимает владелец меню.
    key = (q.message.chat.id, q.message.message_id)
    owner = _CASINO_OWNERS.get(key)

    if owner is not None and owner != user.id:
        await q.answer(
            "Это не твоё казино. Открой своё командой «Казино».",
            show_alert=True,
        )
        return

    if data == ["cas", "help"]:
        await q.answer(
            "Монетка: 50/50, выплата x1.92.\n"
            "Кубик: 1 шанс из 6, выплата x5.5.\n"
            "Слоты: тройка x13, пара x1.4.\n\n"
            "Ставки: от 5 до 100 монет.",
            show_alert=True,
        )
        return

    if len(data) == 3 and data[:2] == ["cas", "game"]:
        game = data[2]

        if game == "coin":
            await _show_coin(q, user)
            return

        if game == "dice":
            await _show_dice(q, user)
            return

        if game == "slots":
            await _show_slots(q, user)
            return

    if len(data) == 3 and data[:2] == ["cas", "dice"]:
        try:
            number = int(data[2])
        except ValueError:
            await q.answer("Неверное число.", show_alert=True)
            return

        if not 1 <= number <= 6:
            await q.answer("Число должно быть от 1 до 6.", show_alert=True)
            return

        await _show_dice_bets(q, user, number)
        return

    if len(data) == 5 and data[:2] == ["cas", "play"]:
        game = data[2]

        try:
            choice = data[3]
            bet = int(data[4])
        except ValueError:
            await q.answer("Некорректная ставка.", show_alert=True)
            return

        if bet not in BET_OPTIONS:
            await q.answer("Недопустимая ставка.", show_alert=True)
            return

        await q.answer("🎲 Игра началась!")

        if game == "coin" and choice in ("orel", "reshka"):
            side = "орел" if choice == "orel" else "решка"
            await _play_coin(user, q.message, side, bet)
            return

        if game == "dice":
            try:
                number = int(choice)
            except ValueError:
                return

            if 1 <= number <= 6:
                await _play_dice(user, q.message, number, bet)
            return

        if game == "slots":
            await _play_slots(user, q.message, bet)
            return

    if data == ["cas", "back"]:
        await _edit_menu(q, user)
        return

    await q.answer()


async def _edit_menu(q, user) -> None:
    try:
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "       🎰 <b>МИНИ-КАЗИНО</b>\n"
            "╚══════════════════════╝\n\n"
            "Выбери игру:\n\n"
            "🪙 Монетка — угадай сторону.\n"
            "🎲 Кубик — угадай число.\n"
            "🎰 Слоты — собери комбинацию.\n\n"
            f"💰 Ставка: {MIN_BET}–{MAX_BET} монет.",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )
    except TelegramError:
        pass

    _CASINO_OWNERS[(q.message.chat.id, q.message.message_id)] = user.id
    await q.answer()


def _bet_buttons(prefix: str):
    return [
        InlineKeyboardButton(
            f"{bet} 🪙",
            callback_data=f"{prefix}:{bet}",
        )
        for bet in BET_OPTIONS
    ]


async def _show_coin(q, user) -> None:
    rows = []

    for side, label in (
        ("orel", "🦅 Орёл"),
        ("reshka", "🌙 Решка"),
    ):
        rows.append([
            InlineKeyboardButton(
                f"{label} · {bet} 🪙",
                callback_data=f"cas:play:coin:{side}:{bet}",
            )
            for bet in BET_OPTIONS
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Назад",
            callback_data="cas:back",
        )
    ])

    try:
        await q.edit_message_text(
            "🪙 <b>МОНЕТКА</b>\n\n"
            "Угадай, какая сторона выпадет.\n\n"
            "🎯 Вероятность: 50%\n"
            f"🏆 Выплата: x{COIN_MULT}\n\n"
            "Выбери сторону и размер ставки:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    await q.answer()


async def _show_dice(q, user) -> None:
    rows = [
        [
            InlineKeyboardButton(
                f"🎲 {n}",
                callback_data=f"cas:dice:{n}",
            )
            for n in (1, 2, 3)
        ],
        [
            InlineKeyboardButton(
                f"🎲 {n}",
                callback_data=f"cas:dice:{n}",
            )
            for n in (4, 5, 6)
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="cas:back",
            )
        ],
    ]

    try:
        await q.edit_message_text(
            "🎲 <b>КУБИК</b>\n\n"
            "Выбери число от 1 до 6.\n\n"
            "🎯 Вероятность: 1 из 6\n"
            f"🏆 Выплата: x{DICE_MULT}\n\n"
            "Какое число выберешь?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    await q.answer()


async def _show_dice_bets(q, user, number: int) -> None:
    rows = [
        _bet_buttons(f"cas:play:dice:{number}"),
        [
            InlineKeyboardButton(
                "⬅️ К выбору числа",
                callback_data="cas:game:dice",
            )
        ],
    ]

    try:
        await q.edit_message_text(
            f"🎲 <b>ТВОЯ СТАВКА НА ЧИСЛО {number}</b>\n\n"
            "Выбери размер ставки:\n\n"
            f"🏆 При попадании выплата x{DICE_MULT}.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    await q.answer()


async def _show_slots(q, user) -> None:
    rows = [
        _bet_buttons("cas:play:slots"),
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="cas:back",
            )
        ],
    ]

    try:
        await q.edit_message_text(
            "🎰 <b>СЛОТЫ</b>\n\n"
            "Крути барабаны и лови комбинации!\n\n"
            "🍒 🍋 🍇 ⭐ 💎 7️⃣\n\n"
            "🏆 Три одинаковых: x13\n"
            "✨ Одна пара: x1.4\n"
            "💨 Остальные комбинации: проигрыш.\n\n"
            "Выбери ставку:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    await q.answer()


# ============================================================
# СТАВКА
# ============================================================

async def _take_bet(user, msg, bet: int) -> bool:
    if bet < MIN_BET or bet > MAX_BET:
        await msg.reply_text(
            f"⛔ Ставка должна быть от {MIN_BET} до {MAX_BET} монет."
        )
        return False

    ok = await db.take_balance_atomic(user.id, bet)

    if not ok:
        row = await db.get_user(user.id)
        balance = int((row or {}).get("balance") or 0)

        await msg.reply_text(
            f"⛔ Недостаточно монет.\n"
            f"Твой баланс: <b>{balance}</b> 🪙",
            parse_mode=ParseMode.HTML,
        )
        return False

    return True


# ============================================================
# ЗАВЕРШЕНИЕ ИГРЫ И ЗАЩИТА КАЗНЫ
# ============================================================

async def _finish(
    user,
    msg,
    chat_id: int,
    bet: int,
    win: int,
    detail: str,
) -> None:
    """Переводит ставку в казну и выплачивает выигрыш из казны."""

    lock = _get_lock(chat_id)

    async with lock:
        # Ставка поступает в казну.
        deposited = await db.treasury_add(chat_id, bet)

        if not deposited:
            # Не удалось зачислить ставку в казну — возвращаем её.
            refunded = await db.add_balance(user.id, bet)

            await msg.reply_text(
                "⚠️ Казино временно недоступно.\n"
                + (
                    "Ставка возвращена."
                    if refunded
                    else "Не удалось автоматически вернуть ставку. Обратись к админу."
                )
            )
            return

        if win > 0:
            # Проверяем, что казна действительно может оплатить выигрыш.
            paid_from_treasury = await db.treasury_withdraw(
                chat_id,
                win,
            )

            if not paid_from_treasury:
                # Откатываем поступление ставки и возвращаем её игроку.
                await db.treasury_withdraw(chat_id, bet)
                refunded = await db.add_balance(user.id, bet)

                await msg.reply_text(
                    "🏦 <b>В казне недостаточно средств для выплаты.</b>\n\n"
                    "Раунд отменён, ставка возвращена.\n"
                    + (
                        "Можешь попробовать позже."
                        if refunded
                        else "⚠️ Не удалось вернуть ставку. Обратись к админу."
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return

            # Начисляем пользователю уже зарезервированный выигрыш.
            credited = await db.add_balance(user.id, win)

            if not credited:
                # Если начисление не удалось, возвращаем деньги в казну.
                await db.treasury_add(chat_id, win)

                refunded = await db.add_balance(user.id, bet)

                await msg.reply_text(
                    "⚠️ Не удалось начислить выигрыш.\n"
                    + (
                        "Ставка возвращена."
                        if refunded
                        else "Обратись к администратору."
                    )
                )
                return

    row = await db.get_user(user.id)
    balance = int((row or {}).get("balance") or 0)

    if win > bet:
        sign = "🏆"
        result_title = "ПОБЕДА!"
    elif win == bet:
        sign = "🤝"
        result_title = "СТАВКА ВЕРНУЛАСЬ"
    elif win > 0:
        sign = "✨"
        result_title = "ЕСТЬ ВЫИГРЫШ"
    else:
        sign = "💸"
        result_title = "НЕ ПОВЕЗЛО"

    net = win - bet
    if net > 0:
        net_text = f"+{net}"
    else:
        net_text = str(net)

    await msg.reply_text(
        f"{sign} <b>{result_title}</b>\n\n"
        f"{detail}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Ставка: <b>{bet}</b> 🪙\n"
        f"💰 Выплата: <b>{win}</b> 🪙\n"
        f"📊 Результат: <b>{net_text}</b> 🪙\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👛 Баланс: <b>{balance}</b> 🪙",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ИГРА: МОНЕТКА
# ============================================================

async def _play_coin(user, msg, side: str, bet: int) -> None:
    side = side.casefold().replace("ё", "е")

    if side not in ("орел", "решка"):
        await msg.reply_text("⛔ Выбери орёл или решку.")
        return

    if not await _take_bet(user, msg, bet):
        return

    result = secrets.choice(["орёл", "решка"])
    normalized_result = result.replace("ё", "е")

    if side == normalized_result:
        win = int(bet * COIN_MULT)
        await _finish(
            user,
            msg,
            msg.chat.id,
            bet,
            win,
            f"🪙 Выпало: <b>{result}</b>\n"
            f"Ты угадал! Множитель x{COIN_MULT}.",
        )
    else:
        await _finish(
            user,
            msg,
            msg.chat.id,
            bet,
            0,
            f"🪙 Выпало: <b>{result}</b>\n"
            f"Ты выбрал: {escape(side)}.\n"
            "В этот раз не повезло.",
        )


# ============================================================
# ИГРА: КУБИК
# ============================================================

async def _play_dice(user, msg, number: int, bet: int) -> None:
    if number < 1 or number > 6:
        await msg.reply_text("⛔ Число должно быть от 1 до 6.")
        return

    if not await _take_bet(user, msg, bet):
        return

    roll = secrets.randbelow(6) + 1

    if roll == number:
        win = int(bet * DICE_MULT)
        await _finish(
            user,
            msg,
            msg.chat.id,
            bet,
            win,
            f"🎲 Выпало: <b>{roll}</b>\n"
            f"Точное попадание! Множитель x{DICE_MULT}.",
        )
    else:
        await _finish(
            user,
            msg,
            msg.chat.id,
            bet,
            0,
            f"🎲 Выпало: <b>{roll}</b>\n"
            f"Ты ставил на число {number}.\n"
            "Кубик оказался сильнее!",
        )


# ============================================================
# ИГРА: СЛОТЫ
# ============================================================

async def _play_slots(user, msg, bet: int) -> None:
    if not await _take_bet(user, msg, bet):
        return

    a, b, c = (
        secrets.choice(_SLOT)
        for _ in range(3)
    )

    line = f"{a}  {b}  {c}"

    if a == b == c:
        win = int(bet * SLOT_TRIPLE_MULT)
        detail = (
            f"🎰 <b>{line}</b>\n\n"
            f"💎 ДЖЕКПОТ! Три одинаковых!\n"
            f"Множитель x{SLOT_TRIPLE_MULT}."
        )

    elif a == b or b == c or a == c:
        win = int(bet * SLOT_PAIR_MULT)
        detail = (
            f"🎰 <b>{line}</b>\n\n"
            f"✨ ПАРА! Неплохой результат!\n"
            f"Множитель x{SLOT_PAIR_MULT}."
        )

    else:
        win = 0
        detail = (
            f"🎰 <b>{line}</b>\n\n"
            "💨 Комбинации нет. Попробуй ещё раз!"
        )

    await _finish(
        user,
        msg,
        msg.chat.id,
        bet,
        win,
        detail,
    )
