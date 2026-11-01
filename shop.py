
"""Красивый магазин пакетов команд и токенов ИИ.

Меню приватное: кнопки работают только у того, кто вызвал «Магазин».
Чужие нажатия → «Это не твоё меню».
"""

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

_MENU_OWNERS: dict[tuple[int, int], int] = {}


# Один запрос к ИИ = один токен.
# Цены указаны в монетах чата.
AI_TOKEN_PACKS = {
    1: {"name": "Маленький", "tokens": 10, "price": 5},
    2: {"name": "Средний", "tokens": 30, "price": 12},
    3: {"name": "Большой", "tokens": 75, "price": 25},
    4: {"name": "Огромный", "tokens": 150, "price": 40},
}


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

async def cmd_shop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if not chat or not msg or not user:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🧠  Токены ИИ",
                callback_data="shop:ai",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁  Пакеты команд",
                callback_data="shop:packages",
            ),
        ],
    ])

    sent = await msg.reply_text(
        "╔════════════════════╗\n"
        "       🛒 <b>МАГАЗИН</b>\n"
        "╚════════════════════╝\n\n"
        "Добро пожаловать в магазин!\n\n"
        "Здесь ты можешь приобрести:\n\n"
        "🧠 <b>Токены ИИ</b>\n"
        "<i>Общайся с искусственным интеллектом.</i>\n\n"
        "🎁 <b>Пакеты команд</b>\n"
        "<i>Покупай наборы дополнительных команд.</i>\n\n"
        "👇 <b>Выбери нужный раздел:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

    _MENU_OWNERS[(sent.chat_id, sent.message_id)] = user.id


# ============================================================
# ОБРАБОТКА КНОПОК
# ============================================================

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

    message_key = (
        q.message.chat.id,
        q.message.message_id,
    )

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

    if action == "packages":
        await _render_packages(q, user)
        return

    if action in ("ai_buy", "view", "buy") and len(parts) == 3:
        try:
            item_id = int(parts[2])
        except (TypeError, ValueError):
            await q.answer(
                "Некорректный запрос.",
                show_alert=True,
            )
            return

        if action == "ai_buy":
            await _buy_ai_tokens(q, user, item_id)

        elif action == "view":
            await _view_pocket(q, user, item_id)

        elif action == "buy":
            await _buy_pocket(q, user, item_id)

        return

    await q.answer()


# ============================================================
# ВОЗВРАТ В ГЛАВНОЕ МЕНЮ
# ============================================================

async def _render_main(q, user) -> None:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🧠  Токены ИИ",
                callback_data="shop:ai",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁  Пакеты команд",
                callback_data="shop:packages",
            ),
        ],
    ])

    try:
        await q.edit_message_text(
            "╔════════════════════╗\n"
            "       🛒 <b>МАГАЗИН</b>\n"
            "╚════════════════════╝\n\n"
            "Выбери, что хочешь приобрести:\n\n"
            "🧠 <b>Токены ИИ</b>\n"
            "<i>Запросы к искусственному интеллекту.</i>\n\n"
            "🎁 <b>Пакеты команд</b>\n"
            "<i>Наборы дополнительных команд.</i>\n\n"
            "👇 <b>Выбирай раздел:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        pass

    await q.answer()


# ============================================================
# РАЗДЕЛ ПАКЕТОВ КОМАНД
# ============================================================

async def _render_packages(q, user) -> None:
    pockets = await db.shop_pockets_list(q.message.chat.id)

    rows = []

    for p in pockets:
        if not p["active"]:
            continue

        rows.append([
            InlineKeyboardButton(
                f"🎁 {p['name']}  ·  {p['price']} 🪙",
                callback_data=f"shop:view:{p['id']}",
            )
        ])

    if not rows:
        text = (
            "🎁 <b>ПАКЕТЫ КОМАНД</b>\n\n"
            "Пока что здесь нет доступных пакетов.\n"
            "Загляни сюда немного позже!"
        )
    else:
        text = (
            "╔════════════════════╗\n"
            "      🎁 <b>ПАКЕТЫ КОМАНД</b>\n"
            "╚════════════════════╝\n\n"
            "Покупай наборы команд и расширяй\n"
            "свои возможности в чате!\n\n"
            "🪙 Оплата производится монетами.\n\n"
            "👇 <b>Выбери понравившийся пакет:</b>"
        )

    rows.append([
        InlineKeyboardButton(
            "⬅️  В главное меню",
            callback_data="shop:main",
        )
    ])

    try:
        await q.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    await q.answer()


# ============================================================
# РАЗДЕЛ ТОКЕНОВ ИИ
# ============================================================

async def _render_ai_tokens(q, user, *, answer=True) -> None:
    tokens = await db.get_ai_tokens(user.id)

    rows = []

    for pack_id, pack in AI_TOKEN_PACKS.items():
        rows.append([
            InlineKeyboardButton(
                f"🪙 {pack['tokens']} токенов — {pack['price']} монет",
                callback_data=f"shop:ai_buy:{pack_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️  В главное меню",
            callback_data="shop:main",
        )
    ])

    text = (
        "╔════════════════════╗\n"
        "       🧠 <b>ТОКЕНЫ ИИ</b>\n"
        "╚════════════════════╝\n\n"
        f"👤 Твой баланс: <b>{tokens}</b> токенов\n\n"
        "💡 <b>Как это работает?</b>\n"
        "• Один запрос к ИИ = 1 токен.\n"
        "• Токены не сгорают.\n"
        "• Купленные токены остаются на балансе.\n\n"
        "🛍 <b>Выбери пакет:</b>\n"
        "<i>Чем больше пакет, тем выгоднее покупка.</i>"
    )

    try:
        await q.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError:
        pass

    if answer:
        await q.answer()


# ============================================================
# ПОКУПКА ТОКЕНОВ ИИ
# ============================================================

async def _buy_ai_tokens(q, user, pack_id: int) -> None:
    pack = AI_TOKEN_PACKS.get(pack_id)

    if not pack:
        await q.answer(
            "Такого пакета нет.",
            show_alert=True,
        )
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
            f"Не хватает монет!\n"
            f"Нужно: {pack['price']} 🪙\n"
            f"У тебя: {balance} 🪙",
            show_alert=True,
        )
        return

    new_tokens = await db.get_ai_tokens(user.id)

    await q.answer(
        f"✅ Куплено {pack['tokens']} токенов!\n"
        f"Теперь у тебя {new_tokens} токенов.",
        show_alert=True,
    )

    # Обновляем экран без повторного ответа на callback.
    await _render_ai_tokens(q, user, answer=False)


# ============================================================
# ПРОСМОТР ПАКЕТА КОМАНД
# ============================================================

async def _view_pocket(
    q,
    user,
    pocket_id: int,
    *,
    answer=True,
) -> None:
    pocket = await db.shop_pocket_get(pocket_id)

    if not pocket or not pocket["active"]:
        await q.answer(
            "Пакет недоступен.",
            show_alert=True,
        )
        return

    cmds = await db.shop_commands_by_pocket(pocket_id)

    chat_id = q.message.chat.id

    has = await db.shop_user_has_pocket(
        chat_id,
        user.id,
        pocket_id,
    )

    lines = [
        "╔════════════════════╗",
        f"       🎁 <b>{escape(pocket['name'])}</b>",
        "╚════════════════════╝",
        "",
        f"🪙 Цена: <b>{pocket['price']}</b> монет",
        f"📦 Команд в пакете: <b>{len(cmds)}</b>",
    ]

    if cmds:
        lines.append("")
        lines.append("✨ <b>Что входит в пакет:</b>")

        for c in cmds[:30]:
            lines.append(f"▫️ {escape(c['trigger'])}")

        if len(cmds) > 30:
            lines.append(f"… и ещё {len(cmds) - 30} команд")

    if has:
        lines.append("")
        lines.append("✅ <i>Этот пакет уже есть у тебя.</i>")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️  К пакетам команд",
                    callback_data="shop:packages",
                )
            ],
        ])

    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"🛒 Купить за {pocket['price']} 🪙",
                    callback_data=f"shop:buy:{pocket_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️  К пакетам команд",
                    callback_data="shop:packages",
                )
            ],
        ])

    try:
        await q.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        pass

    if answer:
        await q.answer()


# ============================================================
# ПОКУПКА ПАКЕТА КОМАНД
# ============================================================

async def _buy_pocket(q, user, pocket_id: int) -> None:
    chat = q.message.chat

    pocket = await db.shop_pocket_get(pocket_id)

    if not pocket or not pocket["active"]:
        await q.answer(
            "Пакет недоступен.",
            show_alert=True,
        )
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
    balance = int((row or {}).get("balance") or 0)

    if balance < price:
        await q.answer(
            f"⛔ Не хватает монет!\n"
            f"Нужно: {price} 🪙\n"
            f"У тебя: {balance} 🪙",
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
            "⚠️ Не удалось выдать пакет. Монеты возвращены.",
            show_alert=True,
        )
        return

    try:
        await db.treasury_add(chat.id, price)
    except Exception as e:
        logger.warning("shop treasury: %s", e)

    await q.answer(
        f"✅ Куплено: {pocket['name']} за {price} 🪙",
        show_alert=True,
    )

    # Показываем обновлённую информацию о пакете.
    await _view_pocket(
        q,
        user,
        pocket_id,
        answer=False,
    )
