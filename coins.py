"""Магазин коинов.

Создание (только ССС/админ) — FSM на _PENDING_CREATE:
  имя → описание → цена (0..10) → тираж (10/20/100/1000) → доход (0..100)
  → фото (сжатие 512x512 JPEG) → Принять/Отказать → оплата → создание.

Покупка: цена bonding curve, 6% в казну, 94% создателю.
Команда «Коины» — список кнопок.
"""
import os
import re
import io
import hashlib
import logging
from datetime import datetime, timezone
from html import escape
from typing import Optional

from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import sss

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

FEE_REG = 10
FEE_NAME = 5
FEE_DESC = 2
FEE_PHOTO = 5
FEE_BASE = FEE_REG + FEE_NAME + FEE_DESC + FEE_PHOTO  # 22
SECOND_COIN_FEE = 1000

TIER_FEE = {10: 0, 20: 1, 100: 10, 1000: 100}
TIER_LIST = [10, 20, 100, 1000]

MARKET_FEE_PCT = 6
PHOTO_SIZE = (512, 512)
PHOTO_QUALITY = 85
NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
DESC_MAX = 300
YIELD_MAX = 100


def _is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


def _is_sss(uid: int) -> bool:
    return _is_admin(uid) or sss.is_sss_member(uid)


def _current_price(coin: dict) -> int:
    """Bonding curve: +1 к цене за каждые 10% тиража."""
    total = max(1, int(coin["total_supply"]))
    step = max(1, total // 10)
    sold = int(coin["sold"] or 0)
    return int(coin["start_price"]) + sold // step


def _tier_fee(total_supply: int) -> int:
    return TIER_FEE.get(total_supply, 0)


def _total_fee(user: dict, total_supply: int) -> int:
    base = FEE_BASE + _tier_fee(total_supply)
    if (user.get("coins_created") or 0) >= 1:
        base += SECOND_COIN_FEE
    return base


# FSM-состояние создания: user_id -> dict(step, chat_id, ...)
_PENDING_CREATE: dict[int, dict] = {}


# ---------- /Коины ----------

async def cmd_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    rows = await db.coins_list_by_chat(chat.id)
    if not rows:
        await msg.reply_text("Пока нет зарегистрированных коинов.")
        return
    kb_rows = []
    for c in rows:
        kb_rows.append([InlineKeyboardButton(
            f"{c['name']} — {_current_price(c)}💰",
            callback_data=f"coin:view:{c['id']}",
        )])
    kb_rows.append([InlineKeyboardButton("🎒 Мои коины", callback_data="coin:mine")])
    await msg.reply_text(
        "🪙 <b>Зарегистрированные коины</b>\nВыбери монету:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


# ---------- карточка монеты ----------

async def _render_coin_card(coin: dict) -> tuple[str, InlineKeyboardMarkup]:
    cp = _current_price(coin)
    sold = int(coin["sold"] or 0)
    total = int(coin["total_supply"] or 0)
    left = max(0, total - sold)
    text = (
        f"🪙 <b>{escape(coin['name'])}</b>\n"
        f"{escape(coin['description'])}\n\n"
        f"Цена: <b>{cp}</b> 💰\n"
        f"Куплено: <b>{sold}/{total}</b> (осталось <b>{left}</b>)\n"
        f"Доход: <b>{coin['yield_per_day']}</b> 💰/сутки"
    )
    rows = [
        [InlineKeyboardButton("💰 Купить", callback_data=f"coin:buy:{coin['id']}")],
        [InlineKeyboardButton("📈 Посмотреть доход", callback_data=f"coin:chart:{coin['id']}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="coin:main")],
    ]
    return text, InlineKeyboardMarkup(rows)


async def coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) < 2 or parts[0] != "coin":
        await q.answer()
        return
    action = parts[1]

    if action == "main":
        rows = await db.coins_list_by_chat(q.message.chat.id)
        kb_rows = [[InlineKeyboardButton(
            f"{c['name']} — {_current_price(c)}💰",
            callback_data=f"coin:view:{c['id']}",
        )] for c in rows]
        kb_rows.append([InlineKeyboardButton("🎒 Мои коины", callback_data="coin:mine")])
        try:
            await q.edit_message_text(
                "🪙 <b>Зарегистрированные коины</b>\nВыбери монету:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )
        except TelegramError:
            pass
        await q.answer()
        return

    if action == "view" and len(parts) == 3:
        cid = int(parts[2])
        coin = await db.coin_get(cid)
        if not coin:
            await q.answer("Монета не найдена.", show_alert=True)
            return
        text, kb = await _render_coin_card(coin)
        try:
            if coin.get("photo_file_id"):
                await q.message.reply_photo(
                    photo=coin["photo_file_id"],
                    caption=text, parse_mode=ParseMode.HTML, reply_markup=kb,
                )
                await q.message.delete()
            else:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramError as e:
            logger.warning("coin view: %s", e)
        await q.answer()
        return

    if action == "buy" and len(parts) == 3:
        cid = int(parts[2])
        await _buy_coin(q, user, cid)
        return

    if action == "chart" and len(parts) == 3:
        cid = int(parts[2])
        await _send_chart(q, context, cid)
        return

    if action == "mine":
        await _render_my_coins(q, user)
        return

    if action == "new":
        await _start_create(q, user)
        return

    await q.answer()


async def _buy_coin(q, user, cid: int) -> None:
    coin = await db.coin_get(cid)
    if not coin or not coin["active"]:
        await q.answer("Монета недоступна.", show_alert=True)
        return
    if int(coin["sold"]) >= int(coin["total_supply"]):
        await q.answer("Распродано. Ищи на Рынке.", show_alert=True)
        return

    price = _current_price(coin)
    row = await db.get_user(user.id)
    bal = (row or {}).get("balance") or 0
    if bal < price:
        await q.answer(f"⛔ Нужно {price}, у тебя {bal}.", show_alert=True)
        return

    ok = await db.take_balance_atomic(user.id, price)
    if not ok:
        await q.answer("⛔ Не удалось списать.", show_alert=True)
        return

    fee = max(1, price * MARKET_FEE_PCT // 100)
    to_creator = price - fee

    try:
        await db.add_balance(coin["creator_id"], to_creator)
    except Exception as e:
        logger.warning("coin creator payout: %s", e)

    try:
        await db.treasury_add(q.message.chat.id, fee)
    except Exception as e:
        logger.warning("coin treasury: %s", e)

    ok2 = await db.coin_buy_atomic(cid, user.id, price, to_creator)
    if not ok2:
        await db.add_balance(user.id, price)
        await db.add_balance(coin["creator_id"], -to_creator)
        await q.answer("⚠️ Покупка не удалась, монеты возвращены.", show_alert=True)
        return

    coin2 = await db.coin_get(cid)
    try:
        await db.coin_price_history_add(cid, _current_price(coin2 or coin), int((coin2 or coin)["sold"]))
    except Exception as e:
        logger.warning("coin history: %s", e)

    await q.answer(f"✅ Куплено за {price}💰")
    text, kb = await _render_coin_card(coin2 or coin)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        pass


async def _send_chart(q, context, cid: int) -> None:
    import coin_graph
    coin = await db.coin_get(cid)
    if not coin:
        await q.answer("Монета не найдена.", show_alert=True)
        return
    hist = await db.coin_price_history_get(cid, limit=60)
    if len(hist) < 2:
        await q.answer("Нет данных для графика.", show_alert=True)
        return
    png = coin_graph.render_price_chart(
        coin["name"],
        [(h["ts"], h["price"], h["sold"]) for h in hist],
        _current_price(coin),
        int(coin["yield_per_day"]),
    )
    if not png:
        await q.answer("Нет данных для графика.", show_alert=True)
        return
    await context.bot.send_photo(
        chat_id=q.message.chat.id,
        photo=InputFile(io.BytesIO(png), filename="chart.png"),
    )
    await q.answer()


async def _render_my_coins(q, user) -> None:
    chat_id = q.message.chat.id
    bought = await db.coin_holders_by_user(chat_id, user.id)
    created = await db.coins_by_creator(chat_id, user.id)
    lines = ["🎒 <b>Мои коины</b>"]
    lines.append("\n<b>Купленные:</b>")
    if not bought:
        lines.append("• —")
    else:
        for h in bought:
            coin = await db.coin_get(h["coin_id"])
            if not coin:
                continue
            cp = _current_price(coin)
            value = cp * int(h["quantity"])
            lines.append(
                f"• {escape(coin['name'])}: <b>{h['quantity']}</b> шт · "
                f"цена <b>{cp}</b>💰 · стоимость <b>{value}</b>💰"
            )
    lines.append("\n<b>Выпущенные:</b>")
    if not created:
        lines.append("• —")
    else:
        for c in created:
            lines.append(
                f"• {escape(c['name'])}: продано <b>{c['sold']}/{c['total_supply']}</b> · "
                f"доход <b>{c['yield_per_day']}</b>💰/сутки"
            )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="coin:main")]])
    try:
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        pass
    await q.answer()


# ---------- создание ----------

async def _start_create(q, user) -> None:
    if not _is_sss(user.id):
        logger.info("coin create DENY: user_id=%s not in SSS", user.id)
        await q.answer("Только члены ССС могут создавать коины.", show_alert=True)
        return

    logger.info("coin create START: user_id=%s chat=%s", user.id, q.message.chat_id)
    _PENDING_CREATE[user.id] = {"step": "name", "chat_id": q.message.chat_id}

    try:
        await q.bot.send_message(
            chat_id=q.message.chat_id,
            text=(
                "🪙 <b>Создание монеты</b>\n\n"
                "Шаг 1/6: напиши название (3–16 символов, A-Za-z0-9_)."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("coin create send: %s", e)

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await q.answer()


async def create_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True, если сообщение было обработано как шаг создания."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return False
    st = _PENDING_CREATE.get(user.id)
    if not st:
        return False

    text = (msg.text or "").strip()
    step = st["step"]
    logger.info("coin create input: user_id=%s step=%s text=%r",
                user.id, step, text[:40])

    if step == "name":
        if not NAME_RE.match(text):
            await msg.reply_text("⛔ Название: 3–16 символов, буквы/цифры/_.")
            return True
        if await db.coin_find_by_name(st["chat_id"], text):
            await msg.reply_text("⛔ Такое название уже занято в чате.")
            return True
        st["name"] = text
        st["step"] = "desc"
        await msg.reply_text(
            f"Название: <b>{escape(text)}</b>\n\n"
            f"Шаг 2/6: описание (до {DESC_MAX} символов).",
            parse_mode=ParseMode.HTML,
        )
        return True

    if step == "desc":
        if not text or len(text) > DESC_MAX:
            await msg.reply_text(f"⛔ Описание: 1–{DESC_MAX} символов.")
            return True
        st["description"] = text
        st["step"] = "price"
        await msg.reply_text(
            "Шаг 3/6: цена (целое 0–10 монет)."
        )
        return True

    if step == "price":
        if not text.isdigit() or not (0 <= int(text) <= 10):
            await msg.reply_text("⛔ Цена: целое 0–10.")
            return True
        st["start_price"] = int(text)
        st["step"] = "supply"
        rows = [[InlineKeyboardButton(
            f"Тираж {t} — плата {TIER_FEE[t]}💰",
            callback_data=f"coin:tier:{t}",
        )] for t in TIER_LIST]
        await msg.reply_text(
            "Шаг 4/6: выбери тираж:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if step == "yield":
        if not text.isdigit() or not (0 <= int(text) <= YIELD_MAX):
            await msg.reply_text(f"⛔ Доход: целое 0–{YIELD_MAX}.")
            return True
        st["yield_per_day"] = int(text)
        st["step"] = "photo"
        await msg.reply_text(
            "Шаг 6/6: пришли фото для монеты. Оно будет сжато в 512×512 JPEG."
        )
        return True

    return False


async def tier_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "coin" or parts[1] != "tier":
        await q.answer()
        return
    t = int(parts[2])
    if t not in TIER_FEE:
        await q.answer("Неверный тираж.", show_alert=True)
        return
    st = _PENDING_CREATE.get(user.id)
    if not st:
        await q.answer("Сессия создания истекла.", show_alert=True)
        return
    st["total_supply"] = t
    st["step"] = "yield"
    row = await db.get_user(user.id)
    fee = _total_fee(row or {}, t)
    await q.edit_message_text(
        f"Тираж: <b>{t}</b>\n"
        f"Полная плата: <b>{fee}</b>💰 (в казну)\n\n"
        f"Шаг 5/6: доход в сутки (целое 0–{YIELD_MAX}).",
        parse_mode=ParseMode.HTML,
    )
    await q.answer()


async def photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ловит фото в шаге photo, сжимает 512x512 JPEG, показывает превью."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return False
    st = _PENDING_CREATE.get(user.id)
    if not st or st.get("step") != "photo":
        return False
    photo = msg.photo[-1] if msg.photo else None
    if not photo:
        await msg.reply_text("Пришли именно фото.")
        return True

    f = await context.bot.get_file(photo.file_id)
    raw = await f.download_as_bytearray()
    try:
        img = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
        img = img.resize(PHOTO_SIZE, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=PHOTO_QUALITY)
        jpeg = buf.getvalue()
    except Exception as e:
        logger.warning("coin photo resize: %s", e)
        await msg.reply_text("⚠️ Не удалось обработать фото.")
        return True

    h = hashlib.sha256(jpeg).hexdigest()
    if await db.coin_find_by_photo_hash(st["chat_id"], h):
        await msg.reply_text("⛔ Такое фото уже используется в чате.")
        return True

    st["photo_bytes"] = jpeg
    st["photo_hash"] = h

    sent = await context.bot.send_photo(
        chat_id=msg.chat_id,
        photo=InputFile(io.BytesIO(jpeg), filename="coin.jpg"),
        caption="Превью 512×512 JPEG. Принять?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принять", callback_data="coin:photo_ok"),
            InlineKeyboardButton("❌ Отказать", callback_data="coin:photo_no"),
        ]]),
    )
    st["preview_msg_id"] = sent.message_id
    return True


async def photo_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    if q.data == "coin:photo_no":
        _PENDING_CREATE.pop(user.id, None)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text="❌ Создание отменено.",
            )
        except TelegramError:
            pass
        await q.answer()
        return
    if q.data == "coin:photo_ok":
        st = _PENDING_CREATE.get(user.id)
        if not st:
            await q.answer("Сессия истекла.", show_alert=True)
            return
        row = await db.get_user(user.id)
        bal = (row or {}).get("balance") or 0
        total_fee = _total_fee(row or {}, st["total_supply"])
        if bal < total_fee:
            await q.answer(f"⛔ Нужно {total_fee}, у тебя {bal}.", show_alert=True)
            _PENDING_CREATE.pop(user.id, None)
            return
        ok = await db.take_balance_atomic(user.id, total_fee)
        if not ok:
            await q.answer("⛔ Не удалось списать.", show_alert=True)
            _PENDING_CREATE.pop(user.id, None)
            return
        try:
            await db.treasury_add(st["chat_id"], total_fee)
        except Exception as e:
            logger.warning("coin treasury create: %s", e)
        sent = await context.bot.send_photo(
            chat_id=st["chat_id"],
            photo=InputFile(io.BytesIO(st["photo_bytes"]), filename="coin.jpg"),
        )
        file_id = sent.photo[-1].file_id
        try:
            await sent.delete()
        except TelegramError:
            pass
        cid = await db.coin_create(
            st["chat_id"], user.id, st["name"], st["description"],
            st["start_price"], st["total_supply"], st["yield_per_day"],
            file_id, st["photo_hash"],
        )
        _PENDING_CREATE.pop(user.id, None)
        if not cid:
            await db.add_balance(user.id, total_fee)
            await q.answer("⚠️ Не удалось создать монету, монеты возвращены.", show_alert=True)
            return
        try:
            await db.coin_price_history_add(cid, st["start_price"], 0)
        except Exception as e:
            logger.warning("coin history create: %s", e)
        await db.inc_user_counter_coin_created(user.id)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=st["chat_id"],
                text=(
                    f"✅ Монета <b>{escape(st['name'])}</b> создана!\n"
                    f"Тираж: <b>{st['total_supply']}</b> · Цена: <b>{st['start_price']}</b>💰\n"
                    f"Доход: <b>{st['yield_per_day']}</b>💰/сутки"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("coin create done send: %s", e)
        await q.answer()
        return
    await q.answer()
