"""Работа с Turso DB (libsql-client, HTTP-транспорт)."""
import os
import logging
from typing import Optional, Any
from libsql_client import create_client

logger = logging.getLogger(__name__)

# Переменные окружения (задаются в Render → Environment)
TURSO_URL = os.getenv("TURSO_DATABASE_URL")      # libsql://xxx.turso.io
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")      # токен

_client: Optional[Any] = None


async def init_db() -> None:
    """Инициализация клиента и создание таблиц."""
    global _client
    if not TURSO_URL or not TURSO_TOKEN:
        raise SystemExit(
            "TURSO_DATABASE_URL и/или TURSO_AUTH_TOKEN не заданы в Environment"
        )
    if _client is None:
        # scheme="http" — используем HTTPS /v2/pipeline вместо WebSocket,
        # иначе Turso отвечает 400 на wss:// handshake
        _client = create_client(
            url=TURSO_URL,
            auth_token=TURSO_TOKEN,
            scheme="http",
        )
    await _create_tables()
    logger.info("DB инициализирована")


async def _create_tables() -> None:
    """Создаём таблицы, если их нет.
    Сюда же легко добавлять новые таблицы для новых фич.
    """
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


# ---------- users ----------

async def upsert_user(user_id: int, username: Optional[str], first_name: str) -> None:
    await _client.execute(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name""",
        [user_id, username or "", first_name or ""],
    )


async def get_user(user_id: int) -> Optional[dict]:
    rs = await _client.execute("SELECT * FROM users WHERE user_id = ?", [user_id])
    if not rs.rows:
        return None
    return dict(zip(rs.columns, rs.rows[0]))


# ---------- subscriptions ----------

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
