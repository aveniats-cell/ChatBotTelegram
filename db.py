"""Turso DB через HTTP-транспорт (libsql-client 0.3.1)."""
import os
import uuid
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
    """Заглушка результата. error=True — запрос упал (сеть/БД).

    Вызывающий код может отличить «нет строк» (error=False, rows=[]) от
    «запрос не выполнился» (error=True). Для критичных операций (свадьбы,
    экономика) это важно: нельзя писать «не найдено», если на самом деле
    Turso моргнул.
    """

    def __init__(self, error: bool = False) -> None:
        self.rows: list = []
        self.rows_affected: int = 0
        self.error: bool = error


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


async def _exec(
    sql: str,
    params: Optional[list] = None,
    retries: int = 2,
) -> _EmptyResult:
    """Обёртка над _client.execute с ретраями.

    - Ретраи только для read-only (SELECT/PRAGMA), чтобы не дублировать
      INSERT/UPDATE/DELETE.
    - При ошибке возвращает _EmptyResult(error=True), а не «пусто».
    """
    if _client is None:
        logger.error("db: _client не инициализирован (sql: %.80s)", sql)
        return _EmptyResult(error=True)

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
            continue
        except Exception as e:
            # Прочие ошибки (SQL, IntegrityError и т.п.) — не ретраим,
            # но логируем как exception, чтобы не терять стек.
            logger.exception("db exec error (sql: %.80s): %s", sql, e)
            return _EmptyResult(error=True)

    if last_exc is not None:
        logger.error("db exec failed (sql: %.80s): %s", sql, last_exc)
    return _EmptyResult(error=True)


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
    # marriages: id без AUTOINCREMENT — генерируем в Python (uuid),
    # чтобы не зависеть от last_insert_rowid() на Turso HTTP.
    await _exec("""
        CREATE TABLE IF NOT EXISTS marriages (
            id           INTEGER PRIMARY KEY,
            chat_id      INTEGER NOT NULL,
            user1_id     INTEGER NOT NULL,
            user2_id     INTEGER NOT NULL,
            created_at   TEXT DEFAULT (datetime('now')),
            married_at   TEXT,
            active       INTEGER NOT NULL DEFAULT 0,
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
        # миграция marriages: добавляем married_at, если таблица старая
        ("marriages", "married_at", "TEXT"),
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
#
# Ключевые изменения:
# 1. id генерируется в Python (uuid), а не через last_insert_rowid() —
#    на Turso HTTP last_insert_rowid() не привязан к «этому» INSERT.
# 2. accept_marriage атомарно проверяет, что proposal ещё active=0 и что
#    ни у одного из участников нет другого активного брака.
# 3. delete_marriage возвращает rows_affected — чтобы отличать «удалил»
#    от «уже удалён».
# 4. married_at фиксируется в момент accept; _marriage_level считает от него.

def _new_marriage_id() -> int:
    """62-битный положительный int — влезает в SQLite INTEGER (64-bit)."""
    return uuid.uuid4().int & ((1 << 62) - 1)


async def create_proposal(chat_id: int, user1_id: int, user2_id: int,
                          forced: bool = False) -> int:
    """Создаёт запись брака. Обычный proposal: active=0. Принудительный: active=1.

    Возвращает id созданной записи или 0 при ошибке БД.
    """
    new_id = _new_marriage_id()
    active = 1 if forced else 0
    forced_flag = 1 if forced else 0

    # Для принудительного брака сразу ставим married_at.
    if forced:
        rs = await _exec(
            """INSERT INTO marriages
                 (id, chat_id, user1_id, user2_id, active, forced, married_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            [new_id, chat_id, user1_id, user2_id, active, forced_flag],
        )
    else:
        rs = await _exec(
            """INSERT INTO marriages
                 (id, chat_id, user1_id, user2_id, active, forced)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [new_id, chat_id, user1_id, user2_id, active, forced_flag],
        )

    if getattr(rs, "error", False):
        logger.error("create_proposal: ошибка БД (chat=%s)", chat_id)
        return 0
    return new_id


async def accept_marriage(marriage_id: int) -> int:
    """Активирует proposal. Возвращает rows_affected (1 — успех, 0 — нельзя).

    Условия: запись active=0 и ни у одного из участников нет другого
    активного брака в этом чате.
    """
    rs = await _exec(
        """UPDATE marriages SET active = 1, married_at = datetime('now')
           WHERE id = ?
             AND active = 0
             AND NOT EXISTS (
                 SELECT 1 FROM marriages m2
                 WHERE m2.chat_id = marriages.chat_id
                   AND m2.active = 1
                   AND m2.id != marriages.id
                   AND (
                       m2.user1_id IN (marriages.user1_id, marriages.user2_id)
                       OR m2.user2_id IN (marriages.user1_id, marriages.user2_id)
                   )
             )""",
        [marriage_id],
    )
    return getattr(rs, "rows_affected", 0) or 0


async def delete_marriage(marriage_id: int) -> int:
    """Удаляет запись. Возвращает rows_affected (0 — уже удалена)."""
    rs = await _exec("DELETE FROM marriages WHERE id = ?", [marriage_id])
    return getattr(rs, "rows_affected", 0) or 0


async def get_marriage(marriage_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, chat_id, user1_id, user2_id, created_at, married_at,
                  active, forced
           FROM marriages WHERE id = ?""",
        [marriage_id],
    )
    if getattr(rs, "error", False):
        # Не маскируем ошибку БД под «не найдено» — пусть вызывающий
        # решит, что показать. Возвращаем None, но логируем.
        logger.error("get_marriage(%s): ошибка БД", marriage_id)
        return None
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "chat_id": r[1], "user1_id": r[2],
        "user2_id": r[3], "created_at": r[4], "married_at": r[5],
        "active": bool(r[6]), "forced": bool(r[7]),
    }


async def find_active_marriage(chat_id: int, user_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, chat_id, user1_id, user2_id, created_at, married_at,
                  active, forced
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
        "user2_id": r[3], "created_at": r[4], "married_at": r[5],
        "active": bool(r[6]), "forced": bool(r[7]),
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
