"""Резолвинг @username -> user_id через Telethon (MTProto)."""
import os
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    FloodWaitError,
)

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

# Файл сессии — чтобы не логиниться каждый раз
SESSION_PATH = os.getenv("TG_SESSION_PATH", "resolver_session")

_client: Optional[TelegramClient] = None


def get_client() -> TelegramClient:
    global _client
    if _client is None:
        if not API_ID or not API_HASH:
            raise RuntimeError("API_ID / API_HASH не заданы")
        _client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    return _client


async def start_client() -> None:
    """Запуск в post_init. Если сессии нет — упадёт, см. инструкцию ниже."""
    client = get_client()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon-сессия не авторизована. "
            "Сгенерируй resolver_session.session локально (см. README) "
            "и положи рядом с ботом, либо задай TG_SESSION_PATH."
        )
    logger.info("Telethon подключён")


async def stop_client() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


async def resolve_username(username: str) -> Optional[tuple[int, str]]:
    """
    '@user' или 'user' -> (user_id, first_name) или None.
    Кэш не нужен — БД всё равно проверяется раньше (см. db.get_user_by_username).
    """
    if not username:
        return None
    uname = username.lstrip("@")
    if not uname:
        return None
    client = get_client()
    try:
        entity = await client.get_entity(uname)
        # entity может быть User / Channel / Chat
        first_name = getattr(entity, "first_name", None)
        title = getattr(entity, "title", None)
        name = first_name or title or uname
        return entity.id, name
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
        logger.info("resolve @%s: %s", uname, e)
        return None
    except FloodWaitError as e:
        logger.warning("FloodWait при resolve @%s: ждать %s сек", uname, e.seconds)
        return None
    except Exception as e:
        logger.warning("resolve @%s: %s", uname, e)
        return None
