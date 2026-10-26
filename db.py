"""Turso DB через HTTP-транспорт (libsql-client 0.3.1)."""
import os
import asyncio
import logging
from typing import Optional, Any, List

import aiohttp
from libsql_client import create_client

logger = logging.getLogger(__name__)

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

_client: Optional[Any] = None


class _EmptyResult:
    """Заглушка, если Turso вернул ошибку — чтобы бот не падал."""

    def __init__(self) -> None:
        self.rows: list = []
        self.rows_affected: int = 0


def _http_url(url: str) -> str:
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


def _url_with_token(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}authToken={token}"


def _is_read_only(sql: str) -> bool:
    """True, если запрос можно безопасно повторять (SELECT/PRAGMA)."""
    s = sql.lstrip().upper()
    return s.startswith("SELECT") or s.startswith("PRAGMA")


async def _exec(sql: str, params: Optional[list] = None, retries: int = 2):
    """Обёртка над _client.execute с ретраями.

    Ретраи применяются только к read-only запросам, чтобы не дублировать
    INSERT/UPDATE/DELETE на повторной попытке.
    """
    if _client is None:
        logger.error("db: _client не инициализирован (sql: %.80s)", sql)
        return _EmptyResult()

    max_retries = retries if _is_read_only(sql) else 0
    last_exc: Optional[BaseException] = None

    for attempt in range(max_retries + 1):
        try:
            if params is None:
                return await _client.execute(sql)
            return await _client.execute(sql, params)
        except (KeyError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt < max_retries:
                logger.warning(
                    "db retry %d/%d (sql: %.60s): %s",
                    attempt + 1, max_retries, sql, e,
                )
                await asyncio.sleep(0.2 * (attempt + 1))

    if last_exc is not None:
        logger.error("db exec failed (sql: %.80s): %s", sql, last_exc)
    return _EmptyResult()


async def init_db() -> None:
    global _client
    if not TURSO_URL or not TURSO_TOKEN:
        raise SystemExit("TURSO_DATABASE_URL и/или TURSO_AUTH_TOKEN не заданы")
    if _client is None:
        _client = create_client(_url_with_token(_http_url(TURSO_URL), TURSO_TOKEN))
    await _create_tables()
    await _migrate_tables()
    logger.info("DB инициализирована")


async def _create_tables() -> None:
    await _exec("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id      INTEGER PRIMARY KEY,
            is_subscribed INTEGER NOT NULL DEFAULT 0,
            checked_at   TEXT
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS roles (
            user_id    INTEGER PRIMARY KEY,
            role       TEXT NOT NULL,
            granted_by INTEGER,
            granted_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS punishments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            until_ts   INTEGER,
            issued_by  INTEGER,
            issued_at  TEXT DEFAULT (datetime('now')),
            active     INTEGER NOT NULL DEFAULT 1
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            count     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS messages_day (
            chat_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            day       TEXT NOT NULL,
            count     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, day)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS marriages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            user1_id     INTEGER NOT NULL,
            user2_id     INTEGER NOT NULL,
            created_at   TEXT DEFAULT (datetime('now')),
            active       INTEGER NOT NULL DEFAULT 1,
            forced       INTEGER NOT NULL DEFAULT 0
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS marriage_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            user1_id    INTEGER NOT NULL,
            user2_id    INTEGER NOT NULL,
            event       TEXT NOT NULL,
            event_at    TEXT DEFAULT (datetime('now')),
            by_user_id  INTEGER
        )
    """)


async def _migrate_tables() -> None:
    adds = [
        ("users", "balance", "INTEGER NOT NULL DEFAULT 10"),
        ("users", "credit", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "sss_member", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "mutes_issued", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "reports_made", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "courts_held", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "tictactoe_wins", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "tictactoe_losses", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "tictactoe_draws", "INTEGER NOT NULL DEFAULT 0"),
    ]

    existing: dict = {}
    for table in {t for t, _, _ in adds}:
        rs = await _exec(f"PRAGMA table_info({table})")
        existing[table] = {row[1] for row in rs.rows}

    for table, col, decl in adds:
        if col in existing[table]:
            continue
        await _exec(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# ---------- users ----------

async def upsert_user(user_id: int, username: Optional[str], first_name: str) -> None:
    await _exec(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name""",
        [user_id, username or "", first_name or ""],
    )


async def get_user(user_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT user_id, username, first_name, created_at,
                  balance, credit, sss_member,
                  mutes_issued, reports_made, courts_held,
                  tictactoe_wins, tictactoe_losses, tictactoe_draws
           FROM users WHERE user_id = ?""",
        [user_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    keys = ["user_id", "username", "first_name", "created_at",
            "balance", "credit", "sss_member",
            "mutes_issued", "reports_made", "courts_held",
            "tictactoe_wins", "tictactoe_losses", "tictactoe_draws"]
    return dict(zip(keys, r))


async def get_user_by_username(username: str) -> Optional[int]:
    if not username:
        return None
    rs = await _exec(
        "SELECT user_id FROM users WHERE username = ? COLLATE NOCASE",
        [username.lstrip("@")],
    )
    return rs.rows[0][0] if rs.rows else None


async def inc_user_counter(user_id: int, field: str, delta: int = 1) -> None:
    allowed = {"mutes_issued", "reports_made", "courts_held",
               "tictactoe_wins", "tictactoe_losses", "tictactoe_draws"}
    if field not in allowed:
        return
    await _exec(
        f"UPDATE users SET {field} = COALESCE({field}, 0) + ? WHERE user_id = ?",
        [delta, user_id],
    )


async def set_sss(user_id: int, is_member: bool) -> None:
    await _exec(
        "UPDATE users SET sss_member = ? WHERE user_id = ?",
        [1 if is_member else 0, user_id],
    )


async def list_sss_members() -> List[int]:
    """Возвращает user_id всех, у кого sss_member = 1 (для восстановления
    состава ССС после рестарта)."""
    rs = await _exec("SELECT user_id FROM users WHERE sss_member = 1")
    return [row[0] for row in rs.rows]


async def add_balance(user_id: int, delta: int) -> None:
    await _exec(
        "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
        [delta, user_id],
    )


async def add_credit(user_id: int, delta: int) -> None:
    await _exec(
        "UPDATE users SET credit = COALESCE(credit, 0) + ? WHERE user_id = ?",
        [delta, user_id],
    )


async def apply_credit_interest(percent: float) -> int:
    """Начисляет проценты на остаток долга. Возвращает rows_affected."""
    rs = await _exec(
        "UPDATE users SET credit = credit + CAST(credit * ? / 100 AS INTEGER) "
        "WHERE credit > 0",
        [percent],
    )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- subscriptions ----------

async def set_subscription(user_id: int, is_subscribed: bool) -> None:
    await _exec(
        """INSERT INTO subscriptions (user_id, is_subscribed, checked_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             is_subscribed=excluded.is_subscribed,
             checked_at=excluded.checked_at""",
        [user_id, 1 if is_subscribed else 0],
    )


async def get_subscription(user_id: int) -> Optional[bool]:
    rs = await _exec(
        "SELECT is_subscribed FROM subscriptions WHERE user_id = ?", [user_id]
    )
    if not rs.rows:
        return None
    return bool(rs.rows[0][0])


# ---------- roles ----------

async def set_role(user_id: int, role: str, granted_by: int) -> None:
    await _exec(
        """INSERT INTO roles (user_id, role, granted_by, granted_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             role=excluded.role,
             granted_by=excluded.granted_by,
             granted_at=excluded.granted_at""",
        [user_id, role, granted_by],
    )


async def remove_role(user_id: int) -> None:
    await _exec("DELETE FROM roles WHERE user_id = ?", [user_id])


async def get_role(user_id: int) -> Optional[str]:
    rs = await _exec("SELECT role FROM roles WHERE user_id = ?", [user_id])
    if not rs.rows:
        return None
    return rs.rows[0][0]


async def list_roles(role: str) -> List[int]:
    rs = await _exec(
        "SELECT user_id FROM roles WHERE role = ? ORDER BY granted_at", [role]
    )
    return [row[0] for row in rs.rows]


# ---------- punishments ----------

async def add_punishment(user_id: int, chat_id: int, kind: str,
                          until_ts: Optional[int], issued_by: int) -> None:
    await _exec(
        """INSERT INTO punishments (user_id, chat_id, kind, until_ts, issued_by)
           VALUES (?, ?, ?, ?, ?)""",
        [user_id, chat_id, kind, until_ts, issued_by],
    )


async def deactivate_punishment(user_id: int, chat_id: int, kind: str) -> int:
    rs = await _exec(
        """UPDATE punishments SET active = 0
           WHERE user_id = ? AND chat_id = ? AND kind = ? AND active = 1""",
        [user_id, chat_id, kind],
    )
    return getattr(rs, "rows_affected", 0) or 0


async def get_active_punishment(user_id: int, chat_id: int, kind: str) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, until_ts FROM punishments
           WHERE user_id = ? AND chat_id = ? AND kind = ? AND active = 1
           ORDER BY id DESC LIMIT 1""",
        [user_id, chat_id, kind],
    )
    if not rs.rows:
        return None
    row = rs.rows[0]
    return {"id": row[0], "until_ts": row[1]}


# ---------- messages ----------

async def inc_message(chat_id: int, user_id: int) -> None:
    await _exec(
        """INSERT INTO messages (chat_id, user_id, count)
           VALUES (?, ?, 1)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             count = count + 1""",
        [chat_id, user_id],
    )


async def top_messages(chat_id: int, limit: int = 50) -> List[tuple]:
    rs = await _exec(
        """SELECT user_id, count FROM messages
           WHERE chat_id = ? ORDER BY count DESC LIMIT ?""",
        [chat_id, limit],
    )
    return [(r[0], r[1]) for r in rs.rows]


async def user_rank(chat_id: int, user_id: int) -> Optional[tuple]:
    rs = await _exec(
        "SELECT count FROM messages WHERE chat_id = ? AND user_id = ?",
        [chat_id, user_id],
    )
    if not rs.rows:
        return None
    count = rs.rows[0][0]

    rs2 = await _exec(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND count > ?",
        [chat_id, count],
    )
    rank = ((rs2.rows[0][0] if rs2.rows else 0) or 0) + 1

    rs3 = await _exec(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ?", [chat_id]
    )
    total = (rs3.rows[0][0] if rs3.rows else 0) or 0
    return count, rank, total


# ---------- messages_day ----------

async def inc_message_day(chat_id: int, user_id: int, day: str) -> None:
    await _exec(
        """INSERT INTO messages_day (chat_id, user_id, day, count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             count = count + 1""",
        [chat_id, user_id, day],
    )


async def top_messages_day(chat_id: int, day: str, limit: int = 50) -> List[tuple]:
    rs = await _exec(
        """SELECT user_id, count FROM messages_day
           WHERE chat_id = ? AND day = ? ORDER BY count DESC LIMIT ?""",
        [chat_id, day, limit],
    )
    return [(r[0], r[1]) for r in rs.rows]


async def cleanup_old_days(keep_days: int = 0) -> int:
    if keep_days == 0:
        rs = await _exec("DELETE FROM messages_day WHERE day < date('now')")
    else:
        rs = await _exec(
            "DELETE FROM messages_day WHERE day < date('now', ?)",
            [f"-{keep_days} days"],
        )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- marriages ----------

async def create_proposal(chat_id: int, user1_id: int, user2_id: int,
                          forced: bool = False) -> int:
    # RETURNING id — атомарно в одном запросе. Без этого last_insert_rowid()
    # в HTTP-транспорте может уйти на другой коннект и вернуть чужой/0 id.
    rs = await _exec(
        """INSERT INTO marriages (chat_id, user1_id, user2_id, active, forced)
           VALUES (?, ?, ?, ?, ?)
           RETURNING id""",
        [chat_id, user1_id, user2_id, 1 if forced else 0, 1 if forced else 0],
    )
    return rs.rows[0][0] if rs.rows else 0


async def accept_marriage(marriage_id: int) -> None:
    await _exec("UPDATE marriages SET active = 1 WHERE id = ?", [marriage_id])


async def delete_marriage(marriage_id: int) -> None:
    await _exec("DELETE FROM marriages WHERE id = ?", [marriage_id])


async def get_marriage(marriage_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, chat_id, user1_id, user2_id, created_at, active, forced
           FROM marriages WHERE id = ?""",
        [marriage_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "chat_id": r[1], "user1_id": r[2],
        "user2_id": r[3], "created_at": r[4], "active": bool(r[5]),
        "forced": bool(r[6]),
    }


async def find_active_marriage(chat_id: int, user_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, chat_id, user1_id, user2_id, created_at, active, forced
           FROM marriages
           WHERE chat_id = ? AND active = 1
             AND (user1_id = ? OR user2_id = ?)
           LIMIT 1""",
        [chat_id, user_id, user_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "chat_id": r[1], "user1_id": r[2],
        "user2_id": r[3], "created_at": r[4], "active": bool(r[5]),
        "forced": bool(r[6]),
    }


async def log_marriage_event(chat_id: int, user1_id: int, user2_id: int,
                             event: str, by_user_id: Optional[int] = None) -> None:
    await _exec(
        """INSERT INTO marriage_history (chat_id, user1_id, user2_id, event, by_user_id)
           VALUES (?, ?, ?, ?, ?)""",
        [chat_id, user1_id, user2_id, event, by_user_id],
    )


async def count_divorces(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        """SELECT COUNT(*) FROM marriage_history
           WHERE chat_id = ? AND event IN ('divorced', 'forced_divorced')
             AND (user1_id = ? OR user2_id = ?)""",
        [chat_id, user_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0
