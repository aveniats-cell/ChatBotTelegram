"""Справка /help: разделы Общий / Шериф / Комиссар / Создатель."""
import os
import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

_SECTIONS = {
    "general": ("📋 Общий", [
        "Профиль — профиль (кнопки разделов)",
        "Аура / Топ аура / дроч — аурометр",
        "Топ все / Топ день / Мое место",
        "Шип @u1 @u2",
        "Сделать предложение / Мой брак / -Брак",
        "хо @user 50 — крестики-нолики",
        "+Кредит 50 / -Кредит 50",
        "Казино — мини-казино на монеты",
        "ИИ <запрос> — чат с ИИ (или реплай на сообщение бота)",
        "/restart — сбросить контекст ИИ",
        "+Жалоба @user",
        "Дать леща / Поцеловать / Обнять / Побить / Обоссать / Погладить",
    ]),
    "sheriff": ("👮 Шериф", [
        "Поймать @user — запрос админу на бан",
    ]),
    "commissar": ("🎖 Комиссар", [
        "+Шериф / -Шериф @user",
        "+Мут 5ч Причина @user / -Мут @user",
        "+Бан @user / -Бан @user",
        "+Пред / -Пред @user",
        "+Награда Название / 5 @user",
        "-Награда Название @user",
    ]),
    "creator": ("👑 Создатель", [
        "+Комиссар / -Комиссар @user",
        "Список шерифы / Список комиссары",
        "+Монеты 100 @user",
        "+ССС @user / -ССС @user / Список ССС",
        "/stats — админ-панель",
    ]),
}


def _visible_sections(is_admin: bool, role: str | None) -> list[str]:
    out = ["general"]
    if is_admin or role == "commissar":
        out.append("sheriff")
    if is_admin or role == "commissar":
        out.append("commissar")
    if is_admin:
        out.append("creator")
    return out


def _main_kb(sections: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_SECTIONS[s][0], callback_data=f"help:{s}")]
        for s in sections
    ])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    is_admin = bool(ADMIN_ID) and user.id == ADMIN_ID
    try:
        role = await db.get_role(user.id)
    except Exception as e:
        logger.warning("cmd_help get_role: %s", e)
        role = None
    sections = _visible_sections(is_admin, role)
    await msg.reply_text(
        "📖 <b>Справка</b>\nВыбери раздел:",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(sections),
    )


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 2 or parts[0] != "help":
        await q.answer()
        return
    section = parts[1]

    is_admin = bool(ADMIN_ID) and user.id == ADMIN_ID
    try:
        role = await db.get_role(user.id)
    except Exception as e:
        logger.warning("help_callback get_role: %s", e)
        role = None
    sections = _visible_sections(is_admin, role)

    if section == "back":
        await q.edit_message_text(
            "📖 <b>Справка</b>\nВыбери раздел:",
            parse_mode=ParseMode.HTML, reply_markup=_main_kb(sections),
        )
        await q.answer()
        return

    if section not in _SECTIONS or section not in sections:
        await q.answer("Раздел не найден.", show_alert=True)
        return

    title, lines = _SECTIONS[section]
    text = f"<b>{escape(title)}</b>\n" + "\n".join(
        f"• {escape(l)}" for l in lines
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="help:back")
    ]])
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("help_callback edit: %s", e)
    await q.answer()
