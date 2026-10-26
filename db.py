"""Turso DB через HTTP-транспорт (libsql-client 0.3.1)."""
import os
import logging
from typing import Optional, Any
from libsql_client import create_client

logger = logging.getLogger(__name__)

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

_client: Optional[Any] = None


def _http_url(url: str) -> str:
    """libsql:// -> https://, чтобы клиент шёл по HTTP, а не по WebSocket."""
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


def _url_with_token(url: str, token: str) -> str:
    """Добавляет authToken в URL — способ аутентификации в libsql-client 0.3.1."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}authToken={token}"


async def init_db() -> None:
    global _client
    if not TURSO_URL or not TURSO_TOKEN:
        raise SystemExit("TURSO_DATABASE_URL и/или TURSO_AUTH_TOKEN не заданы")
    if _client is None:
        # В libsql-client 0.3.1 нет аргумента auth_token/scheme у create_client —
        # токен передаётся query-параметром authToken в URL.
        url = _url_with_token(_http_url(TURSO_URL), TURSO_TOKEN)
        _client = create_client(url)
    await _create_tables()
    logger.info("DB инициализирована")


async def _create_tables() -> None:
    await _client.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    await _client.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id      INTEGER PRIMARY KEY,
            is_subscribed INTEGER NOT NULL DEFAULT 0,
            checked_at   TEXT
        )
    """)


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def upsert_user(user_id: int, username: Optional[str], first_name: str) -> None:
    await _client.execute(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name""",
        [user_id, username or "", first_name or ""],
    )


async def set_subscription(user_id: int, is_subscribed: bool) -> None:
    await _client.execute(
        """INSERT INTO subscriptions (user_id, is_subscribed, checked_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             is_subscribed=excluded.is_subscribed,
             checked_at=excluded.checked_at""",
        [user_id, 1 if is_subscribed else 0],
    )


async def get_subscription(user_id: int) -> Optional[bool]:
    rs = await _client.execute(
        "SELECT is_subscribed FROM subscriptions WHERE user_id = ?", [user_id]
    )
    if not rs.rows:
        return None
    return bool(rs.rows[0][0])
