"""Магазин пакетов команд.

Меню приватное: кнопки работают только у того, кто вызвал «Магазин».
Чужие нажатия → «Это не твоё меню».
"""
import logging
from html import escape

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

_MENU_OWNERS: dict[tuple[int, int], int] = {}


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or not msg or not user:
        return
    pockets = await db.shop_pockets_list(chat.id)
    if not pockets:
        await msg.reply_text("Магазин пока пуст.")
        return

    rows = []
    for p in pockets:
        if not p["active"]:
            continue
        rows.append([InlineKeyboardButton(
            f"{p['name']} — {p['price']}🪙",
            callback_data=f"shop:view:{p['id']}",
        )])
    sent = await msg.reply_text(
        "🛒 <b>Магазин</b>\nВыбери пакет:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    _MENU_OWNERS[(sent.chat_id, sent.message_id)] = user.id


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) < 2 or parts[0] != "shop":
        await q.answer()
        return

    owner = _MENU_OWNERS.get((q.message.chat.id, q.message.message_id))
    if owner is not None and owner != user.id:
        await q.answer("Это не твоё меню. Вызови «Магазин» сам.", show_alert=True)
        return

    action = parts[1]
    if action == "main":
        await _render_main(q, user)
        return
    if action == "view" and len(parts) == 3:
        await _view_pocket(q, user, int(parts[2]))
        return
    if action == "buy" and len(parts) == 3:
        await _buy_pocket(q, user, int(parts[2]))
        return

    await q.answer()


async def _render_main(q, user) -> None:
    pockets = await db.shop_pockets_list(q.message.chat.id)
    if not pockets:
        await q.edit_message_text("Магазин пока пуст.")
        await q.answer()
        return
    rows = []
    for p in pockets:
        if not p["active"]:
            continue
        rows.append([InlineKeyboardButton(
            f"{p['name']} — {p['price']}🪙",
            callback_data=f"shop:view:{p['id']}",
        )])
    try:
        await q.edit_message_text(
            "🛒 <b>Магазин</b>\nВыбери пакет:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass
    _MENU_OWNERS[(q.message.chat.id, q.message.message_id)] = user.id
    await q.answer()


async def _view_pocket(q, user, pocket_id: int) -> None:
    pocket = await db.shop_pocket_get(pocket_id)
    if not pocket or not pocket["active"]:
        await q.answer("Пакет недоступен.", show_alert=True)
        return
    cmds = await db.shop_commands_by_pocket(pocket_id)
    chat_id = q.message.chat.id
    has = await db.shop_user_has_pocket(chat_id, user.id, pocket_id)

    lines = [
        f"🛒 <b>{escape(pocket['name'])}</b>",
        f"Цена: <b>{pocket['price']}</b>🪙",
        f"Команд в пакете: <b>{len(cmds)}</b>",
    ]
    if cmds:
        lines.append("\n<b>Команды:</b>")
        for c in cmds[:30]:
            lines.append(f"• {escape(c['trigger'])}")
        if len(cmds) > 30:
            lines.append(f"… и ещё {len(cmds) - 30}")

    if has:
        lines.append("\n✅ <i>У тебя уже куплен этот пакет.</i>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="shop:main")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"💳 Купить за {pocket['price']}🪙",
                callback_data=f"shop:buy:{pocket_id}",
            )],
            [InlineKeyboardButton("⬅️ Назад", callback_data="shop:main")],
        ])
    try:
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except TelegramError:
        pass
    await q.answer()


async def _buy_pocket(q, user, pocket_id: int) -> None:
    chat = q.message.chat
    pocket = await db.shop_pocket_get(pocket_id)
    if not pocket or not pocket["active"]:
        await q.answer("Пакет недоступен.", show_alert=True)
        return
    has = await db.shop_user_has_pocket(chat.id, user.id, pocket_id)
    if has:
        await q.answer("У тебя уже есть этот пакет.", show_alert=True)
        return

    price = int(pocket["price"])
    row = await db.get_user(user.id)
    bal = (row or {}).get("balance") or 0
    if bal < price:
        await q.answer(
            f"⛔ Не хватает монет: нужно {price}, у тебя {bal}.\n"
            f"Возьми кредит (+Кредит 50) или поднакопи.",
            show_alert=True,
        )
        return

    ok = await db.take_balance_atomic(user.id, price)
    if not ok:
        await q.answer("⛔ Не удалось списать.", show_alert=True)
        return

    ok2 = await db.shop_purchase_add(chat.id, user.id, pocket_id, price)
    if not ok2:
        await db.add_balance(user.id, price)
        await q.answer("⚠️ Не удалось выдать пакет, монеты возвращены.", show_alert=True)
        return

    try:
        await db.treasury_add(chat.id, price)
    except Exception as e:
        logger.warning("shop treasury: %s", e)

    await q.answer(f"✅ Куплено: {pocket['name']} за {price}🪙", show_alert=True)
    await _view_pocket(q, user, pocket_id)
