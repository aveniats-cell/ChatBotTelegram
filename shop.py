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

# Цена зафиксирована в монетах чата.
# Один запрос к ИИ = один токен.
AI_TOKEN_PACKS = {
    1: {"name": "Маленький", "tokens": 10, "price": 5},
    2: {"name": "Средний", "tokens": 30, "price": 12},
    3: {"name": "Большой", "tokens": 75, "price": 25},
    4: {"name": "Огромный", "tokens": 150, "price": 40},
}


async def cmd_shop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if not chat or not msg or not user:
        return

    pockets = await db.shop_pockets_list(chat.id)
    rows = []

    for p in pockets:
        if not p["active"]:
            continue

        rows.append([
            InlineKeyboardButton(
                f"{p['name']} — {p['price']}🪙",
                callback_data=f"shop:view:{p['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🧠 Токены ИИ",
            callback_data="shop:ai",
        )
    ])

    sent = await msg.reply_text(
        "🛒 <b>Магазин</b>\n"
        "Выбери пакет команд или токены ИИ:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )

    _MENU_OWNERS[(sent.chat_id, sent.message_id)] = user.id


async def shop_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    q = update.callback_query
    user = update.effective_user

    if not q or not user or not q.message:
        return

    parts = (q.data or "").split(":")

    if len(parts) < 2 or parts[0] != "shop":
        await q.answer()
        return

    message_key = (q.message.chat.id, q.message.message_id)
    owner = _MENU_OWNERS.get(message_key)

    if owner is None or owner != user.id:
        await q.answer(
            "Это не твоё меню. Вызови «Магазин» сам.",
            show_alert=True,
        )
        return

    action = parts[1]

    if action == "main":
        await _render_main(q, user)
        return

    if action == "ai":
        await _render_ai_tokens(q, user)
        return

    if action in ("ai_buy", "view", "buy") and len(parts) == 3:
        try:
            item_id = int(parts[2])
        except (TypeError, ValueError):
            await q.answer("Некорректный запрос.", show_alert=True)
            return

        if action == "ai_buy":
            await _buy_ai_tokens(q, user, item_id)
        elif action == "view":
            await _view_pocket(q, user, item_id)
        elif action == "buy":
            await _buy_pocket(q, user, item_id)

        return

    await q.answer()


async def _render_main(q, user) -> None:
    pockets = await db.shop_pockets_list(q.message.chat.id)
    rows = []

    for p in pockets:
        if not p["active"]:
            continue

        rows.append([
            InlineKeyboardButton(
                f"{p['name']} — {p['price']}🪙",
                callback_data=f"shop:view:{p['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🧠 Токены ИИ",
            callback_data="shop:ai",
        )
    ])

    try:
        await q.edit_message_text(
            "🛒 <b>Магазин</b>\n"
            "Выбери пакет команд или токены ИИ:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    _MENU_OWNERS[(q.message.chat.id, q.message.message_id)] = user.id
    await q.answer()


async def _render_ai_tokens(q, user, *, answer=True) -> None:
    tokens = await db.get_ai_tokens(user.id)
    rows = []

    for pack_id, pack in AI_TOKEN_PACKS.items():
        rows.append([
            InlineKeyboardButton(
                f"{pack['name']}: {pack['tokens']} токенов — "
                f"{pack['price']}🪙",
                callback_data=f"shop:ai_buy:{pack_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Назад в магазин",
            callback_data="shop:main",
        )
    ])

    try:
        await q.edit_message_text(
            "🧠 <b>Токены ИИ</b>\n"
            f"Твой баланс: <b>{tokens}</b> токенов\n\n"
            "Один запрос к ИИ стоит 1 токен.\n"
            "Токены не сгорают и сохраняются на балансе.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    if answer:
        await q.answer()


async def _buy_ai_tokens(q, user, pack_id: int) -> None:
    pack = AI_TOKEN_PACKS.get(pack_id)

    if not pack:
        await q.answer("Такого пакета нет.", show_alert=True)
        return

    row = await db.get_user(user.id)

    if not row:
        await q.answer(
            "Сначала напиши /start.",
            show_alert=True,
        )
        return

    ok = await db.buy_ai_tokens(
        user.id,
        pack["price"],
        pack["tokens"],
    )

    if not ok:
        current = await db.get_user(user.id)
        balance = int((current or {}).get("balance") or 0)

        await q.answer(
            f"Не хватает монет. Нужно {pack['price']}🪙, "
            f"у тебя {balance}🪙.",
            show_alert=True,
        )
        return

    new_tokens = await db.get_ai_tokens(user.id)

    await q.answer(
        f"Куплено {pack['tokens']} токенов! "
        f"Баланс: {new_tokens}.",
        show_alert=True,
    )

    # Не отвечаем на callback второй раз.
    await _render_ai_tokens(q, user, answer=False)


async def _view_pocket(q, user, pocket_id: int, *, answer=True) -> None:
    pocket = await db.shop_pocket_get(pocket_id)

    if not pocket or not pocket["active"]:
        await q.answer("Пакет недоступен.", show_alert=True)
        return

    cmds = await db.shop_commands_by_pocket(pocket_id)
    chat_id = q.message.chat.id

    has = await db.shop_user_has_pocket(
        chat_id,
        user.id,
        pocket_id,
    )

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
        lines.append(
            "\n✅ <i>У тебя уже куплен этот пакет.</i>"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="shop:main",
                )
            ],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"💳 Купить за {pocket['price']}🪙",
                    callback_data=f"shop:buy:{pocket_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="shop:main",
                )
            ],
        ])

    try:
        await q.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except TelegramError:
        pass

    if answer:
        await q.answer()


async def _buy_pocket(q, user, pocket_id: int) -> None:
    chat = q.message.chat

    pocket = await db.shop_pocket_get(pocket_id)

    if not pocket or not pocket["active"]:
        await q.answer("Пакет недоступен.", show_alert=True)
        return

    has = await db.shop_user_has_pocket(
        chat.id,
        user.id,
        pocket_id,
    )

    if has:
        await q.answer(
            "У тебя уже есть этот пакет.",
            show_alert=True,
        )
        return

    price = int(pocket["price"])

    row = await db.get_user(user.id)
    bal = int((row or {}).get("balance") or 0)

    if bal < price:
        await q.answer(
            f"⛔ Не хватает монет: нужно {price}, "
            f"у тебя {bal}.\n"
            f"Возьми кредит (+Кредит 50) или поднакопи.",
            show_alert=True,
        )
        return

    ok = await db.take_balance_atomic(user.id, price)

    if not ok:
        await q.answer(
            "⛔ Не удалось списать монеты.",
            show_alert=True,
        )
        return

    ok2 = await db.shop_purchase_add(
        chat.id,
        user.id,
        pocket_id,
        price,
    )

    if not ok2:
        await db.add_balance(user.id, price)

        await q.answer(
            "⚠️ Не удалось выдать пакет, монеты возвращены.",
            show_alert=True,
        )
        return

    try:
        await db.treasury_add(chat.id, price)
    except Exception as e:
        logger.warning("shop treasury: %s", e)

    await q.answer(
        f"✅ Куплено: {pocket['name']} за {price}🪙",
        show_alert=True,
    )

    # Не отвечаем на callback второй раз.
    await _view_pocket(q, user, pocket_id, answer=False)
