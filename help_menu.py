"""Справка /help: разделы Общий / Шериф / Комиссар / Создатель.

Покрыты все команды, зарегистрированные в main.build_app(),
включая новые действия, суды с причиной и опросы.
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
        "+Кредит 50 — взять кредит из казны (1–100, раз в день)",
        "-Кредит 50 — погасить долг в казну",
        "Перевод 50 @user — перевод монет (комиссия 3%)",
        "Казино — меню мини-казино",
        "Казино монетка орёл 50 — ставка на сторону, x1.92",
        "Казино монетка решка 50 — ставка на сторону, x1.92",
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
        "Засос @user — засос",
        "Дать по шапке @user — по шапке",
        "Выстрел @user — выстрел",
        "Расцеловать @user — расцеловать",
        "Сделать 18+ @user — 18+ действие",
        "Станцевать @user — танец",
        "Обнять всех @user — объятие для всех",
        "Погладить всех @user — погладить всех",
        "Лещей всем @user — лещей всем",
        "Тыкнуть @user — тык",
        "Защекотать @user — щекотка",
        "+Суд @user / Причина — публичный суд с причиной",
        "+Суд @user — публичный суд без причины",
        "Опрос / описание /1/ В1 / В2 — публичный опрос (5 мин)",
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
        "+Суд ССС @user / Причина — суд ССС с причиной",
        "+Суд ССС @user — суд ССС без причины",
        "Опрос ССС / описание /1/ В1 / В2 — опрос для ССС (5 мин)",
    ]),
    "creator": ("👑 Создатель", [
        "+Комиссар / -Комиссар @user — назначить/снять комиссара",
        "Список шерифы — список шерифов",
        "Список комиссары — список комиссаров",
        "Расстрел @user — расстрел (только админ)",
        "+Монеты 100 @user — начислить монеты (из воздуха)",
        "-Монеты 100 @user — списать монеты (в казну)",
        "+Утил 100 @user — начислить монеты (из воздуха)",
        "-Утил 100 @user — сжечь монеты у юзера",
        "+Утил 100 казна — начислить в казну",
        "-Утил 100 казна — сжечь из казны",
        "Казна — баланс казны чата",
        "+ВКассу 500 — добавить монеты в казну",
        "ОбрезатьКасну 15000 — сжечь лишнее из казны (оставить N)",
        "Зарплата — инфо о зарплате за сегодня",
        "Топ богачи — топ-20 по балансу",
        "+ССС @user / -ССС @user — состав ССС",
        "Список ССС — кто в ССС",
        "+Суд ССС @user / Причина — суд ССС с причиной",
        "+Суд @user / Причина — публичный суд с причиной",
        "Опрос ССС / описание /1/ В1 / В2 — опрос для ССС (5 мин)",
        "Опрос / описание /1/ В1 / В2 — публичный опрос (5 мин)",
        "+Награда Название / 10 @user — награда до 10★ (админ)",
        "/ALLMONEY 100 — начислить ВСЕМ по 100 монет ИЗ КАЗНЫ",
        "/Defloult220099 — экстремальный дефолт (обнулить все балансы)",
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
