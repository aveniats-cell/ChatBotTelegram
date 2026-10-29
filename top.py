"""2.5 Свадьба: предложение, принятие/отклонение, расторжение, топ браков.

Уровень = дни_с_свадьбы + 1 + xp // XP_PER_LEVEL.
XP даётся действиями пары (см. marriage_xp.py).
Топ браки — по эффективному уровню (активные обгоняют пассивных старых).
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

if not ADMIN_ID:
    logger.error(
        "wedding: ADMIN_ID не задан — админ-команды (+Брак, -Брак @user) "
        "работать не будут!"
    )

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

# Должно совпадать с marriage_xp.XP_PER_LEVEL и db.XP_PER_LEVEL.
XP_PER_LEVEL = 50


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def _marriage_level(married_at_str: str | None, xp: int = 0) -> int:
    base = 1
    if married_at_str:
        try:
            s = str(married_at_str).replace("Z", "+00:00")
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            created = datetime.fromisoformat(s)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - created
            base = max(1, delta.days + 1)
        except Exception:
            base = 1
    return base + max(0, int(xp or 0)) // XP_PER_LEVEL


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, arg_text: str):
    msg = update.effective_message
    if not msg:
        return None
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
    if not msg:
        return targets
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


async def _is_bot(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    try:
        chat = await context.bot.get_chat(uid)
        return bool(getattr(chat, "is_bot", False))
    except TelegramError:
        return False


async def _name_of(context: ContextTypes.DEFAULT_TYPE, uid: int) -> str:
    try:
        c = await context.bot.get_chat(uid)
        return c.first_name or c.username or str(uid)
    except TelegramError:
        return str(uid)


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

    if q.message is None or not q.message.chat:
        await q.answer("Сообщение устарело.", show_alert=True)
        return
    if q.message.chat.id != m["chat_id"]:
        await q.answer("Это предложение из другого чата.", show_alert=True)
        return

    if user.id != m["user2_id"]:
        await q.answer("Это предложение не вам.", show_alert=True)
        return

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
    xp = await db.marriage_get_xp(m["chat_id"], m["user1_id"])
    level = _marriage_level(
        (m_after or {}).get("married_at") or (m_after or {}).get("created_at"),
        xp,
    )

    try:
        await context.bot.send_message(
            chat_id=m["chat_id"],
            text=(
                f"🎉 <b>Свадьба!</b>\n"
                f"Поздравляем новую пару! 💍\n"
                f"Уровень брака: <b>{level}</b> "
                f"(растёт от дней и от действий пары)"
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

    if m["forced"] and not _is_admin(user.id):
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


async def cmd_force_marriage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not _is_admin(user.id):
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

    if _is_admin(id1) and id1 != user.id:
        await msg.reply_text("Админа нельзя женить насильно против его воли.")
        return
    if _is_admin(id2) and id2 != user.id:
        await msg.reply_text("Админа нельзя женить насильно против его воли.")
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


async def cmd_force_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not _is_admin(user.id):
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
    if _is_admin(user.id) and has_target:
        return await cmd_force_divorce(update, context)
    return await cmd_divorce(update, context)


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
    name = await _name_of(context, partner_id)
    xp = await db.marriage_get_xp(chat.id, user.id)
    level = _marriage_level(m.get("married_at") or m.get("created_at"), xp)
    rank = await db.marriage_rank(chat.id, user.id)
    rank_line = ""
    if rank:
        r, total = rank
        rank_line = f"\nМесто в топе браков: <b>{r}/{total}</b>"
    forced = " 🔒 (принудительный)" if m["forced"] else ""
    when = m.get("married_at") or m.get("created_at") or "—"
    to_next = XP_PER_LEVEL - (xp % XP_PER_LEVEL)
    await msg.reply_text(
        f"💍 Ваш брак с <b>{escape(name)}</b>{forced}\n"
        f"Уровень: <b>{level}</b>\n"
        f"Опыт: <b>{xp}</b> XP · до след. уровня: <b>{to_next}</b> XP\n"
        f"Заключён: {escape(str(when))}"
        f"{rank_line}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_marriage_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    rows = await db.marriage_top(chat.id, limit=20)
    if not rows:
        await msg.reply_text("В этом чате пока нет активных браков.")
        return

    lines = ["💍 <b>Топ браки</b> (активные обгоняют старых)"]
    for i, m in enumerate(rows, start=1):
        n1 = await _name_of(context, m["user1_id"])
        n2 = await _name_of(context, m["user2_id"])
        level = _marriage_level(m.get("married_at") or m.get("created_at"), m["xp"])
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lock = " 🔒" if m["forced"] else ""
        lines.append(
            f"{medal} {escape(n1)} + {escape(n2)}{lock} — "
            f"ур. <b>{level}</b> · <b>{m['xp']}</b> XP"
        )
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
