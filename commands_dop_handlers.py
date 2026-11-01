"""Обработчики динамических команд из commands_dop.py.

Бесплатные команды: триггер -> шаблон.
Платные: триггер -> шаблон, только если пользователь купил пакет.
Если у команды задан marriage_xp, XP начисляется только при использовании
команды на действующем супруге/супруге автора в этом чате.
"""
import logging
import re
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters

import db
import utils
import command_registry
import marriage_xp

logger = logging.getLogger(__name__)


def _fmt_template(template: str, actor_name: str, target_name: str) -> str:
    return template.replace("user1", escape(actor_name)).replace(
        "user2", escape(target_name)
    )


async def _resolve_target(update, context, text_after_trigger: str):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return None
    target = await utils.resolve_target(update, context, text_after_trigger)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return None
    target_id, target_name = target
    if target_id == user.id:
        await msg.reply_text("Нельзя применить к себе 🙂")
        return None
    if context.bot and target_id == context.bot.id:
        await msg.reply_text("На бота нельзя 🙂")
        return None
    actor_name = user.first_name or user.username or str(user.id)
    return user.id, actor_name, target_id, target_name


async def _award_custom_marriage_xp(update, cmd: dict,
                                    actor_id: int, target_id: int) -> None:
    amount = int(cmd.get("marriage_xp") or 0)
    if amount <= 0:
        return
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    awarded = await marriage_xp.award_custom_if_spouse(
        chat.id, actor_id, target_id, amount,
    )
    if awarded:
        try:
            await msg.reply_text(f"💍 Вашему браку начислено +{awarded} XP.")
        except TelegramError as e:
            logger.warning("dop marriage xp notice: %s", e)


async def _handle_free(update, context, cmd: dict, clean_text: str):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    resolved = await _resolve_target(update, context, clean_text)
    if not resolved:
        return
    actor_id, actor_name, target_id, target_name = resolved
    text = _fmt_template(cmd["template"], actor_name, target_name)
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("dop free send: %s", e)
        return
    try:
        await db.inc_action_stat(chat.id, actor_id, cmd["trigger"], 1)
    except Exception as e:
        logger.warning("dop free stat: %s", e)
    await _award_custom_marriage_xp(update, cmd, actor_id, target_id)


async def _handle_buy(update, context, cmd: dict, clean_text: str):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    pocket_id = cmd["pocket_id"]
    has = await db.shop_user_has_pocket(chat.id, user.id, pocket_id)
    if not has:
        pocket = await db.shop_pocket_get(pocket_id)
        price = int((pocket or {}).get("price") or 0)
        name = (pocket or {}).get("name") or f"id={pocket_id}"
        await msg.reply_text(
            f"⛔ Команда из пакета «{escape(name)}».\n"
            f"Купи пакет в <b>Магазине</b> за <b>{price}</b>🪙.",
            parse_mode=ParseMode.HTML,
        )
        return
    resolved = await _resolve_target(update, context, clean_text)
    if not resolved:
        return
    actor_id, actor_name, target_id, target_name = resolved
    text = _fmt_template(cmd["template"], actor_name, target_name)
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("dop buy send: %s", e)
        return
    try:
        await db.inc_action_stat(chat.id, actor_id, cmd["trigger"], 1)
    except Exception as e:
        logger.warning("dop buy stat: %s", e)
    await _award_custom_marriage_xp(update, cmd, actor_id, target_id)


def _make_handler(cmd: dict):
    trigger_re = re.compile(rf"(?i)^{re.escape(cmd['trigger'])}(\s|$)")

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg:
            return
        text = msg.text or ""
        clean = trigger_re.sub("", text).strip()
        if cmd["free"]:
            await _handle_free(update, context, cmd, clean)
        else:
            await _handle_buy(update, context, cmd, clean)

    return trigger_re, handler


async def sync_to_db() -> None:
    """Синхронизировать пакеты и команды из реестра с БД магазина."""
    for pocket in command_registry.get_pockets():
        try:
            await db.shop_pocket_upsert(
                pocket["id"], pocket["name"], pocket["price"],
            )
        except Exception as e:
            logger.warning("shop pocket upsert %s: %s", pocket["id"], e)
    for cmd in command_registry.get_commands():
        try:
            await db.shop_command_add(
                cmd["trigger"], cmd["template"], cmd["pocket_id"], cmd["free"],
            )
        except Exception as e:
            logger.warning("shop command add %s: %s", cmd["trigger"], e)


def register(app) -> None:
    """Зарегистрировать все команды из реестра в PTB."""
    for cmd in command_registry.get_commands():
        try:
            trigger_re, handler = _make_handler(cmd)
            app.add_handler(MessageHandler(filters.Regex(trigger_re), handler))
            logger.info(
                "dop command registered: %s (marriage_xp=%s)",
                cmd["trigger"], cmd.get("marriage_xp", 0),
            )
        except Exception as e:
            logger.warning("dop register %s: %s", cmd.get("trigger"), e)
