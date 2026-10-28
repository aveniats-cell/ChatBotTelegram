"""ССС и суды."""
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

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админа судить нельзя.")
        return

    trial_id = uuid.uuid4().hex[:10]
    SSS_TRIALS[trial_id] = {
        "chat_id": chat.id,
        "target_id": uid,
        "target_name": name,
        "votes": {},
        "voters": set(),
        "by_user_id": user.id,
        "by_name": user.first_name or user.username or str(user.id),
    }

    members = _sss_all()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Забанить", callback_data=f"ssstrial:yes:{trial_id}"),
        InlineKeyboardButton("❌ Оставить", callback_data=f"ssstrial:no:{trial_id}"),
    ]])
    await msg.reply_text(
        f"⚖️ <b>Суд ССС</b>\n"
        f"Инициатор: {escape(user.first_name or str(user.id))}\n"
        f"Подсудимый: {escape(name)} (id <code>{uid}</code>)\n\n"
        f"Голосуют <b>только участники ССС</b>.\n"
        f"Если хоть один против — бан не выдаётся.\n"
        f"Участников: <b>{len(members)}</b>\n"
        f"<i>Таймаут: {SSS_TRIAL_TIMEOUT_SEC // 60} мин.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    if context.job_queue:
        context.job_queue.run_once(
            sss_trial_timeout_job,
            when=SSS_TRIAL_TIMEOUT_SEC,
            data=trial_id,
        )


async def _sss_trial_finalize(context: ContextTypes.DEFAULT_TYPE, trial_id: str,
                              force: bool = False) -> None:
    t = SSS_TRIALS.get(trial_id)
    if not t:
        return
    members = _sss_all()
    if not force and not members.issubset(t["voters"]):
        return

    any_no = any(v == "no" for v in t["votes"].values())
    SSS_TRIALS.pop(trial_id, None)

    if any_no:
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=(
                    f"⚖️ <b>Суд ССС завершён</b>\n"
                    f"Подсудимый: {escape(t['target_name'])}\n"
                    f"Решение: ❌ <b>Оставить</b> (кто-то проголосовал против)."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("sss trial finalize: %s", e)
        return

    if force and not any(v == "yes" for v in t["votes"].values()):
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=(
                    f"⚖️ <b>Суд ССС завершён</b>\n"
                    f"Подсудимый: {escape(t['target_name'])}\n"
                    f"Решение: ⏳ <b>Недостаточно голосов</b> — оставить."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("sss trial timeout: %s", e)
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
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=f"⚠️ Суд решил забанить, но Telegram не дал: {escape(str(e))}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    try:
        await context.bot.send_message(
            chat_id=t["chat_id"],
            text=(
                f"⚖️ <b>Суд ССС завершён</b>\n"
                f"Подсудимый: {escape(t['target_name'])}\n"
                f"Решение: ✅ <b>Забанен</b>"
                f"{'' if not force else ' (по таймауту)'}."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("sss trial finalize msg: %s", e)


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
        await q.answer("Суд не найден.", show_alert=True)
        return
    if user.id not in _sss_all():
        await q.answer("Только ССС.", show_alert=True)
        return
    if user.id in t["voters"]:
        await q.answer("Вы уже голосовали.", show_alert=True)
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

    target = await utils.resolve_target(update, context, msg.text or "")
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == ADMIN_ID:
        await msg.reply_text("Админа судить нельзя.")
        return

    trial_id = uuid.uuid4().hex[:10]
    PUBLIC_TRIALS[trial_id] = {
        "chat_id": chat.id,
        "target_id": uid,
        "target_name": name,
        "yes": 0,
        "no": 0,
        "voters": set(),
        "by_user_id": user.id,
        "by_name": user.first_name or user.username or str(user.id),
        "finalized": False,
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Забанить", callback_data=f"pubtrial:yes:{trial_id}"),
        InlineKeyboardButton("❌ Оставить", callback_data=f"pubtrial:no:{trial_id}"),
    ]])
    await msg.reply_text(
        f"⚖️ <b>Суд</b>\n"
        f"Инициатор: {escape(user.first_name or str(user.id))}\n"
        f"Подсудимый: {escape(name)} (id <code>{uid}</code>)\n\n"
        f"Голосовать может любой. Итог — большинством.\n"
        f"<i>Решение уйдёт на утверждение админу.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    if context.job_queue:
        context.job_queue.run_once(
            public_trial_finalize_job,
            when=PUBLIC_TRIAL_TIMEOUT_SEC,
            data=trial_id,
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
        await q.answer("Суд не найден.", show_alert=True)
        return
    if t.get("finalized"):
        await q.answer("Голосование уже закрыто.", show_alert=True)
        return
    if user.id == t["target_id"]:
        await q.answer("Подсудимый не голосует.", show_alert=True)
        return
    if user.id in t["voters"]:
        await q.answer("Вы уже голосовали.", show_alert=True)
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
            f"⚖️ <b>Суд</b>\n"
            f"Подсудимый: {escape(t['target_name'])} (id <code>{t['target_id']}</code>)\n\n"
            f"✅ За: <b>{yes}</b> | ❌ Против: <b>{no}</b>\n"
            f"Голосов: <b>{yes + no}</b>",
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

    verdict_ban = t["yes"] > t["no"]
    text = (
        f"⚖️ <b>Итог суда</b>\n"
        f"Чат: <code>{t['chat_id']}</code>\n"
        f"Подсудимый: {escape(t['target_name'])} (id <code>{t['target_id']}</code>)\n"
        f"Голоса: ✅ {t['yes']} / ❌ {t['no']}\n"
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
