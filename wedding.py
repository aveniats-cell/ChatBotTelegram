"""2.5 Свадьба: предложение, принятие/отклонение, расторжение.

Исправления:
- accept/reject проверяют, что proposal ещё active=0 (нет двойных «Свадьба!»).
- accept атомарно проверяет отсутствие активного брака у обоих участников
  (нет «двух браков одновременно»).
- delete_marriage возвращает rows_affected — не спамим «отклонено» после
  уже обработанного proposal.
- кнопки проверяют, что callback пришёл из того же чата, где proposal.
- _marriage_level считает от married_at (момент accept), а не created_at.
- +Брак не женит ботов и админа. Принудительный брак создаётся как
  предложение (forced=1, active=0) и АКТИВИРУЕТСЯ через accept_marriage —
  иначе активного брака не будет (регрессия исправлена).
- При ошибке БД показываем «ошибка», а не «не найдено».
"""
import os
import re
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
import resolver

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, arg_text: str):
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or (u.username or str(u.id))
    m = USERNAME_RE.search(arg_text)
    if not m:
        return None
    uname = m.group(1)
    uid = await db.get_user_by_username(uname)
    if uid:
        return uid, uname
    r = await resolver.resolve_username(uname)
    if r:
        return r
    try:
        chat = await context.bot.get_chat(f"@{uname}")
        return chat.id, chat.first_name or uname
    except TelegramError as e:
        logger.warning("get_chat @%s: %s", uname, e)
    return None


async def _resolve_two(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    targets = []
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        targets.append((u.id, u.first_name or u.username or str(u.id)))
    for uname in USERNAME_RE.findall(text):
        if len(targets) >= 2:
            break
        uid = await db.get_user_by_username(uname)
        if uid:
            targets.append((uid, uname))
            continue
        r = await resolver.resolve_username(uname)
        if r:
            targets.append(r)
            continue
        try:
            c = await context.bot.get_chat(f"@{uname}")
            targets.append((c.id, c.first_name or uname))
        except TelegramError as e:
            logger.warning("get_chat @%s: %s", uname, e)
    return targets


def _marriage_level(married_at_str: str) -> int:
    """Уровень брака от момента accept (married_at), а не от proposal."""
    try:
        s = (married_at_str or "").replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        created = datetime.fromisoformat(s)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        return max(1, delta.days + 1)
    except Exception:
        return 1


async def _is_bot(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    try:
        chat = await context.bot.get_chat(uid)
        return bool(getattr(chat, "is_bot", False))
    except TelegramError:
        return False


# ---------- предложение ----------

async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

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
    if await _is_bot(context, target_id):
        await msg.reply_text("Ботам предложение делать нельзя 🤖")
        return

    other = await db.find_active_marriage(chat.id, target_id)
    if other:
        await msg.reply_text("💔 Этот пользователь уже в браке.")
        return

    mid = await db.create_proposal(chat.id, user.id, target_id)
    if not mid:
        logger.warning("cmd_propose: create_proposal вернул 0")
        await msg.reply_text("⚠️ Не удалось создать предложение. Попробуйте ещё раз.")
        return

    logger.info("propose: chat=%s mid=%s user1=%s user2=%s",
                chat.id, mid, user.id, target_id)

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


# ---------- callback кнопок ----------

async def wedding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    user = update.effective_user
    if not user:
        return

    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "wed":
        await q.answer()
        return

    _, action, mid_s = parts
    try:
        mid = int(mid_s)
    except ValueError:
        await q.answer("Некорректный id.", show_alert=True)
        return

    logger.info("wed callback: data=%r mid=%r user=%s", q.data, mid, user.id)

    m = await db.get_marriage(mid)
    if not m:
        await q.answer("Предложение не найдено или устарело.", show_alert=True)
        return

    # callback должен прийти из того же чата, где proposal
    if q.message and q.message.chat and q.message.chat.id != m["chat_id"]:
        await q.answer("Это предложение из другого чата.", show_alert=True)
        return

    if user.id != m["user2_id"]:
        await q.answer("Это предложение не вам.", show_alert=True)
        return

    # уже обработано (accept/reject) — не спамим
    if m["active"]:
        await q.answer("Это предложение уже принято.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    if action == "reject":
        affected = await db.delete_marriage(mid)
        if affected == 0:
            # гонка: кто-то уже удалил/принял
            await q.answer("Это предложение уже обработано.", show_alert=True)
            return
        await q.answer("Отклонено")
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
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n❌ <b>Отклонено.</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("edit reject msg: %s", e)
        return

    if action != "accept":
        await q.answer()
        return

    affected = await db.accept_marriage(mid)
    if affected == 0:
        # либо уже принято, либо у кого-то из участников появился другой брак
        m2 = await db.get_marriage(mid)
        if m2 and m2["active"]:
            await q.answer("Это предложение уже принято.", show_alert=True)
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        else:
            await q.answer(
                "Нельзя: у одного из вас уже есть активный брак.",
                show_alert=True,
            )
        return

    await db.log_marriage_event(
        m["chat_id"], m["user1_id"], m["user2_id"], "married",
        by_user_id=m["user2_id"],
    )
    await q.answer("Брак заключён!")

    m_after = await db.get_marriage(mid)
    level = _marriage_level(m_after["married_at"] if m_after else m["created_at"])

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
    try:
        await q.edit_message_text(
            (q.message.text or "") + "\n\n✅ <b>Зарегистрировано!</b>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("edit accept msg: %s", e)


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
    if m["forced"]:
        await msg.reply_text(
            "🔒 Этот брак принудительный. Расторгнуть его может только админ."
        )
        return
    affected = await db.delete_marriage(m["id"])
    if affected == 0:
        await msg.reply_text("Брак уже расторгнут.")
        return
    await db.log_marriage_event(
        chat.id, m["user1_id"], m["user2_id"], "divorced", by_user_id=user.id,
    )
    await msg.reply_text(
        f"💔 Брак расторгнут. {escape(user.first_name or 'Пользователь')} "
        f"снова свободен(а).",
        parse_mode=ParseMode.HTML,
    )


# ---------- принудительный брак ----------

async def cmd_force_marriage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    targets = await _resolve_two(update, context, msg.text or "")
    if len(targets) < 2:
        await msg.reply_text(
            "Формат: <code>+Брак @u1 @u2</code> или реплай + "
            "<code>+Брак @u2</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    (id1, name1), (id2, name2) = targets[0], targets[1]
    if id1 == id2:
        await msg.reply_text("Это один и тот же человек 🙂")
        return
    if id1 == ADMIN_ID or id2 == ADMIN_ID:
        await msg.reply_text("Админа женить нельзя.")
        return
    if await _is_bot(context, id1) or await _is_bot(context, id2):
        await msg.reply_text("Ботов женить нельзя 🤖")
        return

    for uid in (id1, id2):
        m = await db.find_active_marriage(chat.id, uid)
        if m:
            affected = await db.delete_marriage(m["id"])
            if affected:
                await db.log_marriage_event(
                    chat.id, m["user1_id"], m["user2_id"], "forced_divorced",
                    by_user_id=user.id,
                )

    mid = await db.create_proposal(chat.id, id1, id2, forced=True)
    if not mid:
        logger.warning("cmd_force_marriage: create_proposal вернул 0")
        await msg.reply_text("⚠️ Не удалось создать брак. Попробуйте ещё раз.")
        return

    # ВАЖНО: create_proposal создаёт запись с active=0. Принудительный брак
    # активируется тем же атомарным accept_marriage, что и обычный — иначе
    # активного брака в БД не будет.
    affected = await db.accept_marriage(mid)
    if affected == 0:
        logger.warning("cmd_force_marriage: accept_marriage(%s) вернул 0", mid)
        await db.delete_marriage(mid)
        await msg.reply_text(
            "⚠️ Не удалось активировать брак (возможно, у одного из "
            "участников уже есть активный брак)."
        )
        return

    await db.log_marriage_event(chat.id, id1, id2, "forced_married", by_user_id=user.id)

    await msg.reply_text(
        f"💍 <b>Принудительная свадьба!</b>\n"
        f"{escape(name1)} + {escape(name2)} = ❤️\n\n"
        f"<i>Брак заключён волей админа. Уровень: 1</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- принудительный развод ----------

async def cmd_force_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    target = await _resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    m = await db.find_active_marriage(chat.id, uid)
    if not m:
        await msg.reply_text("Этот пользователь не в браке.")
        return
    affected = await db.delete_marriage(m["id"])
    if affected == 0:
        await msg.reply_text("Брак уже расторгнут.")
        return
    await db.log_marriage_event(
        chat.id, m["user1_id"], m["user2_id"], "forced_divorced",
        by_user_id=user.id,
    )
    await msg.reply_text(
        f"💔 Админ принудительно расторг брак. {escape(name)} снова свободен(а).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_force_divorce_or_self(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    text = msg.text or ""
    has_target = bool(msg.reply_to_message or USERNAME_RE.search(text))
    if ADMIN_ID and user.id == ADMIN_ID and has_target:
        return await cmd_force_divorce(update, context)
    return await cmd_divorce(update, context)


# ---------- инфо ----------

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
    level = _marriage_level(m["married_at"] or m["created_at"])
    forced = " 🔒 (принудительный)" if m["forced"] else ""
    await msg.reply_text(
        f"💍 Ваш брак с <b>{escape(name)}</b>{forced}\n"
        f"Уровень: <b>{level}</b>\n"
        f"Заключён: {escape(m['married_at'] or m['created_at'])}",
        parse_mode=ParseMode.HTML,
    )
