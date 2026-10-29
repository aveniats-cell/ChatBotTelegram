"""Роли (Шериф/Комиссар), муты, баны, «Поймать».

Исправления:
  - ADMIN_ID валидируется на старте с громким логом (частая причина
    «админ не админ» — незаданный env ADMIN_ID на Render);
  - _require_role логирует отказ (видно в логах, почему админ не прошёл);
  - понятные сообщения при TelegramError: «бот не админ в чате» vs
    «нельзя замутить админа/владельца»;
  - защита от мут/бан самого себя и админа;
  - +Мут: корректный парсинг «@user 5ч причина» и «5ч @user причина»;
  - sheriff_catch не падает, если ADMIN_ID не задан.
"""
import os
import re
import time
import logging
from html import escape

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

if not ADMIN_ID:
    logger.error(
        "roles: ADMIN_ID не задан в переменных окружения — "
        "админ-команды (+Мут, +Бан, +Шериф и т.д.) работать не будут!"
    )

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

_UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


async def _require_role(update: Update, roles: tuple[str, ...]) -> bool:
    user = update.effective_user
    if not user:
        return False

    if _is_admin(user.id):
        return True

    try:
        role = await db.get_role(user.id)
    except Exception as e:
        logger.warning("get_role(%s): %s", user.id, e)
        return False

    ok = role in roles
    if not ok:
        logger.info(
            "require_role DENY: user_id=%s role=%r need=%s ADMIN_ID=%r",
            user.id, role, roles, ADMIN_ID,
        )
    return ok


async def _deny(update: Update, text: str = "⛔ Недостаточно прав.") -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(text)


def _fmt_tg_error(e: TelegramError) -> str:
    """Человеческое объяснение ошибки Telegram API."""
    s = str(e).lower()
    if "not enough rights" in s or "chat_admin_required" in s:
        return (
            "⚠️ У бота нет прав администратора в чате.\n"
            "Добавь бота в админы и включи право "
            "«Блокировать участников» (can_restrict_members)."
        )
    if "can't remove chat owner" in s or "chat_owner" in s:
        return "⚠️ Нельзя применить к владельцу чата."
    if "user is an administrator" in s:
        return "⚠️ Нельзя применить к администратору чата."
    if "user not found" in s or "peer_id_invalid" in s:
        return "⚠️ Пользователь не найден в этом чате."
    return f"⚠️ Ошибка Telegram: {escape(str(e))}"


async def _is_self_or_admin_target(
    context: ContextTypes.DEFAULT_TYPE, target_id: int, actor_id: int,
) -> str | None:
    """Возвращает текст ошибки, если цель — сам актор, админ или бот."""
    if target_id == actor_id:
        return "Себе это применить нельзя 🙂"
    if _is_admin(target_id):
        return "На админа это применить нельзя."
    if context.bot and target_id == context.bot.id:
        return "На бота это применить нельзя 🤖"
    return None


# ---------- +Шериф / -Шериф / +Комиссар / -Комиссар ----------

async def grant_sheriff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только админ или комиссар.")
        return
    msg = update.effective_message
    if not msg:
        return
    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.set_role(uid, "sheriff", update.effective_user.id)
    await msg.reply_text(f"✅ {escape(name)} назначен шерифом.")


async def revoke_sheriff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только админ или комиссар.")
        return
    msg = update.effective_message
    if not msg:
        return
    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.remove_role(uid)
    await msg.reply_text(f"✅ {escape(name)} больше не шериф.")


async def grant_commissar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not _is_admin(user.id):
        await _deny(update, "⛔ Только админ.")
        return
    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.set_role(uid, "commissar", user.id)
    await msg.reply_text(f"✅ {escape(name)} назначен комиссаром.")


async def revoke_commissar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not _is_admin(user.id):
        await _deny(update, "⛔ Только админ.")
        return
    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    await db.remove_role(uid)
    await msg.reply_text(f"✅ {escape(name)} больше не комиссар.")


async def list_sheriffs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    ids = await db.list_roles("sheriff")
    if not ids:
        await msg.reply_text("Шерифов пока нет.")
        return
    lines = []
    for uid in ids:
        try:
            chat = await context.bot.get_chat(uid)
            lines.append(f"• {escape(chat.first_name or str(uid))}")
        except TelegramError:
            lines.append(f"• {uid}")
    await msg.reply_text("👮 Шерифы:\n" + "\n".join(lines))


async def list_commissars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    ids = await db.list_roles("commissar")
    if not ids:
        await msg.reply_text("Комиссаров пока нет.")
        return
    lines = []
    for uid in ids:
        try:
            chat = await context.bot.get_chat(uid)
            lines.append(f"• {escape(chat.first_name or str(uid))}")
        except TelegramError:
            lines.append(f"• {uid}")
    await msg.reply_text("🎖 Комиссары:\n" + "\n".join(lines))


# ---------- Поймать ----------

async def sheriff_catch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("sheriff",)):
        await _deny(update, "⛔ Только шериф.")
        return

    msg = update.effective_message
    sheriff = update.effective_user
    chat = update.effective_chat
    if not msg or not sheriff or not chat:
        return

    if not ADMIN_ID:
        await msg.reply_text(
            "⚠️ Жалобы не настроены: ADMIN_ID не задан в env."
        )
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    err = await _is_self_or_admin_target(context, uid, sheriff.id)
    if err:
        await msg.reply_text(err)
        return

    text = (
        f"🚨 <b>Подозрение</b>\n"
        f"Шериф: {escape(sheriff.first_name or str(sheriff.id))}\n"
        f"На пользователя: {escape(name)} (id <code>{uid}</code>)\n\n"
        f"Принять — забанить в чате. Отклонить — оставить."
    )
    kb = InlineKeyboardMarkup([[ 
        InlineKeyboardButton("✅ Принять (бан)", callback_data=f"catch:accept:{uid}:{chat.id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"catch:reject:{uid}:{chat.id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except TelegramError as e:
        logger.warning("Не смог отправить запрос админу: %s", e)
        await msg.reply_text(
            "⚠️ Не смог связаться с админом. "
            "Возможно, админ не начинал диалог с ботом."
        )
        return
    await msg.reply_text("📨 Запрос отправлен админу.")


async def catch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    if not _is_admin(user.id):
        await q.answer("Только админ.", show_alert=True)
        return
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) != 4 or parts[0] != "catch":
        return
    _, action, uid_s, chat_s = parts
    try:
        uid, chat_id = int(uid_s), int(chat_s)
    except ValueError:
        await q.answer("Некорректные данные.", show_alert=True)
        return

    if action == "reject":
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n❌ <b>Отклонено</b> админом.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=uid)
        await db.add_punishment(uid, chat_id, "ban", None, user.id)
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n✅ <b>Принято</b>: пользователь забанен.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    except TelegramError as e:
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n" + _fmt_tg_error(e),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


# ---------- Мут / Бан ----------

def _parse_mute_args(rest: str) -> tuple[int, str | None, str]:
    """Разбирает 'хвост' команды +Мут.

    Возвращает (seconds, reason, text_for_target_resolve).
    Поддерживает оба порядка:
      '@user 5ч Богохульство'
      '5ч @user Богохульство'
      '1ч 30м @user Причина'
    """
    if not rest:
        return 0, None, ""

    uname_m = USERNAME_RE.search(rest)
    if uname_m:
        before = rest[:uname_m.start()].strip()
        after = rest[uname_m.end():].strip()

        seconds, time_rest = utils.parse_duration(after)
        if seconds:
            reason = (time_rest or "").strip() or None
            return seconds, reason, rest

        seconds, time_rest = utils.parse_duration(before)
        if seconds:
            # причина может быть в after, либо остаток before после времени
            reason_src = after or time_rest
            reason = (reason_src or "").strip() or None
            return seconds, reason, rest

        return 0, None, rest

    seconds, time_rest = utils.parse_duration(rest)
    reason = (time_rest or "").strip() or None
    return seconds, reason, rest


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return

    msg = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not msg or not actor or not chat:
        return

    raw = msg.text or ""
    parts = raw.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""

    seconds, reason, resolve_text = _parse_mute_args(rest)
    if not seconds or seconds <= 0:
        await msg.reply_text(
            "Не понял время. Пример: <code>+Мут 1ч 30м @user</code>, "
            "<code>+Мут @user 5ч Богохульство</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    target = await utils.resolve_target(update, context, resolve_text)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    err = await _is_self_or_admin_target(context, uid, actor.id)
    if err:
        await msg.reply_text(err)
        return

    until = int(time.time()) + seconds
    perms = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id, user_id=uid,
            permissions=perms, until_date=until,
        )
    except BadRequest as e:
        logger.warning("mute BadRequest: %s (bot_id=%s chat=%s target=%s)",
                       e, context.bot.id if context.bot else "?", chat.id, uid)
        await msg.reply_text(_fmt_tg_error(e))
        return
    except TelegramError as e:
        logger.warning("mute TelegramError: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return

    await db.add_punishment(uid, chat.id, "mute", until, actor.id)
    try:
        await db.inc_user_counter(actor.id, "mutes_issued", 1)
    except Exception as e:
        logger.warning("inc_user_counter mutes_issued: %s", e)

    reason_line = f"\nПричина: {escape(reason)}" if reason else ""
    await msg.reply_text(
        f"🔇 {escape(name)} замучен на {utils.fmt_duration(seconds)}."
        f"{reason_line}",
        parse_mode=ParseMode.HTML,
    )


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return

    msg = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not msg or not actor or not chat:
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id, user_id=uid, permissions=_UNMUTE_PERMS,
        )
    except BadRequest as e:
        logger.warning("unmute BadRequest: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return
    except TelegramError as e:
        logger.warning("unmute TelegramError: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return

    await db.deactivate_punishment(uid, chat.id, "mute")
    await msg.reply_text(f"🔊 {escape(name)} размучен.")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return

    msg = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not msg or not actor or not chat:
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    err = await _is_self_or_admin_target(context, uid, actor.id)
    if err:
        await msg.reply_text(err)
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=uid)
    except BadRequest as e:
        logger.warning("ban BadRequest: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return
    except TelegramError as e:
        logger.warning("ban TelegramError: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return

    await db.add_punishment(uid, chat.id, "ban", None, actor.id)
    await msg.reply_text(f"🚫 {escape(name)} забанен.")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_role(update, ("commissar",)):
        await _deny(update, "⛔ Только комиссар или админ.")
        return

    msg = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not msg or not actor or not chat:
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    try:
        await context.bot.unban_chat_member(
            chat_id=chat.id, user_id=uid, only_if_banned=True,
        )
    except BadRequest as e:
        logger.warning("unban BadRequest: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return
    except TelegramError as e:
        logger.warning("unban TelegramError: %s", e)
        await msg.reply_text(_fmt_tg_error(e))
        return

    await db.deactivate_punishment(uid, chat.id, "ban")
    await msg.reply_text(f"✅ {escape(name)} разбанен.")
