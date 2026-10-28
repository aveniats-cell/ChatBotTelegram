"""ССС и суды.

Исправления:
  - при финализации суда исходное сообщение редактируется (кнопки убираются);
  - callback на завершённый суд отвечает «Этот суд уже завершён», а не «не найден»;
  - нельзя запустить второй активный суд против того же человека в том же чате;
  - поддержка причины: "+Суд ССС @user / Причина" и "+Суд @user / Причина".
"""
import os
import re
import uuid
import logging
from html import escape
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")

MAX_SSS = 4

SSS_MEMBERS: set[int] = set()
SSS_TRIALS: dict[str, dict] = {}
PUBLIC_TRIALS: dict[str, dict] = {}

PUBLIC_TRIAL_VOTE_THRESHOLD = 5
PUBLIC_TRIAL_TIMEOUT_SEC = 600
SSS_TRIAL_TIMEOUT_SEC = 600


def _sss_all() -> set[int]:
    s = set(SSS_MEMBERS)
    if ADMIN_ID:
        s.add(ADMIN_ID)
    return s


def is_sss_member(user_id: int) -> bool:
    return user_id in _sss_all()


def _split_reason(text: str, triggers: str) -> tuple[str, Optional[str]]:
    """Разделяет "команда @user / причина" на (часть_с_целью, причина).

    triggers — regexp триггеров команды (например, r"\\+Суд\\s+ССС|\\+Суд").
    Возвращает (target_part, reason|None).
    """
    s = (text or "").strip()
    # отрезаем саму команду
    s = re.sub(rf"(?i)^(?:{triggers})\s*", "", s).strip()
    # причина после "/"
    reason: Optional[str] = None
    if "/" in s:
        head, tail = s.split("/", 1)
        s = head.strip()
        reason = tail.strip() or None
    return s, reason


def _active_trial_for(chat_id: int, target_id: int) -> Optional[dict]:
    """Ищет активный суд (ССС или публичный) против target_id в chat_id."""
    for t in SSS_TRIALS.values():
        if t["chat_id"] == chat_id and t["target_id"] == target_id:
            return t
    for t in PUBLIC_TRIALS.values():
        if t["chat_id"] == chat_id and t["target_id"] == target_id and not t.get("finalized"):
            return t
    return None


async def restore_sss_members() -> None:
    try:
        ids = await db.list_sss_members()
    except Exception as e:
        logger.warning("restore_sss_members: %s", e)
        return
    SSS_MEMBERS.clear()
    for uid in ids:
        if uid != ADMIN_ID:
            SSS_MEMBERS.add(uid)
    logger.info("sss: восстановлено участников: %s", len(SSS_MEMBERS))


async def cmd_sss_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    members = _sss_all()
    if len(members) >= MAX_SSS:
        await msg.reply_text(
            f"⛔ В ССС уже {len(members)} человек (максимум {MAX_SSS} вместе с админом)."
        )
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админ уже входит в ССС по умолчанию.")
        return
    if uid in SSS_MEMBERS:
        await msg.reply_text(f"{escape(name)} уже в ССС.")
        return
    if len(_sss_all()) >= MAX_SSS:
        await msg.reply_text("⛔ Мест в ССС больше нет.")
        return

    if not await db.set_sss(uid, True):
        await msg.reply_text(
            "⚠️ Не удалось сохранить в БД. Попробуйте ещё раз."
        )
        return
    SSS_MEMBERS.add(uid)
    await msg.reply_text(
        f"🏛 {escape(name)} добавлен(а) в ССС. "
        f"Всего: <b>{len(_sss_all())}/{MAX_SSS}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_sss_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админа из ССС убрать нельзя.")
        return
    if uid not in SSS_MEMBERS:
        await msg.reply_text(f"{escape(name)} не в ССС.")
        return

    if not await db.set_sss(uid, False):
        await msg.reply_text(
            "⚠️ Не удалось сохранить в БД. Попробуйте ещё раз."
        )
        return
    SSS_MEMBERS.discard(uid)
    await msg.reply_text(f"🏛 {escape(name)} исключён(а) из ССС.")


async def cmd_sss_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members = _sss_all()
    if not members:
        await update.message.reply_text("ССС пуст.")
        return
    lines = []
    for uid in members:
        name = str(uid)
        tag = " (админ)" if uid == ADMIN_ID else ""
        try:
            c = await context.bot.get_chat(uid)
            name = c.first_name or c.username or str(uid)
        except TelegramError:
            pass
        lines.append(f"• {escape(name)}{tag}")
    await update.message.reply_text(
        f"🏛 <b>Совет ССС</b> ({len(members)}/{MAX_SSS}):\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def cmd_sss_trial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    chat = update.effective_chat
    if not user or not msg or not chat:
        return
    if user.id not in _sss_all():
        await msg.reply_text("⛔ Только участники ССС.")
        return

    target_part, reason = _split_reason(msg.text or "", r"\+Суд\s+ССС")
    # resolve_target берёт из реплая, если target_part пуст
    target = await utils.resolve_target(update, context, target_part)
    if not target:
        await msg.reply_text(
            "Укажите @username или сделайте реплай.\n"
            "Причину можно добавить через <code>/</code>:\n"
            "<code>+Суд ССС @user / Причина</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админа судить нельзя.")
        return

    if _active_trial_for(chat.id, uid) is not None:
        await msg.reply_text(
            f"⚠️ Против {escape(name)} уже идёт суд. "
            f"Дождитесь его итога или начните позже.",
            parse_mode=ParseMode.HTML,
        )
        return

    trial_id = uuid.uuid4().hex[:10]
    sent = await msg.reply_text(
        _format_sss_trial_message(
            user_name=user.first_name or user.username or str(user.id),
            target_name=name,
            target_id=uid,
            reason=reason,
            members_count=len(_sss_all()),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_sss_trial_kb(trial_id),
    )

    SSS_TRIALS[trial_id] = {
        "chat_id": chat.id,
        "target_id": uid,
        "target_name": name,
        "reason": reason,
        "votes": {},
        "voters": set(),
        "by_user_id": user.id,
        "by_name": user.first_name or user.username or str(user.id),
        "message_id": sent.message_id,
        "finalized": False,
    }

    if context.job_queue:
        context.job_queue.run_once(
            sss_trial_timeout_job,
            when=SSS_TRIAL_TIMEOUT_SEC,
            data=trial_id,
        )


def _sss_trial_kb(trial_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Забанить", callback_data=f"ssstrial:yes:{trial_id}"),
        InlineKeyboardButton("❌ Оставить", callback_data=f"ssstrial:no:{trial_id}"),
    ]])


def _format_sss_trial_message(
    user_name: str, target_name: str, target_id: int,
    reason: Optional[str], members_count: int,
) -> str:
    reason_line = f"\nПричина: {escape(reason)}" if reason else ""
    return (
        f"⚖️ <b>Суд ССС</b>\n"
        f"Инициатор: {escape(user_name)}\n"
        f"Подсудимый: {escape(target_name)} (id <code>{target_id}</code>)"
        f"{reason_line}\n\n"
        f"Голосуют <b>только участники ССС</b>.\n"
        f"Если хоть один против — бан не выдаётся.\n"
        f"Участников: <b>{members_count}</b>\n"
        f"<i>Таймаут: {SSS_TRIAL_TIMEOUT_SEC // 60} мин.</i>"
    )


async def _edit_trial_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: Optional[int],
    text: str, remove_kb: bool = True,
) -> None:
    if not message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=None if remove_kb else None,
        )
    except TelegramError as e:
        logger.warning("edit trial message: %s", e)


async def _sss_trial_finalize(context: ContextTypes.DEFAULT_TYPE, trial_id: str,
                              force: bool = False) -> None:
    t = SSS_TRIALS.get(trial_id)
    if not t or t.get("finalized"):
        return
    members = _sss_all()
    if not force and not members.issubset(t["voters"]):
        return

    t["finalized"] = True
    any_no = any(v == "no" for v in t["votes"].values())
    reason_line = f"\nПричина: {escape(t['reason'])}" if t.get("reason") else ""

    # финализация: убираем кнопки у исходного сообщения
    if any_no:
        final_text = (
            f"⚖️ <b>Суд ССС завершён</b>\n"
            f"Подсудимый: {escape(t['target_name'])}{reason_line}\n"
            f"Решение: ❌ <b>Оставить</b> (кто-то проголосовал против)."
        )
        await _edit_trial_message(context, t["chat_id"], t.get("message_id"), final_text)
        SSS_TRIALS.pop(trial_id, None)
        return

    if force and not any(v == "yes" for v in t["votes"].values()):
        final_text = (
            f"⚖️ <b>Суд ССС завершён</b>\n"
            f"Подсудимый: {escape(t['target_name'])}{reason_line}\n"
            f"Решение: ⏳ <b>Недостаточно голосов</b> — оставить."
        )
        await _edit_trial_message(context, t["chat_id"], t.get("message_id"), final_text)
        SSS_TRIALS.pop(trial_id, None)
        return

    try:
        await context.bot.ban_chat_member(
            chat_id=t["chat_id"], user_id=t["target_id"],
        )
        await db.add_punishment(
            t["target_id"], t["chat_id"], "ban", None, ADMIN_ID,
        )
    except TelegramError as e:
        logger.warning("sss ban: %s", e)
        final_text = (
            f"⚖️ <b>Суд ССС завершён</b>\n"
            f"Подсудимый: {escape(t['target_name'])}{reason_line}\n"
            f"Решение: ✅ <b>Забанить</b>, но Telegram не дал: {escape(str(e))}"
        )
        await _edit_trial_message(context, t["chat_id"], t.get("message_id"), final_text)
        SSS_TRIALS.pop(trial_id, None)
        return

    final_text = (
        f"⚖️ <b>Суд ССС завершён</b>\n"
        f"Подсудимый: {escape(t['target_name'])}{reason_line}\n"
        f"Решение: ✅ <b>Забанен</b>"
        f"{'' if not force else ' (по таймауту)'}."
    )
    await _edit_trial_message(context, t["chat_id"], t.get("message_id"), final_text)
    SSS_TRIALS.pop(trial_id, None)


async def sss_trial_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    trial_id = job.data if job else None
    if not trial_id:
        return
    await _sss_trial_finalize(context, trial_id, force=True)


async def sss_trial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "ssstrial":
        await q.answer()
        return
    _, vote, trial_id = parts
    t = SSS_TRIALS.get(trial_id)
    if not t:
        await q.answer("Этот суд уже завершён.", show_alert=True)
        return
    if t.get("finalized"):
        await q.answer("Голосование уже закрыто.", show_alert=True)
        return
    if user.id not in _sss_all():
        await q.answer("Только ССС.", show_alert=True)
        return
    if user.id in t["voters"]:
        await q.answer("Вы уже голосовали в этом суде.", show_alert=True)
        return

    t["voters"].add(user.id)
    t["votes"][user.id] = vote
    await q.answer("Голос учтён")
    await _sss_trial_finalize(context, trial_id)


async def cmd_public_trial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    chat = update.effective_chat
    if not user or not msg or not chat:
        return

    target_part, reason = _split_reason(msg.text or "", r"\+Суд")
    target = await utils.resolve_target(update, context, target_part)
    if not target:
        await msg.reply_text(
            "Укажите @username или сделайте реплай.\n"
            "Причину можно добавить через <code>/</code>:\n"
            "<code>+Суд @user / Причина</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админа судить нельзя.")
        return

    if _active_trial_for(chat.id, uid) is not None:
        await msg.reply_text(
            f"⚠️ Против {escape(name)} уже идёт суд. "
            f"Дождитесь его итога или начните позже.",
            parse_mode=ParseMode.HTML,
        )
        return

    trial_id = uuid.uuid4().hex[:10]
    sent = await msg.reply_text(
        _format_public_trial_message(
            user_name=user.first_name or user.username or str(user.id),
            target_name=name,
            target_id=uid,
            reason=reason,
            yes=0, no=0,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_public_trial_kb(trial_id),
    )

    PUBLIC_TRIALS[trial_id] = {
        "chat_id": chat.id,
        "target_id": uid,
        "target_name": name,
        "reason": reason,
        "yes": 0,
        "no": 0,
        "voters": set(),
        "by_user_id": user.id,
        "by_name": user.first_name or user.username or str(user.id),
        "finalized": False,
        "message_id": sent.message_id,
    }

    if context.job_queue:
        context.job_queue.run_once(
            public_trial_finalize_job,
            when=PUBLIC_TRIAL_TIMEOUT_SEC,
            data=trial_id,
        )


def _public_trial_kb(trial_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Забанить", callback_data=f"pubtrial:yes:{trial_id}"),
        InlineKeyboardButton("❌ Оставить", callback_data=f"pubtrial:no:{trial_id}"),
    ]])


def _format_public_trial_message(
    user_name: str, target_name: str, target_id: int,
    reason: Optional[str], yes: int, no: int,
) -> str:
    reason_line = f"\nПричина: {escape(reason)}" if reason else ""
    return (
        f"⚖️ <b>Суд</b>\n"
        f"Инициатор: {escape(user_name)}\n"
        f"Подсудимый: {escape(target_name)} (id <code>{target_id}</code>)"
        f"{reason_line}\n\n"
        f"✅ За: <b>{yes}</b> | ❌ Против: <b>{no}</b>\n"
        f"Голосов: <b>{yes + no}</b>"
    )


async def public_trial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "pubtrial":
        await q.answer()
        return
    _, vote, trial_id = parts
    t = PUBLIC_TRIALS.get(trial_id)
    if not t:
        await q.answer("Этот суд уже завершён.", show_alert=True)
        return
    if t.get("finalized"):
        await q.answer("Голосование уже закрыто.", show_alert=True)
        return
    if user.id == t["target_id"]:
        await q.answer("Подсудимый не голосует.", show_alert=True)
        return
    if user.id in t["voters"]:
        await q.answer("Вы уже голосовали в этом суде.", show_alert=True)
        return

    t["voters"].add(user.id)
    if vote == "yes":
        t["yes"] += 1
    else:
        t["no"] += 1
    await q.answer("Голос учтён")

    yes, no = t["yes"], t["no"]
    try:
        await q.edit_message_text(
            _format_public_trial_message(
                user_name=t["by_name"],
                target_name=t["target_name"],
                target_id=t["target_id"],
                reason=t.get("reason"),
                yes=yes, no=no,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=q.message.reply_markup,
        )
    except TelegramError:
        pass

    if yes + no >= PUBLIC_TRIAL_VOTE_THRESHOLD:
        await _send_public_trial_to_admin(context, trial_id)


async def public_trial_finalize_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    trial_id = job.data if job else None
    if not trial_id:
        return
    await _send_public_trial_to_admin(context, trial_id)


async def _send_public_trial_to_admin(
    context: ContextTypes.DEFAULT_TYPE, trial_id: str,
) -> None:
    t = PUBLIC_TRIALS.get(trial_id)
    if not t or t.get("finalized"):
        return
    t["finalized"] = True

    # редактируем исходное сообщение — убираем кнопки голосования
    final_text = (
        f"⚖️ <b>Суд завершён</b>\n"
        f"Подсудимый: {escape(t['target_name'])} (id <code>{t['target_id']}</code>)"
        + (f"\nПричина: {escape(t['reason'])}" if t.get('reason') else "")
        + f"\nГолоса: ✅ {t['yes']} / ❌ {t['no']}\n"
        f"<i>Итог ушёл на утверждение админу.</i>"
    )
    await _edit_trial_message(context, t["chat_id"], t.get("message_id"), final_text)

    verdict_ban = t["yes"] > t["no"]
    text = (
        f"⚖️ <b>Итог суда</b>\n"
        f"Чат: <code>{t['chat_id']}</code>\n"
        f"Подсудимый: {escape(t['target_name'])} (id <code>{t['target_id']}</code>)\n"
        + (f"Причина: {escape(t['reason'])}\n" if t.get('reason') else "")
        + f"Голоса: ✅ {t['yes']} / ❌ {t['no']}\n"
        f"Инициатор: {escape(t['by_name'])}\n\n"
        f"Решение большинства: <b>{'ЗАБАНИТЬ' if verdict_ban else 'ОСТАВИТЬ'}</b>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔨 Забанить", callback_data=f"pubadm:ban:{trial_id}"),
        InlineKeyboardButton("✅ Оставить", callback_data=f"pubadm:keep:{trial_id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except TelegramError as e:
        logger.warning("public trial admin send: %s", e)


async def public_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await q.answer("Только админ.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "pubadm":
        await q.answer()
        return
    _, action, trial_id = parts
    t = PUBLIC_TRIALS.pop(trial_id, None)
    if not t:
        await q.answer("Суд не найден.", show_alert=True)
        return

    if action == "keep":
        await q.answer("Оставлен")
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n✅ <b>Админ: оставить.</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=(
                    f"⚖️ Суд над {escape(t['target_name'])} завершён. "
                    f"Админ решил: ✅ <b>оставить</b>."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    if action == "ban":
        try:
            await context.bot.ban_chat_member(
                chat_id=t["chat_id"], user_id=t["target_id"],
            )
            await db.add_punishment(
                t["target_id"], t["chat_id"], "ban", None, ADMIN_ID,
            )
            await q.answer("Забанен")
            await q.edit_message_text(
                (q.message.text or "") + "\n\n🔨 <b>Админ: забанить.</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=(
                    f"⚖️ Суд над {escape(t['target_name'])} завершён. "
                    f"Админ решил: 🔨 <b>забанить</b>."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
