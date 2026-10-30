"""Рынок коинов: выставить купленную монету, купить лот.

FIX #3 (монеты как активы):
  - комиссия с лота (3%) делится 50/50: казна чата / dividend_pool монеты;
  - circulating при продаже лота НЕ меняется (монета уже в обороте);
  - invested у покупателя лота НЕ растёт (вторичка, не эмиссия);
  - цена лота не может быть 0 (валидация при выставлении).

FIX #1: покупка лота с ценой 0 больше не ломается.
  - если price == 0, fee = 0, продавцу 0, но лот закрывается корректно.
"""
import os
import logging
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

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

MARKET_FEE_PCT = 3
MIN_LOT_PRICE = 1
MAX_LOT_PRICE = 1_000_000


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    lots = await db.coin_lots_list(chat.id, limit=20)
    if not lots:
        await msg.reply_text("Рынок пуст. Выстави свою монету: Рынок → Продать.")
        return
    rows = []
    for lot in lots:
        coin = await db.coin_get(lot["coin_id"])
        if not coin:
            continue
        rows.append([InlineKeyboardButton(
            f"{coin['name']} × {lot['quantity']} — {lot['price']}💰",
            callback_data=f"cmkt:buy:{lot['id']}",
        )])
    rows.append([InlineKeyboardButton("💰 Продать монету", callback_data="cmkt:sell")])
    await msg.reply_text(
        "📈 <b>Рынок коинов</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) < 2 or parts[0] != "cmkt":
        await q.answer()
        return
    action = parts[1]

    if action == "buy" and len(parts) == 3:
        lot_id = int(parts[2])
        await _buy_lot(q, user, lot_id)
        return

    if action == "sell":
        await _render_sell_menu(q, user)
        return

    if action == "sell_coin" and len(parts) == 3:
        coin_id = int(parts[2])
        _SELL_PENDING[user.id] = {"coin_id": coin_id, "chat_id": q.message.chat.id}
        await q.edit_message_text(
            "Введи цену за 1 монету (целое > 0).\n"
            "Количество для продажи — вся твоя доля.",
        )
        await q.answer()
        return

    await q.answer()


_SELL_PENDING: dict[int, dict] = {}


async def _render_sell_menu(q, user) -> None:
    chat_id = q.message.chat.id
    held = await db.coin_holders_by_user(chat_id, user.id)
    if not held:
        await q.answer("У тебя нет купленных монет.", show_alert=True)
        return
    rows = []
    for h in held:
        if int(h["quantity"]) <= 0:
            continue
        coin = await db.coin_get(h["coin_id"])
        if not coin:
            continue
        rows.append([InlineKeyboardButton(
            f"{coin['name']} × {h['quantity']}",
            callback_data=f"cmkt:sell_coin:{coin['id']}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cmkt:back")])
    await q.edit_message_text(
        "Какую монету продать?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await q.answer()


async def sell_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ловит цену лота от юзера. Возвращает True, если обработали."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return False
    st = _SELL_PENDING.get(user.id)
    if not st:
        return False
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.reply_text(f"⛔ Цена: целое от {MIN_LOT_PRICE} до {MAX_LOT_PRICE}.")
        return True
    price = int(text)
    if price < MIN_LOT_PRICE or price > MAX_LOT_PRICE:
        await msg.reply_text(f"⛔ Цена: целое от {MIN_LOT_PRICE} до {MAX_LOT_PRICE}.")
        return True

    held = await db.coin_holder_get(st["coin_id"], user.id)
    qty = int((held or {}).get("quantity") or 0)
    if qty <= 0:
        await msg.reply_text("⛔ У тебя нет этой монеты.")
        _SELL_PENDING.pop(user.id, None)
        return True

    ok = await db.coin_market_lot_create(st["coin_id"], user.id, qty, price)
    _SELL_PENDING.pop(user.id, None)
    if not ok:
        await msg.reply_text("⚠️ Не удалось выставить лот.")
        return True
    await msg.reply_text(
        f"✅ Лот выставлен: {price}💰 × {qty} шт."
    )
    return True


async def _buy_lot(q, user, lot_id: int) -> None:
    lot = await db.coin_market_lot_get(lot_id)
    if not lot or not lot["active"]:
        await q.answer("Лот недоступен.", show_alert=True)
        return
    if lot["seller_id"] == user.id:
        await q.answer("Свой лот купить нельзя.", show_alert=True)
        return

    price = int(lot["price"])
    qty = int(lot["quantity"])
    coin = await db.coin_get(lot["coin_id"])
    if not coin:
        await q.answer("Монета не найдена.", show_alert=True)
        return

    row = await db.get_user(user.id)
    bal = (row or {}).get("balance") or 0

    # FIX #1: если price == 0 — списывать нечего, покупка разрешена
    if price > 0:
        if bal < price:
            await q.answer(f"⛔ Нужно {price}, у тебя {bal}.", show_alert=True)
            return
        ok = await db.take_balance_atomic(user.id, price)
        if not ok:
            await q.answer("⛔ Не удалось списать.", show_alert=True)
            return

    # FIX #3: комиссия 3% делится 50/50: казна / dividend_pool монеты
    if price <= 1:
        fee = 0
        to_seller = price
    else:
        fee = max(1, price * MARKET_FEE_PCT // 100)
        to_seller = price - fee
        if to_seller < 0:
            to_seller = 0

    treasury_part = fee // 2
    dividend_part = fee - treasury_part

    if to_seller > 0:
        try:
            await db.add_balance(lot["seller_id"], to_seller)
        except Exception as e:
            logger.warning("market seller payout: %s", e)

    if treasury_part > 0:
        try:
            await db.treasury_add(q.message.chat.id, treasury_part)
        except Exception as e:
            logger.warning("market treasury: %s", e)

    if dividend_part > 0:
        try:
            await db.coin_dividend_add(lot["coin_id"], dividend_part)
        except Exception as e:
            logger.warning("market dividend: %s", e)

    ok2 = await db.coin_market_buy_atomic(
        lot_id, user.id, qty, price,
    )
    if not ok2:
        # откат
        if price > 0:
            await db.add_balance(user.id, price)
        if to_seller > 0:
            await db.add_balance(lot["seller_id"], -to_seller)
        if treasury_part > 0:
            await db.treasury_add(q.message.chat.id, -treasury_part)
        if dividend_part > 0:
            await db.coin_dividend_add(lot["coin_id"], -dividend_part)
        await q.answer("⚠️ Не удалось купить лот, монеты возвращены.", show_alert=True)
        return

    await q.answer(f"✅ Куплено {qty} шт за {price}💰")
