"""Личный кабинет предложки TAMBOV INSIDE.

Обычные пользователи в личке работают только с предложениями/жалобами.
Групповые функции Superbot этот модуль не затрагивает.
"""
import logging
import os
import secrets
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove,
)
from telegram.ext import ContextTypes, ApplicationHandlerStop

import db

logger = logging.getLogger(__name__)

MENU_TEXT = (
    "Добрый день! Вы написали в предложку TAMBOV INSIDE.\n\n"
    "Выберите, с чем хотите обратиться:"
)
MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💡 Предложить идею")],
        [KeyboardButton("📰 Предложить пост")],
        [KeyboardButton("⚠️ Жалоба")],
        [KeyboardButton("❓ Почему отклонили пост/идею")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
PROMPTS = {
    "idea": "💡 Опишите вашу идею как можно подробнее. Мы передадим её на рассмотрение администрации.",
    "post": "📰 Отправьте текст поста. Если к публикации нужны фотографии или видео, укажите это в сообщении.",
    "complaint": "⚠️ Опишите ситуацию и укажите, на кого или на что вы жалуетесь. Обращение увидит администрация.",
}
TYPE_NAMES = {"idea": "ИДЕЯ", "post": "ПОСТ", "complaint": "ЖАЛОБА"}
REASONS = {
    "adult": "Материал содержит контент 18+ или неприемлемые для публикации подробности.",
    "profanity": "В тексте обнаружена ненормативная лексика. Пожалуйста, отправьте исправленный вариант.",
    "spam": "Материал не подходит для публикации: мало смысла, отсутствует полезная информация или это спам.",
}
MAX_TEXT = 3500


async def init_db() -> None:
    """Создаёт отдельную таблицу, используя уже инициализированное подключение db."""
    result = await db._exec("""
        CREATE TABLE IF NOT EXISTS helper_submissions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            kind TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT,
            admin_comment TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    if getattr(result, "error", False):
        raise RuntimeError("Не удалось создать таблицу helper_submissions")


async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    context.user_data.pop("helper_state", None)
    await msg.reply_text(MENU_TEXT, reply_markup=MENU)


async def _create_submission(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             kind: str, body: str) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    submission_id = secrets.token_hex(5)
    result = await db._exec(
        """INSERT INTO helper_submissions
           (id, user_id, username, first_name, kind, body)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [submission_id, user.id, user.username or "", user.first_name or "", kind, body],
    )
    if getattr(result, "error", False):
        await msg.reply_text("Не получилось отправить обращение из-за технической ошибки. Попробуйте немного позже.")
        return

    await msg.reply_text(
        "✅ Спасибо! Ваше обращение отправлено администрации TAMBOV INSIDE.\n"
        "Ожидайте ответа в течение 2 часов.",
        reply_markup=MENU,
    )

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    user_label = f"{user.first_name or 'Пользователь'}"
    if user.username:
        user_label += f" (@{user.username})"
    admin_text = (
        f"📥 НОВОЕ ОБРАЩЕНИЕ · {TYPE_NAMES[kind]}\n"
        f"От: {user_label}\nID: {user.id}\n"
        f"Номер: {submission_id}\n\n"
        f"{body}"
    )
    if kind in ("idea", "post"):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принять", callback_data=f"sug:accept:{submission_id}"),
            InlineKeyboardButton("❌ Отказать", callback_data=f"sug:reject:{submission_id}"),
        ]])
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Дать комментарий", callback_data=f"sug:comment:{submission_id}")
        ]])
    try:
        await context.bot.send_message(chat_id=int(os.getenv("ADMIN_ID", "0") or 0),
                                       text=admin_text, reply_markup=keyboard)
    except Exception:
        logger.exception("Не удалось доставить обращение админу: %s", submission_id)
        await msg.reply_text("⚠️ Обращение сохранено, но уведомить администратора не удалось. Попробуйте позже.")


async def _handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    text = (msg.text or "").strip()
    state = context.user_data.get("helper_state")

    if state in ("idea", "post", "complaint"):
        if not text:
            await msg.reply_text("Пожалуйста, отправьте обращение текстом. Если нужно приложить медиа, добавьте ссылку или пояснение.")
            return
        if len(text) > MAX_TEXT:
            await msg.reply_text(f"Сообщение слишком длинное. Максимум — {MAX_TEXT} символов. Сократите текст и отправьте ещё раз.")
            return
        context.user_data.pop("helper_state", None)
        await _create_submission(update, context, state, text)
        return

    if text in ("💡 Предложить идею", "Предложить идею"):
        context.user_data["helper_state"] = "idea"
        await msg.reply_text(PROMPTS["idea"], reply_markup=ReplyKeyboardRemove())
    elif text in ("📰 Предложить пост", "Предложить пост"):
        context.user_data["helper_state"] = "post"
        await msg.reply_text(PROMPTS["post"], reply_markup=ReplyKeyboardRemove())
    elif text in ("⚠️ Жалоба", "Жалоба"):
        context.user_data["helper_state"] = "complaint"
        await msg.reply_text(PROMPTS["complaint"], reply_markup=ReplyKeyboardRemove())
    elif text in ("❓ Почему отклонили пост/идею", "Почему отклонили пост/идею"):
        rs = await db._exec(
            """SELECT rejection_reason FROM helper_submissions
               WHERE user_id = ? AND status = 'rejected' AND rejection_reason IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            [update.effective_user.id],
        )
        if rs.rows:
            answer = "ℹ️ Причина последнего отказа:\n\n" + str(rs.rows[0][0])
        else:
            answer = (
                "ℹ️ Материалы могут быть отклонены, если содержат:\n"
                "• контент 18+ или неприемлемые подробности;\n"
                "• мат и оскорбления;\n"
                "• спам, бессодержательный или не относящийся к теме материал.\n\n"
                "Если вы уже получали отказ, здесь будет указана его конкретная причина."
            )
        await msg.reply_text(answer, reply_markup=MENU)
    elif text in ("/start", "/menu", "Меню"):
        await start_menu(update, context)
    else:
        await msg.reply_text("Выберите действие с помощью кнопок меню ниже.", reply_markup=MENU)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         admin_id: int) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("sug:"):
        return False
    if not update.effective_user or update.effective_user.id != admin_id:
        await query.answer("Эти кнопки доступны только администратору.", show_alert=True)
        return True
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 3:
        return True
    action, submission_id = parts[1], parts[2]
    rs = await db._exec(
        "SELECT user_id, kind, body, status FROM helper_submissions WHERE id = ?",
        [submission_id],
    )
    if not rs.rows:
        await query.edit_message_text((query.message.text or "") + "\n\n⚠️ Заявка не найдена.")
        return True
    user_id, kind, body, status = rs.rows[0]
    if status != "pending":
        await query.answer("Эта заявка уже обработана.", show_alert=True)
        return True

    if action == "accept" and kind in ("idea", "post"):
        await db._exec("UPDATE helper_submissions SET status='accepted', updated_at=datetime('now') WHERE id=? AND status='pending'", [submission_id])
        await context.bot.send_message(chat_id=int(user_id),
            text=f"✅ Ваша заявка ({TYPE_NAMES[kind].lower()}) принята администрацией TAMBOV INSIDE. Спасибо за участие!")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Заявка принята. Пользователь получил уведомление.")
    elif action == "reject" and kind in ("idea", "post"):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔞 Контент 18+", callback_data=f"sug:reason_adult:{submission_id}")],
            [InlineKeyboardButton("🤬 Ненормативная лексика", callback_data=f"sug:reason_profanity:{submission_id}")],
            [InlineKeyboardButton("🗑 Спам / бессодержательный материал", callback_data=f"sug:reason_spam:{submission_id}")],
        ])
        await query.message.reply_text("Выберите причину отказа:", reply_markup=keyboard)
    elif action.startswith("reason_"):
        reason_key = action[len("reason_"):]
        reason = REASONS.get(reason_key)
        if not reason:
            return True
        changed = await db._exec(
            """UPDATE helper_submissions SET status='rejected', rejection_reason=?,
               updated_at=datetime('now') WHERE id=? AND status='pending'""",
            [reason, submission_id],
        )
        if not getattr(changed, "error", False):
            await context.bot.send_message(chat_id=int(user_id),
                text="❌ К сожалению, администрация отклонила вашу заявку.\n\nПричина:\n" + reason)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Отказ отправлен пользователю.")
    elif action == "comment" and kind == "complaint":
        context.bot_data["helper_admin_comment"] = {"submission_id": submission_id, "user_id": int(user_id)}
        await query.message.reply_text("Напишите комментарий одним сообщением. Он будет отправлен автору жалобы.")
    else:
        await query.answer("Действие недоступно для этого обращения.", show_alert=True)
    return True


async def handle_private_update(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                admin_id: int) -> None:
    """Роутер группы -2: изолирует личку обычных пользователей от всех функций Superbot."""
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return

    user = update.effective_user
    if not user:
        raise ApplicationHandlerStop

    # Админ сохраняет полный доступ к существующим командам в личке.
    # Здесь обрабатываются только кнопки предложки и ожидаемый текст комментария.
    if user.id == admin_id:
        query = update.callback_query
        if query and query.data and query.data.startswith("sug:"):
            await admin_callback(update, context, admin_id)
            raise ApplicationHandlerStop
        pending = context.bot_data.get("helper_admin_comment")
        msg = update.effective_message
        if pending and msg and msg.text and not msg.text.startswith("/"):
            context.bot_data.pop("helper_admin_comment", None)
            await context.bot.send_message(chat_id=pending["user_id"],
                text="💬 Комментарий администрации по вашей жалобе:\n\n" + msg.text)
            await msg.reply_text("Комментарий отправлен автору жалобы.")
            raise ApplicationHandlerStop
        return

    # Все обновления обычного пользователя в личке обрабатывает только предложка.
    msg = update.effective_message
    if msg and msg.text and msg.text.strip().startswith("/start"):
        await start_menu(update, context)
    elif update.callback_query:
        await update.callback_query.answer()
    else:
        await _handle_user_message(update, context)
    raise ApplicationHandlerStop
