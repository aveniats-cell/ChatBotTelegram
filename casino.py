"""Мини-казино на монеты с выплатами из казны."""
import asyncio
import logging
import re
import secrets
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

_RE_COIN = re.compile(r"(?i)^казино\s+монетка\s+(орёл|орел|решка)\s+(\d+)\s*$")
_RE_DICE = re.compile(r"(?i)^казино\s+кубик\s+([1-6])\s+(\d+)\s*$")
_RE_SLOTS = re.compile(r"(?i)^казино\s+слоты\s+(\d+)\s*$")

_CASINO_OWNERS: dict[tuple[int, int], int] = {}
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[chat_id] = lock
    return lock


async def _ensure_user(user) -> None:
    try:
        if not await db.get_user(user.id):
            await db.upsert_user(user.id, user.username, user.first_name)
    except Exception:
        logger.exception("casino ensure user")


def _main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 Монетка", callback_data="cas:game:coin"),
            InlineKeyboardButton("🎲 Кубик", callback_data="cas:game:dice"),
        ],
        [InlineKeyboardButton("🎰 Слоты", callback_data="cas:game:slots")],
        [InlineKeyboardButton("📖 Правила и выплаты", callback_data="cas:help")],
        [InlineKeyboardButton("← Главное меню", callback_data="home:back")],
    ])


async def open_casino_from_callback(query, user) -> None:
    """Открывает игровой стол из главной панели."""
    if not query or not query.message or not user:
        return
    await _ensure_user(user)
    key = (query.message.chat.id, query.message.message_id)
    _CASINO_OWNERS[key] = user.id
    try:
        await query.edit_message_text(
            "<b>Мини-казино</b>\n\n"
            "Выбери игру. Ставка списывается только после проверки баланса.\n"
            f"Диапазон ставки: <b>{MIN_BET}–{MAX_BET}</b> монет.",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            logger.exception("Не удалось открыть казино из меню")
            raise


async def cmd_casino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    await _ensure_user(user)
    text = (msg.text or "").strip()

    if text.casefold() in ("казино", "/casino"):
        sent = await msg.reply_text(
            "🎰 <b>МИНИ-КАЗИНО</b>\n\n"
            "Добро пожаловать за игровой стол!\n\n"
            "🪙 <b>Монетка</b> — угадай сторону.\n"
            "🎲 <b>Кубик</b> — угадай число от 1 до 6.\n"
            "🎰 <b>Слоты</b> — собери комбинацию.\n\n"
            f"💰 Ставка: {MIN_BET}–{MAX_BET} монет.\n\n"
            "Выбирай игру:",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )
        _CASINO_OWNERS[(sent.chat_id, sent.message_id)] = user.id
        return

    m = _RE_COIN.match(text)
    if m:
        side = m.group(1).casefold().replace("ё", "е")
        await _play_coin(user, msg, side, int(m.group(2)))
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
        "🎰 Напиши <b>Казино</b>, чтобы открыть меню.\n\n"
        "Или используй:\n<code>Казино монетка орёл 50</code>\n"
        "<code>Казино кубик 4 50</code>\n<code>Казино слоты 50</code>",
        parse_mode=ParseMode.HTML,
    )


async def casino_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q, user = update.callback_query, update.effective_user
    if not q or not user or not q.message:
        return
    key = (q.message.chat.id, q.message.message_id)
    owner = _CASINO_OWNERS.get(key)
    if owner is None or owner != user.id:
        await q.answer("Это не твоё казино. Открой своё командой «Казино».", show_alert=True)
        return

    data = (q.data or "").split(":")
    if data == ["cas", "help"]:
        await q.answer(
            "Монетка: шанс 50%, выплата x1.92.\n"
            "Кубик: шанс 1 из 6, выплата x5.5.\n"
            "Слоты: три одинаковых x13, пара x1.4.\n"
            "Ставки: от 5 до 100 монет.",
            show_alert=True,
        )
    elif len(data) == 3 and data[:2] == ["cas", "game"]:
        if data[2] == "coin":
            await _show_coin(q)
        elif data[2] == "dice":
            await _show_dice(q)
        elif data[2] == "slots":
            await _show_slots(q)
        else:
            await q.answer("Неизвестная игра.", show_alert=True)
    elif len(data) == 3 and data[:2] == ["cas", "dice"]:
        try:
            number = int(data[2])
        except ValueError:
            await q.answer("Неверное число.", show_alert=True)
            return
        if not 1 <= number <= 6:
            await q.answer("Число должно быть от 1 до 6.", show_alert=True)
            return
        await _show_dice_bets(q, number)
    elif len(data) == 5 and data[:2] == ["cas", "play"]:
        game, choice = data[2], data[3]
        try:
            bet = int(data[4])
        except ValueError:
            await q.answer("Некорректная ставка.", show_alert=True)
            return
        if bet not in BET_OPTIONS:
            await q.answer("Недопустимая ставка.", show_alert=True)
            return
        if game == "coin" and choice in ("orel", "reshka"):
            await q.answer("Игра началась!")
            await _play_coin(user, q.message, "орел" if choice == "orel" else "решка", bet)
        elif game == "dice":
            try:
                number = int(choice)
            except ValueError:
                await q.answer("Неверное число.", show_alert=True)
                return
            if number not in range(1, 7):
                await q.answer("Число должно быть от 1 до 6.", show_alert=True)
                return
            await q.answer("Игра началась!")
            await _play_dice(user, q.message, number, bet)
        elif game == "slots":
            await q.answer("Барабаны вращаются!")
            await _play_slots(user, q.message, bet)
        else:
            await q.answer("Неизвестная игра.", show_alert=True)
    elif data == ["cas", "back"]:
        await _edit_menu(q, user)
    else:
        await q.answer()


async def _edit_menu(q, user):
    try:
        await q.edit_message_text(
            "🎰 <b>МИНИ-КАЗИНО</b>\n\n"
            "Выбери игру:\n\n"
            "🪙 Монетка — угадай сторону.\n"
            "🎲 Кубик — угадай число.\n"
            "🎰 Слоты — собери комбинацию.\n\n"
            f"💰 Ставка: {MIN_BET}–{MAX_BET} монет.",
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )
    except TelegramError:
        logger.exception("casino menu edit failed")
    _CASINO_OWNERS[(q.message.chat.id, q.message.message_id)] = user.id
    await q.answer()


def _bet_buttons(prefix: str):
    return [InlineKeyboardButton(f"{bet} 🪙", callback_data=f"{prefix}:{bet}") for bet in BET_OPTIONS]


async def _show_coin(q):
    rows = [
        [InlineKeyboardButton(f"{label} · {bet} 🪙", callback_data=f"cas:play:coin:{side}:{bet}")
         for bet in BET_OPTIONS]
        for side, label in (("orel", "🦅 Орёл"), ("reshka", "🌙 Решка"))
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cas:back")])
    try:
        await q.edit_message_text(
            "🪙 <b>МОНЕТКА</b>\n\nУгадай сторону монеты.\n"
            "🎯 Вероятность: 50%\n"
            f"🏆 Выплата: x{COIN_MULT}\n\nВыбери сторону и ставку:",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        logger.exception("casino coin menu edit failed")
    await q.answer()


async def _show_dice(q):
    rows = [
        [InlineKeyboardButton(f"🎲 {n}", callback_data=f"cas:dice:{n}") for n in (1, 2, 3)],
        [InlineKeyboardButton(f"🎲 {n}", callback_data=f"cas:dice:{n}") for n in (4, 5, 6)],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cas:back")],
    ]
    try:
        await q.edit_message_text(
            "🎲 <b>КУБИК</b>\n\nВыбери число от 1 до 6.\n"
            "🎯 Вероятность: 1 из 6\n"
            f"🏆 Выплата: x{DICE_MULT}",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        logger.exception("casino dice menu edit failed")
    await q.answer()


async def _show_dice_bets(q, number: int):
    rows = [
        _bet_buttons(f"cas:play:dice:{number}"),
        [InlineKeyboardButton("⬅️ К выбору числа", callback_data="cas:game:dice")],
    ]
    try:
        await q.edit_message_text(
            f"🎲 <b>СТАВКА НА ЧИСЛО {number}</b>\n\n"
            "Выбери размер ставки:\n"
            f"🏆 При попадании выплата x{DICE_MULT}.",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        logger.exception("casino dice bet menu edit failed")
    await q.answer()


async def _show_slots(q):
    rows = [
        _bet_buttons("cas:play:slots"),
        [InlineKeyboardButton("⬅️ Назад", callback_data="cas:back")],
    ]
    try:
        await q.edit_message_text(
            "🎰 <b>СЛОТЫ</b>\n\nКрути барабаны и лови комбинации!\n\n"
            "🍒 🍋 🍇 ⭐ 💎 7️⃣\n\n"
            "💎 Три одинаковых: x13\n"
            "✨ Одна пара: x1.4\n"
            "💨 Остальные комбинации: проигрыш.\n\nВыбери ставку:",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        logger.exception("casino slots menu edit failed")
    await q.answer()


async def _take_bet(user, msg, bet: int) -> bool:
    if bet < MIN_BET or bet > MAX_BET:
        await msg.reply_text(f"⛔ Ставка должна быть от {MIN_BET} до {MAX_BET} монет.")
        return False
    ok = await db.take_balance_atomic(user.id, bet)
    if not ok:
        row = await db.get_user(user.id)
        balance = int((row or {}).get("balance") or 0)
        await msg.reply_text(f"⛔ Недостаточно монет.\nТвой баланс: <b>{balance}</b> 🪙", parse_mode=ParseMode.HTML)
        return False
    return True


async def _refund_bet(user, bet: int) -> bool:
    try:
        return bool(await db.add_balance(user.id, bet))
    except Exception:
        logger.exception("casino bet refund failed: user=%s bet=%s", user.id, bet)
        return False


async def _finish(user, msg, chat_id: int, bet: int, win: int, detail: str) -> None:
    """Ставит ставку в казну и выплачивает выигрыш только из неё."""
    async with _get_lock(chat_id):
        try:
            deposited = await db.treasury_add(chat_id, bet)
        except Exception:
            logger.exception("casino treasury deposit failed")
            deposited = False
        if not deposited:
            refunded = await _refund_bet(user, bet)
            await msg.reply_text(
                "⚠️ Казино временно недоступно.\n" +
                ("Ставка возвращена." if refunded else "Не удалось вернуть ставку. Обратись к администратору.")
            )
            return

        if win > 0:
            try:
                paid = await db.treasury_withdraw(chat_id, win)
            except Exception:
                logger.exception("casino treasury payout reserve failed")
                paid = False
            if not paid:
                # Ставка уже внесена в казну. Возвращаем её из казны перед возвратом игроку.
                try:
                    rolled_back = await db.treasury_withdraw(chat_id, bet)
                except Exception:
                    logger.exception("casino treasury rollback failed")
                    rolled_back = False
                if not rolled_back:
                    logger.critical("Casino rollback failed: chat=%s bet=%s user=%s", chat_id, bet, user.id)
                    await msg.reply_text("⚠️ Ошибка казны. Не запускай повторную игру и обратись к администратору.")
                    return
                refunded = await _refund_bet(user, bet)
                await msg.reply_text(
                    "🏦 <b>В казне недостаточно средств.</b>\n\nРаунд отменён.\n" +
                    ("Ставка возвращена." if refunded else "Не удалось вернуть ставку. Обратись к администратору."),
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                credited = await db.add_balance(user.id, win)
            except Exception:
                logger.exception("casino player payout credit failed")
                credited = False
            if not credited:
                try:
                    await db.treasury_add(chat_id, win)
                except Exception:
                    logger.exception("casino treasury payout rollback failed")
                refunded = await _refund_bet(user, bet)
                await msg.reply_text(
                    "⚠️ Не удалось начислить выигрыш.\n" +
                    ("Ставка возвращена." if refunded else "Обратись к администратору."),
                )
                return

    row = await db.get_user(user.id)
    balance = int((row or {}).get("balance") or 0)
    if win > bet:
        sign, title = "🏆", "ПОБЕДА!"
    elif win == bet:
        sign, title = "🤝", "СТАВКА ВЕРНУЛАСЬ"
    elif win > 0:
        sign, title = "✨", "ЕСТЬ ВЫИГРЫШ"
    else:
        sign, title = "💸", "НЕ ПОВЕЗЛО"
    net = win - bet
    net_text = f"+{net}" if net > 0 else str(net)
    await msg.reply_text(
        f"{sign} <b>{title}</b>\n\n{detail}\n\n"
        f"🎯 Ставка: <b>{bet}</b> 🪙\n"
        f"💰 Выплата: <b>{win}</b> 🪙\n"
        f"📊 Результат: <b>{net_text}</b> 🪙\n"
        f"👛 Баланс: <b>{balance}</b> 🪙",
        parse_mode=ParseMode.HTML,
    )


async def _play_coin(user, msg, side: str, bet: int):
    side = side.casefold().replace("ё", "е")
    if side not in ("орел", "решка"):
        await msg.reply_text("⛔ Выбери орёл или решку.")
        return
    if not await _take_bet(user, msg, bet):
        return
    result = secrets.choice(["орёл", "решка"])
    if side == result.replace("ё", "е"):
        await _finish(user, msg, msg.chat.id, bet, int(bet * COIN_MULT),
                      f"🪙 Выпало: <b>{result}</b>\nТы угадал! Множитель x{COIN_MULT}.")
    else:
        await _finish(user, msg, msg.chat.id, bet, 0,
                      f"🪙 Выпало: <b>{result}</b>\nТы выбрал: {escape(side)}.\nВ этот раз не повезло.")


async def _play_dice(user, msg, number: int, bet: int):
    if not 1 <= number <= 6:
        await msg.reply_text("⛔ Число должно быть от 1 до 6.")
        return
    if not await _take_bet(user, msg, bet):
        return
    roll = secrets.randbelow(6) + 1
    if roll == number:
        await _finish(user, msg, msg.chat.id, bet, int(bet * DICE_MULT),
                      f"🎲 Выпало: <b>{roll}</b>\nТочное попадание! Множитель x{DICE_MULT}.")
    else:
        await _finish(user, msg, msg.chat.id, bet, 0,
                      f"🎲 Выпало: <b>{roll}</b>\nТы ставил на число {number}.\nКубик оказался сильнее!")


async def _play_slots(user, msg, bet: int):
    if not await _take_bet(user, msg, bet):
        return
    a, b, c = (secrets.choice(_SLOT) for _ in range(3))
    line = f"{a}  {b}  {c}"
    if a == b == c:
        win = int(bet * SLOT_TRIPLE_MULT)
        detail = f"🎰 <b>{line}</b>\n\n💎 ДЖЕКПОТ! Три одинаковых!\nМножитель x{SLOT_TRIPLE_MULT}."
    elif a == b or b == c or a == c:
        win = int(bet * SLOT_PAIR_MULT)
        detail = f"🎰 <b>{line}</b>\n\n✨ ПАРА! Неплохой результат!\nМножитель x{SLOT_PAIR_MULT}."
    else:
        win = 0
        detail = f"🎰 <b>{line}</b>\n\n💨 Комбинации нет. Попробуй ещё раз!"
    await _finish(user, msg, msg.chat.id, bet, win, detail)
