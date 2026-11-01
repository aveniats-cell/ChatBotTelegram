"""Профиль участника: аккуратный основной экран и отдельные разделы."""
import os
import re
import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import resolver
import sss

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


def _safe(value) -> str:
    """Безопасный текст для Telegram HTML."""
    return escape(str(value if value is not None else "—"))


def _section_title(icon: str, title: str, name: str) -> str:
    return (
        f"{icon} <b>{title}</b>\n"
        f"<i>{_safe(name)}</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id

    match = USERNAME_RE.search(msg.text or "")
    if not match:
        return None

    username = match.group(1)
    try:
        uid = await db.get_user_by_username(username)
        if uid:
            return uid
    except Exception as exc:
        logger.warning("get_user_by_username(%s): %s", username, exc)

    try:
        resolved = await resolver.resolve_username(username)
        if resolved:
            return resolved[0]
    except Exception as exc:
        logger.warning("resolve_username(%s): %s", username, exc)

    try:
        chat = await context.bot.get_chat(f"@{username}")
        return chat.id
    except TelegramError as exc:
        logger.warning("get_chat @%s: %s", username, exc)

    return None


def _kb(target_uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Игры", callback_data=f"prof:games:{target_uid}"),
            InlineKeyboardButton("✨ Аура", callback_data=f"prof:aura:{target_uid}"),
        ],
        [
            InlineKeyboardButton("🏅 Награды", callback_data=f"prof:awards:{target_uid}"),
            InlineKeyboardButton("📊 Статистика", callback_data=f"prof:counters:{target_uid}"),
        ],
        [
            InlineKeyboardButton("🛡️ Безопасность", callback_data=f"prof:safety:{target_uid}"),
        ],
    ])


def _kb_section(target_uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("‹ Назад к профилю", callback_data=f"prof:base:{target_uid}")]
    ])


async def _render_base(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        try:
            chat_user = await context.bot.get_chat(target_uid)
            await db.upsert_user(
                target_uid, chat_user.username, chat_user.first_name or ""
            )
            user_row = await db.get_user(target_uid)
        except TelegramError:
            pass

    if not user_row:
        return "Не нашёл этого пользователя."

    name = user_row.get("first_name") or str(target_uid)
    rank = await db.user_rank(chat_id, target_uid)
    total_msgs = rank[0] if rank else 0
    try:
        reputation_data = await db.reputation_get(target_uid)
        reputation_score = reputation_data.get("score", 5.5)
    except Exception as exc:
        logger.warning("profile reputation: %s", exc)
        reputation_score = "—"

    marriage = await db.find_active_marriage(chat_id, target_uid)
    if marriage:
        partner_id = (
            marriage["user1_id"]
            if marriage["user2_id"] == target_uid
            else marriage["user2_id"]
        )
        partner_name = str(partner_id)
        try:
            partner = await context.bot.get_chat(partner_id)
            partner_name = partner.first_name or partner.username or str(partner_id)
        except TelegramError:
            pass
        lock_mark = " 🔒" if marriage.get("forced") else ""
        relationship = f"💍 В браке с <b>{_safe(partner_name)}</b>{lock_mark}"
    else:
        divorces = await db.count_divorces(chat_id, target_uid)
        relationship = (
            f"💔 Разводов: <b>{divorces}</b>" if divorces > 0
            else "💍 Не состоит в браке"
        )

    role = await db.get_role(target_uid)
    sss_member = bool(user_row.get("sss_member")) or sss.is_sss_member(target_uid)

    if target_uid == ADMIN_ID:
        role_name = "Создатель"
    elif sss_member and role == "sheriff":
        role_name = "Шериф · ССС"
    elif sss_member and role == "commissar":
        role_name = "Комиссар · ССС"
    elif sss_member:
        role_name = "Участник ССС"
    elif role == "sheriff":
        role_name = "Шериф"
    elif role == "commissar":
        role_name = "Комиссар"
    else:
        role_name = "Участник"

    balance = user_row.get("balance")
    if balance is None:
        balance = 10
    credit = user_row.get("credit") or 0

    try:
        awards_total = await db.award_total_stars(chat_id, target_uid)
    except Exception as exc:
        logger.warning("profile awards_total: %s", exc)
        awards_total = 0

    # Разбиваем информацию на короткие смысловые блоки.
    return (
        f"👤 <b>ПРОФИЛЬ УЧАСТНИКА</b>\n"
        f"<b>{_safe(name)}</b>\n"
        f"<i>{_safe(role_name)}</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>{total_msgs:,}</b> сообщений   ·   ⭐ Репутация: <b>{reputation_score}</b>\n"
        f"🪙 <b>{balance:,}</b> монет   ·   💳 Кредит: <b>{credit:,}</b>\n"
        f"🏅 Награды: <b>{awards_total} ★</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>ЛИЧНОЕ</b>\n"
        f"{relationship}\n"
        f"🛡️ Служба ССС: <b>{'Да' if sss_member else 'Нет'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Выбери раздел, чтобы посмотреть подробности ↓</i>"
    )


async def _render_safety(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    name = (user_row or {}).get("first_name") or str(target_uid)

    complaints_by = await db.count_complaints_by(chat_id, target_uid)
    complaints_against = await db.count_complaints_against(chat_id, target_uid)
    warnings_active = await db.count_active_warnings(chat_id, target_uid)
    warnings_total = await db.count_warnings_total(chat_id, target_uid)
    mutes = (user_row or {}).get("mutes_issued") or 0

    return (
        _section_title("🛡️", "БЕЗОПАСНОСТЬ", name)
        + "<b>Жалобы</b>\n"
        + f"  Подано участником: <b>{complaints_by}</b>\n"
        + f"  Получено: <b>{complaints_against}</b>\n\n"
        + "<b>Предупреждения</b>\n"
        + f"  Активных: <b>{warnings_active}</b>\n"
        + f"  Всего: <b>{warnings_total}</b>\n\n"
        + f"🔇 Выдано мутов: <b>{mutes}</b>"
    )


async def _render_games(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        return "Не нашёл этого пользователя."

    name = user_row.get("first_name") or str(target_uid)
    wins = user_row.get("tictactoe_wins") or 0
    losses = user_row.get("tictactoe_losses") or 0
    draws = user_row.get("tictactoe_draws") or 0
    played = wins + losses + draws

    return (
        _section_title("🎮", "ИГРЫ", name)
        + "<b>Крестики-нолики</b>\n"
        + f"🏆 Победы: <b>{wins}</b>\n"
        + f"💥 Поражения: <b>{losses}</b>\n"
        + f"🤝 Ничьи: <b>{draws}</b>\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + f"Всего сыграно: <b>{played}</b>"
    )


async def _render_aura(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    name = (user_row or {}).get("first_name") or str(target_uid)
    value = 0
    rank_line = ""

    try:
        row = await db.aura_get(chat_id, target_uid)
        value = (row or {}).get("value") or 0
        if value > 0:
            rank = await db.aura_rank(chat_id, target_uid)
            if rank:
                _, aura_rank, aura_total = rank
                rank_line = (
                    f"\nМесто в чате: <b>{aura_rank}</b> из <b>{aura_total}</b>"
                )
    except Exception as exc:
        logger.warning("profile aura: %s", exc)

    return (
        _section_title("✨", "АУРА", name)
        + f"<b>{value:,}</b> очков ауры"
        + rank_line
    )


async def _render_awards(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    name = (user_row or {}).get("first_name") or str(target_uid)

    try:
        items = await db.list_awards(chat_id, target_uid)
        total = await db.award_total_stars(chat_id, target_uid)
    except Exception as exc:
        logger.warning("profile awards: %s", exc)
        items, total = [], 0

    if not items:
        return (
            _section_title("🏅", "НАГРАДЫ", name)
            + "Пока наград нет.\n"
            + "<i>Награды, полученные в чате, появятся здесь.</i>"
        )

    lines = [
        _section_title("🏅", "НАГРАДЫ", name),
        f"Получено: <b>{len(items)}</b>  ·  Всего звёзд: <b>{total} ★</b>",
        "",
    ]
    for award in items:
        stars = max(0, min(int(award.get("stars") or 0), 10))
        star_line = "★" * stars + "☆" * (10 - stars)
        title = _safe(award.get("title") or "Без названия")
        lines.append(f"• <b>{title}</b>\n  {star_line}  <b>{stars}/10</b>")

    return "\n".join(lines)


async def _render_counters(context, chat_id: int, target_uid: int) -> str:
    user_row = await db.get_user(target_uid)
    if not user_row:
        return "Не нашёл этого пользователя."

    name = user_row.get("first_name") or str(target_uid)
    mutes = user_row.get("mutes_issued") or 0
    reports = user_row.get("reports_made") or 0
    courts = user_row.get("courts_held") or 0

    return (
        _section_title("📊", "СТАТИСТИКА", name)
        + f"🔇 Выдано мутов: <b>{mutes}</b>\n"
        + f"🚨 Подано жалоб: <b>{reports}</b>\n"
        + f"⚖️ Проведено судов: <b>{courts}</b>"
    )


_RENDERERS = {
    "safety": _render_safety,
    "games": _render_games,
    "aura": _render_aura,
    "awards": _render_awards,
    "counters": _render_counters,
}


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _cmd_profile_inner(update, context)
    except Exception as exc:
        logger.exception("cmd_profile error: %s", exc)
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Не удалось загрузить профиль. Попробуйте позже."
                )
        except Exception:
            pass


async def _cmd_profile_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    target_uid = await _resolve_target(update, context)
    if target_uid is None:
        user = update.effective_user
        if not user:
            return
        target_uid = user.id

    profile_text = await _render_base(context, chat.id, target_uid)
    if profile_text.startswith("Не нашёл"):
        await msg.reply_text(profile_text)
        return

    await msg.reply_text(
        profile_text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb(target_uid),
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "prof":
        await query.answer()
        return

    _, section, uid_text = parts
    try:
        target_uid = int(uid_text)
    except ValueError:
        await query.answer("Некорректный id.", show_alert=True)
        return

    chat = query.message.chat if query.message else None
    if not chat:
        await query.answer()
        return

    try:
        if section == "base":
            profile_text = await _render_base(context, chat.id, target_uid)
            keyboard = _kb(target_uid)
        else:
            renderer = _RENDERERS.get(section)
            if not renderer:
                await query.answer("Раздел не найден.", show_alert=True)
                return
            profile_text = await renderer(context, chat.id, target_uid)
            keyboard = _kb_section(target_uid)

        await query.edit_message_text(
            profile_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        await query.answer()
    except TelegramError as exc:
        if "not modified" in str(exc).lower():
            await query.answer()
            return
        logger.warning("profile_callback: %s", exc)
        try:
            await query.answer("Не удалось обновить профиль.", show_alert=True)
        except TelegramError:
            pass
