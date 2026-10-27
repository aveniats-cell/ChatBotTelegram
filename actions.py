"""Действия: лещ, поцеловать, обнять, побить, обоссать, погладить.

Все команды доступны всем участникам, работают по @username или реплаю.
"""
import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)


_TEMPLATES = {
    "slap": [
        "👋 {a} отвешивает звонкого леща {b}.",
        "👋 {a} даёт леща {b}. Звонко!",
    ],
    "kiss": [
        "💋 {a} целует {b}.",
        "💋 {a} нежно целует {b}.",
    ],
    "hug": [
        "🤗 {a} обнимает {b}.",
        "🤗 {a} крепко обнимает {b}.",
    ],
    "hit": [
        "👊 {a} бьёт {b}.",
        "👊 {a} отвешивает {b} хороший удар.",
    ],
    "piss": [
        "💦 {a} обоссал(а) {b}.",
        "💦 {a} решил(а) обоссать {b}.",
    ],
    "pat": [
        "🫳 {a} гладит {b} по голове.",
        "🫳 {a} ласково гладит {b}.",
    ],
}


async def _do_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str,
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text(
            "Укажите @username или сделайте реплай.",
        )
        return
    uid, name = target

    if uid == user.id:
        await msg.reply_text("С собой так нельзя 🙂")
        return

    a = user.first_name or user.username or str(user.id)
    template = _TEMPLATES[kind][0]
    text = template.format(a=escape(a), b=escape(name))

    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_slap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "slap")


async def cmd_kiss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "kiss")


async def cmd_hug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "hug")


async def cmd_hit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "hit")


async def cmd_piss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "piss")


async def cmd_pat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_action(update, context, "pat")
