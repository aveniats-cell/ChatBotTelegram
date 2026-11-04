"""Поиск и отправка музыки из каталога Jamendo.

Используются только треки, для которых Jamendo разрешает скачивание и
лицензия которых относится к Creative Commons BY, BY-SA или CC0.
Для работы задайте переменную окружения JAMENDO_CLIENT_ID.
"""

import io
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "").strip()
API_URL = "https://api.jamendo.com/v3.0/tracks/"
MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024

ALLOWED_LICENSE_MARKERS = (
    "by/3.0",
    "by/4.0",
    "by-sa/3.0",
    "by-sa/4.0",
    "publicdomain/zero/1.0",
    "cc0",
)


def _allowed_track(track: dict[str, Any]) -> bool:
    """Проверяет разрешение на скачивание и подходящую CC-лицензию."""
    if track.get("audiodownload_allowed") is not True:
        return False

    license_url = str(track.get("license_ccurl") or "").lower()
    return any(marker in license_url for marker in ALLOWED_LICENSE_MARKERS)


def _track_label(track: dict[str, Any]) -> str:
    name = str(track.get("name") or "Без названия").strip()
    artist = str(track.get("artist_name") or "Неизвестный автор").strip()
    label = f"{name} — {artist}"
    return label[:64]


async def _search_tracks(query: str) -> list[dict[str, Any]]:
    if not CLIENT_ID:
        raise RuntimeError("Не задан JAMENDO_CLIENT_ID")

    params = {
        "client_id": CLIENT_ID,
        "format": "json",
        "search": query,
        "limit": 50,
        "audioformat": "mp32",
        "include": "licenses",
        "order": "relevance",
    }

    timeout = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        response = await client.get(API_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    if payload.get("headers", {}).get("status") not in ("success", "ok"):
        message = payload.get("headers", {}).get("error_message") or "ошибка API"
        raise RuntimeError(f"Jamendo: {message}")

    results = payload.get("results") or []

    # Берём максимум 10 подходящих треков.
    # Запрашиваем больше, поскольку часть каталога может быть недоступна
    # для скачивания или не подходить по лицензии.
    return [
        track for track in results
        if _allowed_track(track)
    ][:10]


def _valid_audio_url(url: str) -> bool:
    """Разрешаем только HTTPS-ссылки."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname)


async def cmd_music(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if not query:
        await message.reply_text(
            "🎵 Напиши запрос после команды.\n"
            "Например:\n"
            "Музыка chill electronic\n"
            "Музыка Imagine Dragons"
        )
        return

    if not CLIENT_ID:
        await message.reply_text(
            "⚠️ Поиск музыки пока не настроен.\n"
            "Администратору нужно добавить JAMENDO_CLIENT_ID "
            "в переменные окружения Render."
        )
        return

    status = await message.reply_text(
        "🔎 Ищу разрешённые для скачивания треки…"
    )

    try:
        tracks = await _search_tracks(query)

    except httpx.HTTPError:
        logger.exception("Jamendo API request failed")
        await status.edit_text(
            "⚠️ Не удалось связаться с каталогом музыки. Попробуй позже."
        )
        return

    except Exception:
        logger.exception("Music search failed")
        await status.edit_text(
            "⚠️ Не удалось выполнить поиск. Проверь настройки Jamendo."
        )
        return

    if not tracks:
        await status.edit_text(
            "😕 По этому запросу не нашлось треков, "
            "которые доступны для скачивания и подходят "
            "по лицензии Creative Commons."
        )
        return

    # Сохраняем результаты отдельно для каждого пользователя.
    context.user_data["music_search_results"] = tracks

    keyboard = [
        [
            InlineKeyboardButton(
                _track_label(track),
                callback_data=f"music:{index}",
            )
        ]
        for index, track in enumerate(tracks)
    ]

    await status.edit_text(
        f"🎵 Найдено треков: {len(tracks)}.\n"
        "Нажми на название, чтобы получить аудио:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def music_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data or ""

    try:
        index = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.message.reply_text(
            "⚠️ Некорректный выбор трека."
        )
        return

    tracks = context.user_data.get("music_search_results") or []

    if index < 0 or index >= len(tracks):
        await query.message.reply_text(
            "⌛ Результаты поиска устарели. Выполни поиск ещё раз."
        )
        return

    track = tracks[index]

    download_url = str(
        track.get("audiodownload") or ""
    ).strip()

    if not download_url or not _valid_audio_url(download_url):
        await query.message.reply_text(
            "⚠️ Для этого трека нет безопасной ссылки на скачивание."
        )
        return

    title = str(track.get("name") or "Трек")
    artist = str(track.get("artist_name") or "Неизвестный автор")
    license_url = str(track.get("license_ccurl") or "")

    await query.message.reply_text(
        f"⬇️ Загружаю: {title} — {artist}"
    )

    try:
        await context.bot.send_chat_action(
            chat_id=query.message.chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        timeout = httpx.Timeout(60.0, connect=15.0)

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        ) as client:

            async with client.stream(
                "GET",
                download_url,
            ) as response:

                response.raise_for_status()

                content_length = response.headers.get("content-length")

                if (
                    content_length
                    and int(content_length) > MAX_DOWNLOAD_BYTES
                ):
                    await query.message.reply_text(
                        "⚠️ Файл слишком большой для отправки ботом."
                    )
                    return

                audio = bytearray()

                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)

                    if len(audio) > MAX_DOWNLOAD_BYTES:
                        await query.message.reply_text(
                            "⚠️ Файл слишком большой для отправки ботом."
                        )
                        return

        filename = f"{title} - {artist}.mp3"

        safe_filename = "".join(
            char if char.isalnum() or char in " ._-()" else "_"
            for char in filename
        )[:120]

        audio_file = io.BytesIO(bytes(audio))
        audio_file.name = safe_filename or "track.mp3"

        caption = f"{title} — {artist}"

        if license_url:
            caption += f"\nЛицензия: {license_url}"

        await context.bot.send_audio(
            chat_id=query.message.chat_id,
            audio=audio_file,
            title=title[:64],
            performer=artist[:64],
            caption=caption[:1024],
        )

    except httpx.HTTPError:
        logger.exception("Track download failed")
        await query.message.reply_text(
            "⚠️ Не удалось скачать трек. Попробуй выбрать другой."
        )

    except Exception:
        logger.exception("Track send failed")
        await query.message.reply_text(
            "⚠️ Не удалось отправить аудио. Попробуй позже."
        )
