"""Единая минималистичная навигация по реальным функциям бота."""
import logging
from html import escape

from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ui_design import InlineKeyboardButton

import db
import profile
import economy
import top
import aura
import wedding
import shop
import casino
import help_menu
import rules

logger = logging.getLogger(__name__)


def _keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Профиль", callback_data="home:profile"),
         InlineKeyboardButton("Баланс", callback_data="home:balance")],
        [InlineKeyboardButton("Магазин", callback_data="home:shop"),
         InlineKeyboardButton("Казино", callback_data="home:casino")],
        [InlineKeyboardButton("Рейтинг", callback_data="home:top"),
         InlineKeyboardButton("Аура", callback_data="home:aura")],
        [InlineKeyboardButton("Брак", callback_data="home:marriage"),
         InlineKeyboardButton("Справка", callback_data="home:help")],
        [InlineKeyboardButton("Правила", callback_data="home:rules"),
         InlineKeyboardButton("ИИ Ксения", callback_data="home:ai")],
    ])


def dashboard_text(user_name: str) -> str:
    return (
        f"👋 <b>Привет, {user_name}!</b>\n\n"
        "<b>PR BOT</b>  <i>Главное меню</i>\n\n"
        "Общайся, играй, развивай профиль и управляй балансом.\n"
        "<i>Выбери нужный раздел:</i>"
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not msg:
        return
    if user:
        await db.upsert_user(user.id, user.username, user.first_name or "")
    name = escape((user.first_name if user else None) or "друг")
    await msg.reply_text(
        dashboard_text(name), parse_mode=ParseMode.HTML, reply_markup=_keyboard()
    )


async def _run_action(query, update, context, action: str) -> bool:
    """Выполняет существующую команду через callback, не дублируя бизнес-логику."""
    routes = {
        "balance": economy.cmd_balance,
        "top": top.cmd_top_all,
        "aura": aura.cmd_aura,
        "marriage": wedding.cmd_marriage_info,
        "help": help_menu.cmd_help,
        "rules": rules.cmd_rules,
    }
    handler = routes.get(action)
    if handler is None:
        return False
    await query.answer()
    await handler(update, context)
    return True


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, user = update.callback_query, update.effective_user
    if not query or not user or not query.message:
        return

    action = (query.data or "").split(":", 1)[-1]
    chat_id = query.message.chat.id
    try:
        if action == "profile":
            body = await profile._render_base(context, chat_id, user.id)
            keyboard = profile._kb(user.id)
            await query.edit_message_text(
                body, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            await query.answer()
            return

        if action == "back":
            await query.edit_message_text(
                dashboard_text(escape(user.first_name or "друг")),
                parse_mode=ParseMode.HTML, reply_markup=_keyboard(),
            )
            await query.answer()
            return

        if action == "shop":
            await query.answer()
            await shop.open_shop_from_callback(query, user)
            return

        if action == "casino":
            await query.answer()
            await casino.open_casino_from_callback(query, user)
            return

        if action == "ai":
            await query.edit_message_text(
                "<b>ИИ Ксения</b>\n\n"
                "Напиши в чат сообщение, начиная с «Ксюша» или «Ксения».\n"
                "Например: <code>Ксюша, придумай идею для игры</code>.\n\n"
                "Сбросить контекст: /restart",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Назад в меню", callback_data="home:back")
                ]]),
            )
            await query.answer()
            return

        if await _run_action(query, update, context, action):
            return

        await query.answer("Раздел не найден.", show_alert=True)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            logger.exception("home_callback failed: %s", exc)
            try:
                await query.answer("Не удалось открыть раздел. Попробуй ещё раз.", show_alert=True)
            except TelegramError:
                pass
    except Exception:
        logger.exception("home_callback unexpected error: action=%s", action)
        try:
            await query.answer("Раздел временно недоступен.", show_alert=True)
        except TelegramError:
            pass
