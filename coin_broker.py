"""Панель брокера: портфель + кнопка «Создать монету» (только создатель/ССС).

FIX #2: кнопка «Создать монету» скрывается, если у юзера уже есть
активная сессия создания (_PENDING_CREATE).
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
import sss
import coins

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")


def _is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


def _is_sss(uid: int) -> bool:
    try:
        return _is_admin(uid) or sss.is_sss_member(uid)
    except Exception:
        return _is_admin(uid)


async def cmd_broker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    # ПРИВАТ: панель брокера только для создателя (ADMIN_ID).
    if not _is_admin(user.id):
        try:
            await msg.reply_text("⛔ Панель брокера доступна только создателю.")
        except TelegramError:
            pass
        logger.info("broker DENY: user_id=%s chat=%s", user.id, chat.id)
        return

    bought = await db.coin_holders_by_user(chat.id, user.id)
    total_value = 0
    total_invested = 0
    lines = ["💼 <b>Панель брокера</b>\n", "<b>Активы:</b>"]
    if not bought:
        lines.append("• —")
    else:
        for h in bought:
            coin = await db.coin_get(h["coin_id"])
            if not coin:
                continue
            cp = coins._current_price(coin)
            qty = int(h["quantity"])
            val = cp * qty
            inv = int(h["invested"])
            total_value += val
            total_invested += inv
            pnl = val - inv
            sign = "📈" if pnl >= 0 else "📉"
            lines.append(
                f"• {escape(coin['name'])}: <b>{qty}</b> шт · "
                f"цена <b>{cp}</b>💰 · стоимость <b>{val}</b>💰 · "
                f"{sign} P&L <b>{pnl:+d}</b>💰"
            )
    pnl_total = total_value - total_invested
    lines.append("")
    lines.append(f"Вложено: <b>{total_invested}</b>💰")
    lines.append(f"Текущая стоимость: <b>{total_value}</b>💰")
    lines.append(f"P&L: <b>{pnl_total:+d}</b>💰")

    rows = [[InlineKeyboardButton("🎒 Мои коины", callback_data="coin:mine")]]
    # ── FIX #2: не показываем «Создать», если сессия уже идёт
    if _is_sss(user.id) and not coins._PENDING_CREATE.get(user.id):
        rows.append([InlineKeyboardButton("➕ Создать монету", callback_data="coin:new")])
    rows.append([InlineKeyboardButton("🪙 Магазин коинов", callback_data="coin:main")])

    try:
        await msg.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError as e:
        logger.warning("broker send: %s", e)
