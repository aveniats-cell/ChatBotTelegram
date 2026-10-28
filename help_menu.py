"""Справка /help: разделы Общий / Шериф / Комиссар / Создатель.

Покрыты все команды, зарегистрированные в main.build_app().
"""
import os
import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# Каждый раздел: (заголовок, [строки]).
# Строки — обычный текст, экранируются при рендере.
_SECTIONS = {
    "general": ("📋 Общий", [
        "Профиль [@user] — профиль с кнопками разделов",
        "Аура — своя аура и место в топе",
        "Топ аура — топ-10 по ауре в чате",
        "дроч — +1 аура (не чаще раза в час)",
        "Топ все — топ чата за всё время",
        "Топ день — топ чата за сегодня",
        "Мое место — твоё место в топе сообщений",
        "Шип @u1 @u2 — шуточная пара",
        "Сделать предложение @user — предложение брака",
        "Мой брак — информация о своём браке",
        "-Брак — развод (свой или реплай)",
        "хо @user 50 — крестики-нолики 5×5 на монеты",
        "+Кредит 50 — взять кредит (1–100, раз в день)",
        "-Кредит 50 — погасить долг",
        "Перевод 50 @user — перевод монет (комиссия 3%)",
        "Казино — меню мини-казино",
        "Казино монетка 50 — 50/50, x1.92",
        "Казино кубик 4 50 — ставка на число 1–6, x5.5",
        "Казино слоты 50 — три x13, пара x1.4",
        "ИИ <запрос> — чат с ИИ (или реплай на сообщение бота)",
        "Браини / Мозг / Brainy <запрос> — то же самое",
        "/restart — сбросить контекст ИИ",
        "+Жалоба @user (или +ЖБ / +Жал) — жалоба админу",
        "Дать леща @user — лещ",
        "Поцеловать @user — поцелуй",
        "Обнять @user — объятие",
        "Побить @user — удар",
        "Обоссать @user — обоссать",
        "Погладить @user — погладить",
        "/help — эта справка",
        "/check — перепроверить подписку",
        "/ping — жив?",
        "/start — приветствие",
    ]),
    "sheriff": ("👮 Шериф", [
        "Поймать @user — запрос админу на бан",
    ]),
    "commissar": ("🎖 Комиссар", [
        "+Шериф / -Шериф @user — назначить/снять шерифа",
        "+Мут 5ч Причина @user — замутить",
        "+Мут @user 1ч 30м Причина — время и причина в любом порядке",
        "-Мут @user — размутить",
        "+Бан @user / -Бан @user — бан/разбан",
        "+Пред / +Предупреждение / +Варн @user — предупреждение",
        "-Пред / -Предупреждение / -Варн @user — снять предупреждение",
        "+Награда Название / 5 @user — выдать награду (1–6★)",
        "-Награда Название @user — снять награду",
    ]),
    "creator": ("👑 Создатель", [
        "+Комиссар / -Комиссар @user — назначить/снять комиссара",
        "Список шерифы — список шерифов",
        "Список комиссары — список комиссаров",
        "+Монеты 100 @user — начислить монеты",
        "-Монеты 100 @user — списать монеты (в казну)",
        "Казна — баланс казны чата",
        "+ССС @user / -ССС @user — состав ССС",
        "Список ССС — кто в ССС",
        "+Суд ССС @user — суд ССС (голосование)",
        "+Суд @user — публичный суд (голосование всех)",
        "+Награда Название / 10 @user — награда до 10★ (админ)",
        "/stats — админ-панель (текстом)",
    ]),
}


def _visible_sections(is_admin: bool, role: str | None) -> list[str]:
    out = ["general"]
    if is_admin or role == "commissar":
        out.append("sheriff")
        out.append("commissar")
    if is_admin:
        out.append("creator")
    return out


def _main_kb(sections: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_SECTIONS[s][0], callback_data=f"help:{s}")]
        for s in sections
    ])


def _section_kb(section: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="help:back")
    ]])


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
        try:
            await q.edit_message_text(
                "📖 <b>Справка</b>\nВыбери раздел:",
                parse_mode=ParseMode.HTML, reply_markup=_main_kb(sections),
            )
        except Exception as e:
            if "not modified" not in str(e).lower():
                logger.warning("help back edit: %s", e)
        await q.answer()
        return

    if section not in _SECTIONS or section not in sections:
        await q.answer("Раздел не найден.", show_alert=True)
        return

    title, lines = _SECTIONS[section]
    text = f"<b>{escape(title)}</b>\n" + "\n".join(
        f"• {escape(l)}" for l in lines
    )
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_section_kb(section),
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("help section edit: %s", e)
    await q.answer()
