"""Единый визуальный стандарт интерфейса Telegram-бота.

Модуль отвечает только за подписи inline-кнопок. callback_data и обработчики
остаются без изменений.
"""
from telegram import InlineKeyboardButton as _TelegramInlineKeyboardButton
import re


def _button_label(text: str, callback_data: str | None = None) -> str:
    label = str(text or "").strip()
    callback = str(callback_data or "").lower()
    # Запоминаем существующий значок: если категории нет, сохраняем его.
    match = re.match(r"^((?:[\U0001F300-\U0001FAFF]|[\u2600-\u27BF])(?:\ufe0f)?)\s*", label)
    original_icon = match.group(1) if match else ""
    if match:
        label = label[match.end():]
    plain = label.lstrip("←⬅️➡️🔙◀️▶️ ").lower()

    if "coin" in callback or "монет" in plain:
        return f"🪙 {label}"
    if "dice" in callback or "кубик" in plain:
        return f"🎲 {label}"
    if "slot" in callback or "слот" in plain:
        return f"🎰 {label}"

    # Единый стиль навигации.
    if any(word in plain for word in ("назад", "главное меню", "в меню", "к профилю", "к пакетам", "в магазин", "каталог")):
        return f"↩️ {plain[0].upper() + plain[1:]}" if plain else "↩️ Назад"

    # Семантика callback имеет приоритет над текстом кнопки.
    rules = (
        (("profile", "prof:"), "👤"), (("balance", "money", "economy"), "💰"),
        (("shop", "premium", "pack", "token"), "🛍️"), (("casino", "cas:", "game"), "🎮"),
        (("top", "rating", "rank"), "🏆"), (("aura",), "✨"),
        (("marriage", "wedding", "brak"), "💍"), (("help", "справк"), "📚"),
        (("rule", "правил"), "📜"), (("ai", "ксени"), "🤖"),
        (("music", "музык"), "🎵"), (("poll", "опрос"), "📊"),
        (("warn", "бан", "мут", "role", "жалоб"), "🛡️"),
        (("game", "игр"), "🎮"), (("stat", "статист"), "📈"),
        (("award", "наград"), "🏅"), (("safety", "безопас"), "🔐"),
        (("complaint", "предлож", "comment"), "💬"),
        (("accept", "принять"), "✅"), (("reject", "отказать"), "❌"),
        (("reason_adult", "18+"), "🔞"), (("reason_profanity", "лексик"), "🤬"),
        (("reason_spam", "спам"), "🗑️"), (("renew", "продл"), "🔄"),
    )
    for keys, emoji in rules:
        if any(key in callback for key in keys) or any(key in plain for key in keys):
            return f"{emoji} {label}"

    # Сохраняем осмысленные старые значки; иначе используем нейтральный маркер.
    if original_icon:
        return f"{original_icon} {label}"
    if label:
        return f"🔹 {label}"
    return label


def InlineKeyboardButton(text: str, *args, **kwargs):
    """Совместимая замена Telegram-кнопки с унифицированной подписью."""
    kwargs["text"] = _button_label(text, kwargs.get("callback_data"))
    return _TelegramInlineKeyboardButton(*args, **kwargs)
