"""Резолвинг @username -> user_id через Telethon (MTProto)."""
import os
import base64
import logging
import pathlib
import tempfile
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

_client: Optional[TelegramClient] = None


def _session_base_path() -> str:
    """Путь БЕЗ .session — Telethon сам добавит расширение."""
    b64 = os.getenv("TG_SESSION_B64", "").strip()
    if b64:
        path = pathlib.Path(tempfile.gettempdir()) / "resolver_session.session"
        if not path.exists():
            path.write_bytes(base64.b64decode(b64))
        return str(path)[: -len(".session")]
    return os.getenv("TG_SESSION_PATH", "resolver_session")


def get_client() -> TelegramClient:
    global _client
    if _client is None:
        if not API_ID or not API_HASH:
            raise RuntimeError("API_ID / API_HASH не заданы")
        _client = TelegramClient(_session_base_path(), API_ID, API_HASH)
    return _client


async def start_client() -> None:
    client = get_client()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon-сессия не авторизована. Пересоздай resolver_session.session "
            "локально и обнови TG_SESSION_B64."
        )
    logger.info("Telethon подключён")


async def stop_client() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


async def resolve_username(username: str) -> Optional[tuple[int, str]]:
    """'@user' или 'user' -> (user_id, first_name) или None."""
    if not username:
        return None
    uname = username.lstrip("@")
    if not uname:
        return None
    client = get_client()
    try:
        entity = await client.get_entity(uname)
        first_name = getattr(entity, "first_name", None)
        title = getattr(entity, "title", None)
        name = first_name or title or uname
        return entity.id, name
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
        logger.info("resolve @%s: %s", uname, e)
        return None
    except FloodWaitError as e:
        logger.warning("FloodWait @%s: ждать %s сек", uname, e.seconds)
        return None
    except Exception as e:
        logger.warning("resolve @%s: %s", uname, e)
        return None
