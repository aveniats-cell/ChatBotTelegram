"""Магазин пакетов команд и токенов ИИ. Меню доступно только его владельцу."""
import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)
_MENU_OWNERS: dict[tuple[int, int], int] = {}

# Одно слово в сообщении = один токен.
AI_TOKEN_PACKS = {
    1: {"name": "Маленький", "tokens": 10, "price": 5},
    2: {"name": "Средний", "tokens": 30, "price": 12},
    3: {"name": "Большой", "tokens": 75, "price": 25},
    4: {"name": "Огромный", "tokens": 150, "price": 40},
}


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    keyboard = _main_keyboard()
    sent = await msg.reply_text(
        "🛒 <b>МАГАЗИН</b>\n\n"
        "Здесь можно приобрести токены для общения с ИИ "
        "и дополнительные пакеты команд.\n\n"
        "Выбери нужный раздел:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    _MENU_OWNERS[(sent.chat_id, sent.message_id)] = user.id


def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Токены ИИ", callback_data="shop:ai")],
        [InlineKeyboardButton("Пакеты команд", callback_data="shop:packages")],
        [InlineKeyboardButton("← Главное меню", callback_data="home:back")],
    ])


async def open_shop_from_callback(query, user) -> None:
    """Открывает магазин из главной панели, сохраняя владельца меню."""
    if not query or not query.message or not user:
        return
    key = (query.message.chat.id, query.message.message_id)
    _MENU_OWNERS[key] = user.id
    await _safe_edit(
        query,
        "<b>Магазин</b>\n\nВыбери, что хочешь приобрести.\n"
        "Покупки подтверждаются отдельно; баланс проверяется при оплате.",
        _main_keyboard(),
    )


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q, user = update.callback_query, update.effective_user
    if not q or not user or not q.message:
        return
    parts = (q.data or "").split(":")
    if len(parts) < 2 or parts[0] != "shop":
        await q.answer()
        return

    key = (q.message.chat.id, q.message.message_id)
    owner = _MENU_OWNERS.get(key)
    if owner is None or owner != user.id:
        await q.answer("Это не твоё меню. Вызови «Магазин» сам.", show_alert=True)
        return

    action = parts[1]
    if action == "main":
        await _render_main(q)
    elif action == "ai":
        await _render_ai_tokens(q, user)
    elif action == "packages":
        await _render_packages(q)
    elif action == "ai_buy" and len(parts) == 3:
        try:
            await _buy_ai_tokens(q, user, int(parts[2]))
        except ValueError:
            await q.answer("Некорректный пакет.", show_alert=True)
    elif action == "view" and len(parts) == 3:
        try:
            await _view_pocket(q, user, int(parts[2]))
        except ValueError:
            await q.answer("Некорректный пакет.", show_alert=True)
    elif action == "buy" and len(parts) == 3:
        try:
            await _buy_pocket(q, user, int(parts[2]))
        except ValueError:
            await q.answer("Некорректный пакет.", show_alert=True)
    else:
        await q.answer()


async def _safe_edit(q, text, keyboard):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    except TelegramError:
        logger.exception("Не удалось обновить меню магазина")


async def _render_main(q):
    await _safe_edit(
        q,
        "🛒 <b>МАГАЗИН</b>\n\nВыбери, что хочешь приобрести:\n\n"
        "🧠 <b>Токены ИИ</b> — запросы к искусственному интеллекту.\n"
        "🎁 <b>Пакеты команд</b> — наборы дополнительных команд.",
        _main_keyboard(),
    )
    await q.answer()


async def _render_packages(q):
    pockets = await db.shop_pockets_list(q.message.chat.id)
    rows = []
    for p in pockets or []:
        if not p.get("active"):
            continue
        rows.append([InlineKeyboardButton(
            f"🎁 {p['name']} · {p['price']} 🪙",
            callback_data=f"shop:view:{int(p['id'])}",
        )])
    rows.append([InlineKeyboardButton("← В главное меню", callback_data="shop:main")])
    text = (
        "🎁 <b>ПАКЕТЫ КОМАНД</b>\n\n"
        "Покупай наборы команд и расширяй свои возможности в чате.\n"
        "Оплата производится монетами."
        if len(rows) > 1 else
        "🎁 <b>ПАКЕТЫ КОМАНД</b>\n\nПока нет доступных пакетов."
    )
    await _safe_edit(q, text, InlineKeyboardMarkup(rows))
    await q.answer()


async def _render_ai_tokens(q, user, *, answer=True):
    tokens = int(await db.get_ai_tokens(user.id) or 0)
    rows = [[InlineKeyboardButton(
        f"🪙 {p['tokens']} токенов — {p['price']} монет",
        callback_data=f"shop:ai_buy:{pid}",
    )] for pid, p in AI_TOKEN_PACKS.items()]
    rows.append([InlineKeyboardButton("← В главное меню", callback_data="shop:main")])
    await _safe_edit(
        q,
        "🧠 <b>ТОКЕНЫ ИИ</b>\n\n"
        f"Твой баланс: <b>{tokens}</b> токенов.\n\n"
        "• Одно слово в запросе = 1 токен (слова разделяются пробелами и переносами строк).\n"
        "• Токены не сгорают.\n"
        "• Купленные токены остаются на балансе.\n\n"
        "Выбери пакет:",
        InlineKeyboardMarkup(rows),
    )
    if answer:
        await q.answer()


async def _buy_ai_tokens(q, user, pack_id: int):
    pack = AI_TOKEN_PACKS.get(pack_id)
    if not pack:
        await q.answer("Такого пакета нет.", show_alert=True)
        return
    if not await db.get_user(user.id):
        await q.answer("Сначала напиши /start.", show_alert=True)
        return

    ok = await db.buy_ai_tokens(user.id, pack["price"], pack["tokens"])
    if not ok:
        row = await db.get_user(user.id)
        balance = int((row or {}).get("balance") or 0)
        await q.answer(
            f"Не хватает монет. Нужно {pack['price']} 🪙, у тебя {balance} 🪙.",
            show_alert=True,
        )
        return

    new_tokens = int(await db.get_ai_tokens(user.id) or 0)
    await q.answer(
        f"Куплено {pack['tokens']} токенов. Баланс: {new_tokens}.",
        show_alert=True,
    )
    await _render_ai_tokens(q, user, answer=False)


async def _view_pocket(q, user, pocket_id: int, *, answer=True):
    pocket = await db.shop_pocket_get(pocket_id)
    if not pocket or not pocket.get("active"):
        await q.answer("Пакет недоступен.", show_alert=True)
        return
    cmds = await db.shop_commands_by_pocket(pocket_id)
    chat_id = q.message.chat.id
    has = await db.shop_user_has_pocket(chat_id, user.id, pocket_id)

    lines = [
        f"🎁 <b>{escape(str(pocket['name']))}</b>",
        f"🪙 Цена: <b>{int(pocket['price'])}</b> монет",
        f"📦 Команд в пакете: <b>{len(cmds)}</b>",
    ]
    if cmds:
        lines.extend(["", "✨ <b>Что входит в пакет:</b>"])
        lines.extend(f"▫️ {escape(str(c['trigger']))}" for c in cmds[:30])
        if len(cmds) > 30:
            lines.append(f"… и ещё {len(cmds) - 30} команд")

    if has:
        lines.extend(["", "✅ <i>Этот пакет уже есть у тебя.</i>"])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("← К пакетам команд", callback_data="shop:packages")
        ]])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🛒 Купить за {int(pocket['price'])} 🪙",
                callback_data=f"shop:buy:{pocket_id}",
            )],
            [InlineKeyboardButton("← К пакетам команд", callback_data="shop:packages")],
        ])
    await _safe_edit(q, "\n".join(lines), keyboard)
    if answer:
        await q.answer()


async def _buy_pocket(q, user, pocket_id: int):
    chat = q.message.chat
    pocket = await db.shop_pocket_get(pocket_id)
    if not pocket or not pocket.get("active"):
        await q.answer("Пакет недоступен.", show_alert=True)
        return

    price = int(pocket["price"])

    # shop_purchase_add сам атомарно проверяет баланс и списывает цену.
    # Повторно вызывать take_balance_atomic здесь нельзя: это приводило
    # к двойному списанию за один пакет.
    try:
        purchased = await db.shop_purchase_add(
            chat.id, user.id, pocket_id, price
        )
    except Exception:
        logger.exception(
            "shop_purchase_add failed: chat=%s user=%s pocket=%s",
            chat.id, user.id, pocket_id,
        )
        purchased = False

    if not purchased:
        # shop_purchase_add самостоятельно возвращает деньги, если
        # списание прошло, но запись покупки не удалась. Не делаем
        # второй возврат здесь, иначе пользователю могут начислиться
        # лишние монеты.
        row = await db.get_user(user.id)
        balance = int((row or {}).get("balance") or 0)
        await q.answer(
            "Не удалось купить пакет. "
            f"Проверь баланс: {balance} 🪙. "
            "Если монеты списались, но пакет не появился, обратись к администратору.",
            show_alert=True,
        )
        return

    # Покупка уже выдана. Ошибка пополнения казны не должна отменять
    # покупку или повторно списывать деньги.
    try:
        treasury_ok = await db.treasury_add(chat.id, price)
        if not treasury_ok:
            logger.error(
                "Пакет куплен, но treasury_add вернул False: "
                "chat=%s user=%s pocket=%s",
                chat.id, user.id, pocket_id,
            )
    except Exception:
        logger.exception(
            "Пакет куплен, но не удалось пополнить казну: "
            "chat=%s user=%s pocket=%s",
            chat.id, user.id, pocket_id,
        )

    await q.answer(f"Куплено: {pocket['name']} за {price} 🪙", show_alert=True)
    await _view_pocket(q, user, pocket_id, answer=False)
