"""Магазин коинов (монеты как активы).

FSM создания (только лицензированные эмитенты):
  имя → описание → тираж (10/20/100/1000) → фото → принять → создать.

Цена и доход НЕ вводятся:
  - start_price = 1 (номинал);
  - yield_per_day = 0 (доход = дивиденды от комиссий торговли);
  - цена = NAV = max(1, reserve // circulating).

Покупка на первичке: fee (6%) в казну, to_reserve в резерв монеты.
Продажа в резерв: sell_price = NAV * (100 - 3) // 100.

FIX #1: покупка монеты с ценой 0 больше не ломается.
FIX #2: FSM создания привязан к владельцу (owner_id + актуальные права).
FIX #3: whitelist эмитентов, лимит 1 активная монета, NAV, резерв, дивиденды.
FIX #4: команда «УдалитьМонету <имя>» — архивирует монету (только админ).
"""
import os
import re
import io
import hashlib
import logging
from html import escape

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

MAX_ACTIVE_COINS_PER_USER = 1
MARKET_FEE_PCT = 6
SELL_DISCOUNT_PCT = 3
NAV_MIN = 1

PHOTO_SIZE = (512, 512)
PHOTO_QUALITY = 85
NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
DESC_MAX = 300

TIER_LIST = [10, 20, 100, 1000]


def _is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


def _is_sss(uid: int) -> bool:
    try:
        return _is_admin(uid) or sss.is_sss_member(uid)
    except Exception as e:
        logger.warning("is_sss check failed for %s: %s", uid, e)
        return _is_admin(uid)


def _fsm_owner_ok(user_id: int, st: dict | None) -> bool:
    if not st:
        return False
    if st.get("owner_id") != user_id:
        return False
    return True


def _current_price(coin: dict) -> int:
    circ = max(1, int(coin.get("circulating") or 0))
    reserve = int(coin.get("reserve") or 0)
    return max(NAV_MIN, reserve // circ)


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
    circ = int(coin.get("circulating") or 0)
    reserve = int(coin.get("reserve") or 0)
    div_pool = int(coin.get("dividend_pool") or 0)
    left = max(0, total - sold)
    text = (
        f"🪙 <b>{escape(coin['name'])}</b>\n"
        f"{escape(coin['description'])}\n\n"
        f"Цена (NAV): <b>{cp}</b> 💰\n"
        f"Эмиссия: <b>{sold}/{total}</b> (осталось <b>{left}</b>)\n"
        f"В обороте: <b>{circ}</b> шт\n"
        f"Резерв монеты: <b>{reserve}</b> 💰\n"
        f"Дивидендный пул: <b>{div_pool}</b> 💰"
    )
    rows = [
        [InlineKeyboardButton("💰 Купить", callback_data=f"coin:buy:{coin['id']}")],
        [InlineKeyboardButton("💵 Продать в резерв",
                              callback_data=f"coin:sell_reserve:{coin['id']}")],
        [InlineKeyboardButton("📈 График", callback_data=f"coin:chart:{coin['id']}")],
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

    if action == "sell_reserve" and len(parts) == 3:
        cid = int(parts[2])
        await _sell_to_reserve(q, user, cid)
        return

    if action == "chart" and len(parts) == 3:
        cid = int(parts[2])
        await _send_chart(q, context, cid)
        return

    if action == "mine":
        await _render_my_coins(q, user)
        return

    if action == "new":
        try:
            await _start_create(q, user)
        except Exception as e:
            logger.exception("coin create start CRASH: %s", e)
            try:
                await q.answer(
                    "⚠️ Ошибка запуска создания. Попробуй ещё раз.",
                    show_alert=True,
                )
            except TelegramError:
                pass
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

    if price > 0:
        if bal < price:
            await q.answer(f"⛔ Нужно {price}, у тебя {bal}.", show_alert=True)
            return
        ok = await db.take_balance_atomic(user.id, price)
        if not ok:
            await q.answer("⛔ Не удалось списать.", show_alert=True)
            return

    if price <= 1:
        fee = 0
        to_reserve = price
    else:
        fee = max(1, price * MARKET_FEE_PCT // 100)
        to_reserve = price - fee
        if to_reserve < 0:
            to_reserve = 0

    if fee > 0:
        try:
            await db.treasury_add(q.message.chat.id, fee)
        except Exception as e:
            logger.warning("coin treasury: %s", e)

    ok2 = await db.coin_buy_atomic(cid, user.id, price, to_reserve)
    if not ok2:
        if price > 0:
            await db.add_balance(user.id, price)
        if fee > 0:
            await db.treasury_add(q.message.chat.id, -fee)
        await q.answer("⚠️ Покупка не удалась, монеты возвращены.", show_alert=True)
        return

    coin2 = await db.coin_get(cid)
    try:
        await db.coin_price_history_add(
            cid, _current_price(coin2 or coin), int((coin2 or coin)["sold"])
        )
    except Exception as e:
        logger.warning("coin history: %s", e)

    await q.answer(f"✅ Куплено за {price}💰")
    text, kb = await _render_coin_card(coin2 or coin)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        pass


async def _sell_to_reserve(q, user, cid: int) -> None:
    coin = await db.coin_get(cid)
    if not coin:
        await q.answer("Монета не найдена.", show_alert=True)
        return
    held = await db.coin_holder_get(cid, user.id)
    qty = int((held or {}).get("quantity") or 0)
    if qty <= 0:
        await q.answer("У тебя нет этой монеты.", show_alert=True)
        return
    cp = _current_price(coin)
    sell_unit = max(NAV_MIN, cp * (100 - SELL_DISCOUNT_PCT) // 100)
    total = sell_unit * qty
    reserve = int(coin.get("reserve") or 0)
    if reserve < total:
        await q.answer(
            f"⛔ В резерве недостаточно денег: нужно {total}, есть {reserve}.",
            show_alert=True,
        )
        return

    ok = await db.coin_sell_to_reserve_atomic(cid, user.id, qty, total)
    if not ok:
        await q.answer("⚠️ Не удалось продать. Попробуй ещё раз.", show_alert=True)
        return

    await db.add_balance(user.id, total)
    coin2 = await db.coin_get(cid)
    try:
        await db.coin_price_history_add(
            cid, _current_price(coin2 or coin), int((coin2 or coin)["sold"])
        )
    except Exception as e:
        logger.warning("coin history sell: %s", e)

    await q.answer(f"✅ Продано {qty} шт за {total}💰")
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
        0,
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
                f"NAV <b>{cp}</b>💰 · стоимость <b>{value}</b>💰"
            )
    lines.append("\n<b>Выпущенные:</b>")
    if not created:
        lines.append("• —")
    else:
        for c in created:
            lines.append(
                f"• {escape(c['name'])}: эмиссия <b>{c['sold']}/{c['total_supply']}</b> · "
                f"в обороте <b>{c.get('circulating', 0)}</b> · "
                f"резерв <b>{c.get('reserve', 0)}</b>💰"
            )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="coin:main")]])
    try:
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        pass
    await q.answer()


# ---------- создание ----------

async def _start_create(q, user) -> None:
    chat_id = q.message.chat_id if q.message else None
    if chat_id is None:
        try:
            await q.answer("Не удалось определить чат.", show_alert=True)
        except TelegramError:
            pass
        return

    if not await db.emitter_is_allowed(chat_id, user.id):
        logger.info("coin create DENY: user_id=%s not in emitters", user.id)
        try:
            await q.answer(
                "⛔ Эмиссия доступна только лицензированным эмитентам.",
                show_alert=True,
            )
        except TelegramError as e:
            logger.warning("coin create deny answer: %s", e)
        return

    active = await db.user_active_coin_count(chat_id, user.id)
    if active >= MAX_ACTIVE_COINS_PER_USER:
        try:
            await q.answer(
                f"⛔ У тебя уже есть активная монета. Лимит: "
                f"{MAX_ACTIVE_COINS_PER_USER}.",
                show_alert=True,
            )
        except TelegramError:
            pass
        return

    existing = _PENDING_CREATE.get(user.id)
    if existing is not None:
        try:
            await q.answer(
                "⚠️ У тебя уже идёт создание монеты. Заверши или отмени (/cancel).",
                show_alert=True,
            )
        except TelegramError:
            pass
        return

    logger.info("coin create START: user_id=%s chat=%s", user.id, chat_id)

    _PENDING_CREATE[user.id] = {
        "step": "name",
        "chat_id": chat_id,
        "owner_id": user.id,
    }

    sent_ok = False
    try:
        await q.bot.send_message(
            chat_id=chat_id,
            text=(
                "🪙 <b>Создание монеты</b>\n\n"
                "Шаг 1/4: напиши название (3–16 символов, A-Za-z0-9_).\n"
                "<i>Цена и доход не вводятся — они считаются автоматически.</i>\n"
                "<i>Отмена: /cancel</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
        sent_ok = True
    except TelegramError as e:
        logger.warning("coin create send: %s", e)

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError as e:
        logger.info("coin create edit markup: %s", e)

    try:
        if sent_ok:
            await q.answer()
        else:
            await q.answer("⚠️ Не удалось отправить сообщение. Проверь права бота.", show_alert=True)
    except TelegramError as e:
        logger.warning("coin create answer: %s", e)


async def create_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return False
    st = _PENDING_CREATE.get(user.id)
    if not _fsm_owner_ok(user.id, st):
        return False

    text = (msg.text or "").strip()
    step = st["step"]
    logger.info("coin create input: user_id=%s step=%s text=%r",
                user.id, step, text[:40])

    if text.startswith("/"):
        _PENDING_CREATE.pop(user.id, None)
        return False

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
            f"Шаг 2/4: описание (до {DESC_MAX} символов).",
            parse_mode=ParseMode.HTML,
        )
        return True

    if step == "desc":
        if not text or len(text) > DESC_MAX:
            await msg.reply_text(f"⛔ Описание: 1–{DESC_MAX} символов.")
            return True
        st["description"] = text
        st["step"] = "supply"
        rows = [[InlineKeyboardButton(
            f"Тираж {t}",
            callback_data=f"coin:tier:{t}",
        )] for t in TIER_LIST]
        await msg.reply_text(
            "Шаг 3/4: выбери тираж:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    return False


async def cancel_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    st = _PENDING_CREATE.get(user.id)
    if not _fsm_owner_ok(user.id, st):
        return
    _PENDING_CREATE.pop(user.id, None)
    await msg.reply_text("❌ Создание монеты отменено.")


async def tier_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "coin" or parts[1] != "tier":
        await q.answer()
        return

    st = _PENDING_CREATE.get(user.id)
    if not _fsm_owner_ok(user.id, st):
        await q.answer("⛔ Это не твоя сессия создания.", show_alert=True)
        return

    t = int(parts[2])
    if t not in TIER_LIST:
        await q.answer("Неверный тираж.", show_alert=True)
        return
    st["total_supply"] = t
    st["step"] = "photo"
    try:
        await q.edit_message_text(
            f"Тираж: <b>{t}</b>\n\n"
            f"Шаг 4/4: пришли фото для монеты. Оно будет сжато в 512×512 JPEG.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("tier edit: %s", e)
    await q.answer()


async def photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return False
    st = _PENDING_CREATE.get(user.id)
    if not _fsm_owner_ok(user.id, st) or st.get("step") != "photo":
        return False
    photo = msg.photo[-1] if msg.photo else None
    if not photo:
        await msg.reply_text("Пришли именно фото.")
        return True

    try:
        f = await context.bot.get_file(photo.file_id)
        raw = await f.download_as_bytearray()
    except TelegramError as e:
        logger.warning("coin photo download: %s", e)
        await msg.reply_text("⚠️ Не удалось скачать фото.")
        return True

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

    try:
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
    except TelegramError as e:
        logger.warning("coin photo preview: %s", e)
        await msg.reply_text("⚠️ Не удалось отправить превью.")
    return True


async def photo_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return

    st = _PENDING_CREATE.get(user.id)
    if not _fsm_owner_ok(user.id, st):
        await q.answer("⛔ Это не твоя сессия создания.", show_alert=True)
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
        active = await db.user_active_coin_count(st["chat_id"], user.id)
        if active >= MAX_ACTIVE_COINS_PER_USER:
            _PENDING_CREATE.pop(user.id, None)
            await q.answer(
                f"⛔ У тебя уже есть активная монета. Лимит: "
                f"{MAX_ACTIVE_COINS_PER_USER}.",
                show_alert=True,
            )
            return
        if not await db.emitter_is_allowed(st["chat_id"], user.id):
            _PENDING_CREATE.pop(user.id, None)
            await q.answer(
                "⛔ Ты больше не эмитент. Создание отменено.",
                show_alert=True,
            )
            return

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
            1,
            st["total_supply"],
            0,
            file_id, st["photo_hash"],
        )
        _PENDING_CREATE.pop(user.id, None)
        if not cid:
            await q.answer("⚠️ Не удалось создать монету.", show_alert=True)
            return
        try:
            await db.coin_price_history_add(cid, 1, 0)
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
                    f"Тираж: <b>{st['total_supply']}</b>\n"
                    f"Стартовая цена: <b>1</b>💰 (NAV)\n"
                    f"Доход: дивиденды от комиссий торговли"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("coin create done send: %s", e)
        await q.answer()
        return
    await q.answer()


# ---------- эмитенты (админ) ----------

async def cmd_emitters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    ids = await db.emitter_list(chat.id)
    if not ids:
        await msg.reply_text("Эмитентов нет.")
        return
    lines = ["📜 <b>Эмитенты монет</b>"]
    for uid in ids:
        name = str(uid)
        try:
            c = await context.bot.get_chat(uid)
            name = c.first_name or c.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"• {escape(name)}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_emitter_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not _is_admin(user.id):
        await msg.reply_text("⛔ Только админ.")
        return
    import utils
    text = (msg.text or "")
    clean = re.sub(r"(?i)^\+Эмитент\s*", "", text).strip()
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажи @username или сделай реплай.")
        return
    uid, name = target
    ok = await db.emitter_add(chat.id, uid, user.id)
    if not ok:
        await msg.reply_text("⚠️ Не удалось выдать лицензию.")
        return
    await msg.reply_text(
        f"✅ {escape(str(name))} теперь эмитент.", parse_mode=ParseMode.HTML,
    )


async def cmd_emitter_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not _is_admin(user.id):
        await msg.reply_text("⛔ Только админ.")
        return
    import utils
    text = (msg.text or "")
    clean = re.sub(r"(?i)^[-–−]Эмитент\s*", "", text).strip()
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажи @username или сделай реплай.")
        return
    uid, name = target
    ok = await db.emitter_remove(chat.id, uid)
    if not ok:
        await msg.reply_text("⚠️ Не удалось снять лицензию.")
        return
    await msg.reply_text(
        f"✅ {escape(str(name))} больше не эмитент.", parse_mode=ParseMode.HTML,
    )


# ---------- архивирование монеты (админ) ----------

async def cmd_coin_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить (архивировать) монету по имени. Только админ."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not _is_admin(user.id):
        await msg.reply_text("⛔ Только админ.")
        return

    m = re.search(r"(?i)^УдалитьМонету\s+(\S+)", msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>УдалитьМонету ivangey</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    name = m.group(1)

    cid = await db.coin_find_by_name(chat.id, name)
    if not cid:
        await msg.reply_text(
            f"⚠️ Монета <b>{escape(name)}</b> не найдена.",
            parse_mode=ParseMode.HTML,
        )
        return

    rs = await db._exec(
        "UPDATE coins SET active = 0 WHERE id = ? AND chat_id = ?",
        [cid, chat.id],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        await msg.reply_text("⚠️ Не удалось архивировать.")
        return

    try:
        await db._exec(
            "UPDATE coin_market_lots SET active = 0 WHERE coin_id = ? AND active = 1",
            [cid],
        )
    except Exception as e:
        logger.warning("archive lots %s: %s", cid, e)

    await msg.reply_text(
        f"✅ Монета <b>{escape(name)}</b> архивирована (active=0).\n"
        f"<i>История сохранена, в списках и на рынке её больше нет.</i>",
        parse_mode=ParseMode.HTML,
    )
