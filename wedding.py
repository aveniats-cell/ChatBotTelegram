"""2.5 Свадьба: предложение, принятие/отклонение, расторжение, «уровень» брака."""
import os
import logging
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)

USERNAME_RE = __import__("re").compile(r"@([A-Za-z0-9_]{5,32})")


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, arg_text: str):
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or (u.username or str(u.id))
    m = USERNAME_RE.search(arg_text)
    if m:
        uname = m.group(1)
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            return chat.id, chat.first_name or uname
        except TelegramError as e:
            logger.warning("get_chat @%s: %s", uname, e)
    return None


def _marriage_level(created_at_str: str) -> int:
    """Уровень брака = полные дни с момента создания + 1."""
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        return max(1, delta.days + 1)
    except Exception:
        return 1


# ---------- предложение ----------

async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    # уже в браке?
    existing = await db.find_active_marriage(chat.id, user.id)
    if existing:
        await msg.reply_text("💍 Вы уже в браке. Сначала расторгните текущий.")
        return

    target = await _resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text(
            "Формат: <code>Сделать предложение @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    target_id, target_name = target
    if target_id == user.id:
        await msg.reply_text("Себе предложение делать нельзя 🙂")
        return

    other = await db.find_active_marriage(chat.id, target_id)
    if other:
        await msg.reply_text("💔 Этот пользователь уже в браке.")
        return

    mid = await db.create_proposal(chat.id, user.id, target_id)

    text = (
        f"💐 <b>Минуточку внимания!</b>\n\n"
        f"💍 {escape(user.first_name or str(user.id))} делает предложение "
        f"{escape(target_name)}!\n\n"
        f"Согласны ли вы, {escape(target_name)}, связать судьбу с "
        f"{escape(user.first_name or 'этим человеком')}?\n\n"
        f"<i>(решение принимает только {escape(target_name)})</i>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Зарегистрировать", callback_data=f"wed:accept:{mid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"wed:reject:{mid}"),
    ]])
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------- callback ----------

async def wedding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    user = update.effective_user
    if not user:
        return
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "wed":
        await q.answer()
        return
    _, action, mid_s = parts
    mid = int(mid_s)
    m = await db.get_marriage(mid)
    if not m:
        await q.answer("Предложение не найдено.", show_alert=True)
        return

    # нажимать может только тот, кому предложили (user2)
    if user.id != m["user2_id"]:
        await q.answer("Это предложение не вам.", show_alert=True)
        return

    if action == "reject":
        await db.delete_marriage(mid)
        await q.answer("Отклонено")
        # грустное сообщение в группу от лица отвергнутого
        try:
            await context.bot.send_message(
                chat_id=m["chat_id"],
                text=(
                    f"💔 {escape(user.first_name or 'Пользователь')} отклонил(а) "
                    f"предложение. Сердечко разбито."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("send reject msg: %s", e)
        await q.edit_message_text(
            q.message.text + "\n\n❌ <b>Отклонено.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # accept
    await db.accept_marriage(mid)
    await q.answer("Брак заключён!")
    level = _marriage_level(m["created_at"])
    try:
        await context.bot.send_message(
            chat_id=m["chat_id"],
            text=(
                f"🎉 <b>Свадьба!</b>\n"
                f"Поздравляем новую пару! 💍\n"
                f"Уровень брака: <b>{level}</b> (растёт каждый день)"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("send accept msg: %s", e)
    await q.edit_message_text(
        q.message.text + "\n\n✅ <b>Зарегистрировано!</b>",
        parse_mode=ParseMode.HTML,
    )


# ---------- расторжение ----------

async def cmd_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    m = await db.find_active_marriage(chat.id, user.id)
    if not m:
        await msg.reply_text("Вы не в браке.")
        return
    await db.delete_marriage(m["id"])
    await msg.reply_text(
        f"💔 Брак расторгнут. {escape(user.first_name or 'Пользователь')} "
        f"снова свободен(а).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_marriage_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    m = await db.find_active_marriage(chat.id, user.id)
    if not m:
        await msg.reply_text("Вы не в браке.")
        return
    partner_id = m["user1_id"] if m["user2_id"] == user.id else m["user2_id"]
    name = str(partner_id)
    try:
        partner = await context.bot.get_chat(partner_id)
        name = partner.first_name or partner.username or str(partner_id)
    except TelegramError:
        pass
    level = _marriage_level(m["created_at"])
    await msg.reply_text(
        f"💍 Ваш брак с <b>{escape(name)}</b>\n"
        f"Уровень: <b>{level}</b>\n"
        f"Заключён: {escape(m['created_at'])}",
        parse_mode=ParseMode.HTML,
    )
