"""Резолвинг @username -> user_id через Telethon (MTProto).
Если сессия/креды не заданы или Telethon не авторизовался — resolve_username
возвращает None, бот продолжает работать (резолв через БД).
"""
import os
import base64
import logging
import pathlib
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "") or ""

_client = None
_started = False


def _session_base_path() -> str:
    b64 = os.getenv("TG_SESSION_B64", "").strip()
    if b64:
        path = pathlib.Path(tempfile.gettempdir()) / "resolver_session.session"
        if not path.exists():
            path.write_bytes(base64.b64decode(b64))
        return str(path)[: -len(".session")]
    return os.getenv("TG_SESSION_PATH", "resolver_session")


def get_client():
    global _client
    if _client is None:
        if not API_ID or not API_HASH:
            raise RuntimeError("API_ID / API_HASH не заданы")
        from telethon import TelegramClient
        _client = TelegramClient(_session_base_path(), API_ID, API_HASH)
    return _client


async def start_client() -> None:
    """Пытается поднять Telethon. НЕ крэшит бота при ошибке."""
    global _started
    try:
        client = get_client()
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(
                "Telethon: сессия не авторизована. Резолв @username будет "
                "только через БД (get_user_by_username)."
            )
            await client.disconnect()
            return
        _started = True
        logger.info("Telethon подключён")
    except Exception as e:
        logger.warning(
            "Telethon НЕ подключён (%s). Резолв @username будет только через БД. "
            "Сервис продолжает работать.", e,
        )


async def stop_client() -> None:
    global _client, _started
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
    _started = False


async def resolve_username(username: str) -> Optional[tuple[int, str]]:
    if not _started or _client is None:
        return None
    uname = (username or "").lstrip("@").strip()
    if not uname:
        return None
    try:
        from telethon.errors import FloodWaitError
        entity = await _client.get_entity(uname)
        first_name = getattr(entity, "first_name", None)
        title = getattr(entity, "title", None)
        return entity.id, (first_name or title or uname)
    except FloodWaitError as e:
        logger.warning("resolve @%s: FloodWait %s сек", uname, e.seconds)
        return None
    except Exception as e:
        logger.info("resolve @%s: %s", uname, e)
        return None
