import os
import uuid
import asyncio
import logging
from typing import Optional, Any, List

import aiohttp
from libsql_client import create_client

logger = logging.getLogger(__name__)

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

_client: Optional[Any] = None

XP_PER_LEVEL = 50
_MAX_SQLITE_INT = 2**63 - 1

_ALLOWED_COMPLAINT_STATUSES = {"resolved", "rejected", "pending"}


class _EmptyResult:
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
    s = sql.lstrip().upper()
    return s.startswith("SELECT") or s.startswith("PRAGMA")


def _is_idempotent_write(sql: str) -> bool:
    """Проверяет, можно ли безопасно повторить запись после сетевого сбоя.

    Разрешены только DELETE с WHERE и INSERT OR IGNORE.
    Обычные UPSERT/UPDATE могут менять данные повторно.
    """
    s = sql.lstrip().upper()
    if s.startswith("DELETE") and " WHERE " in s:
        return True
    # UPSERT может содержать инкремент в ON CONFLICT DO UPDATE.
    # Повтор такого запроса после сетевого тайм-аута способен задвоить запись.
    # Безопасно повторять только INSERT OR IGNORE.
    if s.startswith("INSERT OR IGNORE"):
        return True
    return False


async def _exec(
    sql: str,
    params: Optional[list] = None,
    retries: int = 2,
) -> _EmptyResult:
    if _client is None:
        logger.error("db: _client не инициализирован (sql: %.80s)", sql)
        return _EmptyResult(error=True)

    if _is_read_only(sql):
        max_retries = retries
    elif _is_idempotent_write(sql):
        max_retries = 1
    else:
        max_retries = 0

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
            logger.exception("db exec error (sql: %.80s): %s", sql, e)
            return _EmptyResult(error=True)

    if last_exc is not None:
        logger.warning("db exec failed (sql: %.80s): %s", sql, last_exc)
    return _EmptyResult(error=True)


async def init_db() -> None:
    global _client
    if not TURSO_URL or not TURSO_AUTH_TOKEN:
        raise SystemExit("TURSO_DATABASE_URL и/или TURSO_AUTH_TOKEN не заданы")
    if _client is None:
        _client = create_client(_url_with_token(_http_url(TURSO_URL), TURSO_AUTH_TOKEN))
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
        CREATE TABLE IF NOT EXISTS messages_idempotency (
            key        TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS treasury (
            chat_id  INTEGER PRIMARY KEY,
            balance  INTEGER NOT NULL DEFAULT 0
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS treasury_meta (
            chat_id         INTEGER PRIMARY KEY,
            last_default_at TEXT
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS marriages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            user1_id     INTEGER NOT NULL,
            user2_id     INTEGER NOT NULL,
            created_at   TEXT DEFAULT (datetime('now')),
            married_at   TEXT,
            active       INTEGER NOT NULL DEFAULT 0,
            forced       INTEGER NOT NULL DEFAULT 0,
            client_token TEXT,
            xp           INTEGER NOT NULL DEFAULT 0
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
    await _exec("""
        CREATE TABLE IF NOT EXISTS warnings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            issued_by   INTEGER NOT NULL,
            reason      TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            expires_at  TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS complaints (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL,
            target_id     INTEGER NOT NULL,
            reporter_id   INTEGER NOT NULL,
            message_id    INTEGER,
            created_at    TEXT DEFAULT (datetime('now')),
            status        TEXT NOT NULL DEFAULT 'pending',
            resolved_by   INTEGER,
            resolved_at   TEXT
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS aura (
            chat_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            value         INTEGER NOT NULL DEFAULT 0,
            last_droch_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS awards (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            title       TEXT NOT NULL,
            stars       INTEGER NOT NULL,
            issued_by   INTEGER NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS action_stats (
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            action     TEXT NOT NULL,
            count      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, user_id, action)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS pending_salary (
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            day        TEXT NOT NULL,
            frac_delta INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, user_id, day)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS user_cosmetics (
            user_id        INTEGER PRIMARY KEY,
            title          TEXT,
            badge          TEXT,
            frame          TEXT,
            color          TEXT,
            action_prefix  TEXT,
            custom_trigger TEXT
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_emitters (
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            granted_by INTEGER,
            granted_at TEXT DEFAULT (datetime('now')),
            active     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coins (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id        INTEGER NOT NULL,
            creator_id     INTEGER NOT NULL,
            name           TEXT NOT NULL,
            description    TEXT NOT NULL,
            start_price    INTEGER NOT NULL,
            total_supply   INTEGER NOT NULL,
            sold           INTEGER NOT NULL DEFAULT 0,
            yield_per_day  INTEGER NOT NULL DEFAULT 0,
            photo_file_id  TEXT,
            photo_hash     TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            active         INTEGER NOT NULL DEFAULT 1
        )
    """)
    await _exec("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_coins_chat_name
        ON coins(chat_id, name COLLATE NOCASE)
    """)
    await _exec("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_coins_chat_photo
        ON coins(chat_id, photo_hash) WHERE photo_hash IS NOT NULL
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_holders (
            coin_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            quantity  INTEGER NOT NULL DEFAULT 0,
            invested  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (coin_id, user_id)
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_price_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id      INTEGER NOT NULL,
            ts           TEXT DEFAULT (datetime('now')),
            price        INTEGER NOT NULL,
            sold         INTEGER NOT NULL
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_income_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            day      TEXT NOT NULL,
            amount   INTEGER NOT NULL,
            ts       TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_market_lots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id    INTEGER NOT NULL,
            seller_id  INTEGER NOT NULL,
            quantity   INTEGER NOT NULL,
            price      INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            active     INTEGER NOT NULL DEFAULT 1
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS coin_purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id     INTEGER NOT NULL,
            buyer_id    INTEGER NOT NULL,
            quantity    INTEGER NOT NULL,
            price_paid  INTEGER NOT NULL,
            ts          TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS shop_pockets (
            id          INTEGER PRIMARY KEY,
            chat_id     INTEGER,
            name        TEXT NOT NULL,
            price       INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS shop_commands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger     TEXT NOT NULL,
            template    TEXT NOT NULL,
            pocket_id   INTEGER,
            free        INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    await _exec("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_cmd_trigger
        ON shop_commands(trigger COLLATE NOCASE)
    """)
    await _exec("""
        CREATE TABLE IF NOT EXISTS shop_purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            pocket_id   INTEGER NOT NULL,
            price_paid  INTEGER NOT NULL,
            bought_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(chat_id, user_id, pocket_id)
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
        ("users", "credit_taken_day", "TEXT"),
        ("users", "salary_today", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "salary_day", "TEXT"),
        ("users", "salary_frac", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "avatar_file_id", "TEXT"),
        ("users", "coins_created", "INTEGER NOT NULL DEFAULT 0"),
        ("marriages", "married_at", "TEXT"),
        ("marriages", "client_token", "TEXT"),
        ("marriages", "xp", "INTEGER NOT NULL DEFAULT 0"),
        ("coins", "reserve", "INTEGER NOT NULL DEFAULT 0"),
        ("coins", "circulating", "INTEGER NOT NULL DEFAULT 0"),
        ("coins", "dividend_pool", "INTEGER NOT NULL DEFAULT 0"),
    ]

    existing: dict = {}
    for table in {t for t, _, _ in adds}:
        rs = await _exec(f"PRAGMA table_info({table})")
        existing[table] = {row[1] for row in rs.rows}

    for table, col, decl in adds:
        if col in existing[table]:
            continue
        await _exec(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    idx_stmts = [
        "CREATE INDEX IF NOT EXISTS idx_marriages_chat_active "
        "ON marriages(chat_id, active)",
        "CREATE INDEX IF NOT EXISTS idx_marriages_client_token "
        "ON marriages(client_token)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marriages_client_token "
        "ON marriages(client_token) WHERE client_token IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_marriage_history_chat_users "
        "ON marriage_history(chat_id, user1_id, user2_id)",
        "CREATE INDEX IF NOT EXISTS idx_warnings_user_active "
        "ON warnings(chat_id, user_id, active)",
        "CREATE INDEX IF NOT EXISTS idx_warnings_expires "
        "ON warnings(expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_complaints_status "
        "ON complaints(chat_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_complaints_reporter "
        "ON complaints(chat_id, reporter_id)",
        "CREATE INDEX IF NOT EXISTS idx_complaints_target "
        "ON complaints(chat_id, target_id)",
        "CREATE INDEX IF NOT EXISTS idx_aura_chat_value "
        "ON aura(chat_id, value DESC)",
        "CREATE INDEX IF NOT EXISTS idx_awards_user "
        "ON awards(chat_id, user_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_action_stats_chat_action "
        "ON action_stats(chat_id, action, count DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pending_salary_day "
        "ON pending_salary(day)",
        "CREATE INDEX IF NOT EXISTS idx_coin_holders_user "
        "ON coin_holders(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_coin_price_history_coin "
        "ON coin_price_history(coin_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_coin_market_lots_coin "
        "ON coin_market_lots(coin_id, active)",
        "CREATE INDEX IF NOT EXISTS idx_coin_purchases_coin "
        "ON coin_purchases(coin_id)",
        "CREATE INDEX IF NOT EXISTS idx_coin_emitters_chat "
        "ON coin_emitters(chat_id, active)",
    ]
    for sql in idx_stmts:
        rs = await _exec(sql)
        if getattr(rs, "error", False):
            logger.warning("migrate: не удалось выполнить: %s", sql)

    await _backfill_coin_assets()


async def _backfill_coin_assets() -> None:
    """Заполнить reserve/circulating у монет, созданных до ."""
    rs = await _exec(
        """SELECT id, start_price, sold, reserve, circulating
           FROM coins
           WHERE (circulating IS NULL OR circulating = 0) AND sold > 0"""
    )
    if getattr(rs, "error", False) or not rs.rows:
        return
    for r in rs.rows:
        coin_id = r[0]
        start_price = int(r[1] or 0)
        sold = int(r[2] or 0)
        reserve = int(r[3] or 0)
        new_circ = sold
        new_reserve = reserve if reserve > 0 else min(_MAX_SQLITE_INT, max(0, start_price * sold))
        await _exec(
            """UPDATE coins SET circulating = ?, reserve = ?
               WHERE id = ?""",
            [new_circ, new_reserve, coin_id],
        )
    logger.info("backfill coins: обработано %d монет", len(rs.rows))


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
                  tictactoe_wins, tictactoe_losses, tictactoe_draws,
                  credit_taken_day, salary_today, salary_day, salary_frac,
                  avatar_file_id, coins_created
           FROM users WHERE user_id = ?""",
        [user_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    keys = ["user_id", "username", "first_name", "created_at",
            "balance", "credit", "sss_member",
            "mutes_issued", "reports_made", "courts_held",
            "tictactoe_wins", "tictactoe_losses", "tictactoe_draws",
            "credit_taken_day", "salary_today", "salary_day", "salary_frac",
            "avatar_file_id", "coins_created"]
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


async def inc_user_counter_coin_created(user_id: int) -> None:
    await _exec(
        "UPDATE users SET coins_created = COALESCE(coins_created, 0) + 1 "
        "WHERE user_id = ?",
        [user_id],
    )


async def set_sss(user_id: int, is_member: bool) -> bool:
    rs = await _exec(
        "UPDATE users SET sss_member = ? WHERE user_id = ?",
        [1 if is_member else 0, user_id],
    )
    if getattr(rs, "error", False):
        return False
    return True


async def list_sss_members() -> List[int]:
    rs = await _exec("SELECT user_id FROM users WHERE sss_member = 1")
    return [row[0] for row in rs.rows]


async def add_balance(user_id: int, delta: int) -> bool:
    """Атомарное начисление: один INSERT ... ON CONFLICT, без гонки."""
    rs = await _exec(
        """INSERT INTO users (user_id, balance)
           VALUES (?, 10 + ?)
           ON CONFLICT(user_id) DO UPDATE SET
             balance = COALESCE(users.balance, 10) + ?""",
        [user_id, delta, delta],
    )
    return not getattr(rs, "error", False)


async def take_balance_atomic(user_id: int, amount: int) -> bool:
    if amount <= 0:
        return False
    rs = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) - ?
           WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
        [amount, user_id, amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def take_balance_any(user_id: int, amount: int) -> int:
    if amount <= 0:
        return 0
    rs = await _exec(
        "SELECT COALESCE(balance, 10) FROM users WHERE user_id = ?", [user_id],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return 0
    bal = rs.rows[0][0] or 0
    if bal <= 0:
        return 0
    take = min(amount, bal)
    rs2 = await _exec(
        """UPDATE users SET balance = COALESCE(balance, 10) - ?
           WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
        [take, user_id, take],
    )
    if getattr(rs2, "error", False) or (getattr(rs2, "rows_affected", 0) or 0) == 0:
        return 0
    return take


async def burn_balance(user_id: int, amount: int) -> int:
    return await take_balance_any(user_id, amount)


async def add_credit(user_id: int, delta: int) -> None:
    await _exec(
        "UPDATE users SET credit = COALESCE(credit, 0) + ? WHERE user_id = ?",
        [delta, user_id],
    )


async def set_credit_taken_day(user_id: int, day: str) -> None:
    await _exec(
        "UPDATE users SET credit_taken_day = ? WHERE user_id = ?",
        [day, user_id],
    )


async def take_credit_atomic(user_id: int, amount: int) -> bool:
    if amount <= 0:
        return False
    rs = await _exec(
        """UPDATE users
           SET credit  = COALESCE(credit, 0) + ?,
               balance = COALESCE(balance, 10) + ?
           WHERE user_id = ?""",
        [amount, amount, user_id],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def take_credit_daily_atomic(
    user_id: int, amount: int, today: str, balance_floor: int = 0,
) -> bool:
    if amount <= 0 or balance_floor < 0:
        return False
    rs = await _exec(
        """UPDATE users
           SET credit           = COALESCE(credit, 0) + ?,
               balance          = COALESCE(balance, 10) + ?,
               credit_taken_day = ?
           WHERE user_id = ?
             AND COALESCE(balance, 10) >= ?
             AND COALESCE(credit, 0) = 0
             AND (credit_taken_day IS NULL OR credit_taken_day != ?)""",
        [amount, amount, today, user_id, balance_floor, today],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def repay_credit_atomic(user_id: int, amount: int) -> bool:
    if amount <= 0:
        return False
    rs = await _exec(
        """UPDATE users
           SET credit  = COALESCE(credit, 0) - ?,
               balance = COALESCE(balance, 10) - ?
           WHERE user_id = ?
             AND COALESCE(balance, 0) >= ?
             AND COALESCE(credit, 0) >= ?""",
        [amount, amount, user_id, amount, amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def apply_credit_interest(percent: float) -> int:
    rs = await _exec(
        "UPDATE users SET credit = credit + CAST(credit * ? / 100 AS INTEGER) "
        "WHERE credit > 0",
        [percent],
    )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- treasury ----------

async def treasury_add(chat_id: int, amount: int) -> bool:
    if amount == 0:
        return True
    rs = await _exec(
        """INSERT INTO treasury (chat_id, balance)
           VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
             balance = balance + excluded.balance""",
        [chat_id, amount],
    )
    return not getattr(rs, "error", False)


async def treasury_get(chat_id: int) -> int:
    rs = await _exec("SELECT balance FROM treasury WHERE chat_id = ?", [chat_id])
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def treasury_reset(chat_id: int) -> int:
    rs = await _exec(
        "UPDATE treasury SET balance = 0 WHERE chat_id = ?", [chat_id],
    )
    return getattr(rs, "rows_affected", 0) or 0


async def treasury_withdraw(chat_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    rs = await _exec(
        """UPDATE treasury SET balance = balance - ?
           WHERE chat_id = ? AND balance >= ?""",
        [amount, chat_id, amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def burn_treasury(chat_id: int, amount: int) -> int:
    if amount <= 0:
        return 0
    cur = await treasury_get(chat_id)
    if cur <= 0:
        return 0
    take = min(amount, cur)
    await _exec(
        "UPDATE treasury SET balance = balance - ? WHERE chat_id = ?",
        [take, chat_id],
    )
    return take


async def treasury_trim(chat_id: int, keep: int) -> int:
    cur = await treasury_get(chat_id)
    if cur <= keep:
        return 0
    burned = cur - keep
    await _exec(
        "UPDATE treasury SET balance = ? WHERE chat_id = ?",
        [keep, chat_id],
    )
    return burned


async def treasury_default_collect(chat_id: int) -> int:
    """Собирает баланс только у пользователей, писавших в этом чате.

    Сначала читаем снимок user_id/balance, затем списываем каждую сумму
    через compare-and-set (WHERE balance = старое_значение). Если баланс
    успел измениться, строка пропускается: новое пополнение не стирается.
    """
    rs = await _exec(
        """SELECT user_id, balance
           FROM users
           WHERE balance > 0
             AND EXISTS (
                 SELECT 1 FROM messages m
                 WHERE m.user_id = users.user_id AND m.chat_id = ?
             )""",
        [chat_id],
    )
    if getattr(rs, "error", False):
        logger.error("treasury_default_collect: не удалось прочитать балансы")
        return 0

    collected = 0
    for row in rs.rows:
        user_id, balance = row[0], row[1]
        balance = int(balance or 0)
        if balance <= 0:
            continue
        changed = await _exec(
            """UPDATE users SET balance = 0
               WHERE user_id = ? AND balance = ? AND balance > 0
                 AND EXISTS (
                     SELECT 1 FROM messages m
                     WHERE m.user_id = users.user_id AND m.chat_id = ?
                 )""",
            [user_id, balance, chat_id],
        )
        if getattr(changed, "error", False):
            logger.warning(
                "treasury_default_collect: ошибка списания user_id=%s", user_id,
            )
            continue
        if (getattr(changed, "rows_affected", 0) or 0) > 0:
            collected += balance

    if collected <= 0:
        return 0

    ok = await treasury_add(chat_id, collected)
    if not ok:
        logger.error(
            "treasury_default_collect: не удалось зачислить %s в казну chat=%s",
            collected, chat_id,
        )
        return 0

    logger.info(
        "treasury_default_collect: собрано %s (chat=%s)", collected, chat_id,
    )
    return collected


async def set_last_default(chat_id: int, ts: str) -> None:
    await _exec(
        """INSERT INTO treasury_meta (chat_id, last_default_at)
           VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
             last_default_at = excluded.last_default_at""",
        [chat_id, ts],
    )


async def get_last_default(chat_id: int) -> Optional[str]:
    rs = await _exec(
        "SELECT last_default_at FROM treasury_meta WHERE chat_id = ?", [chat_id],
    )
    if not rs.rows:
        return None
    return rs.rows[0][0]


# ---------- tax ----------

async def tax_active_balances(chat_id: int, min_balance: int, pct: int,
                              days: int = 1) -> int:
    """Списывает налог с активных пользователей; возвращает сумму списания.

    Для каждой строки используется compare-and-set: если баланс изменился
    после SELECT, налог с этой строки в этом проходе не списывается.
    """
    if pct <= 0 or pct > 100 or min_balance < 0 or days < 0:
        return 0
    days_mod = f"-{int(days)} days"

    rs = await _exec(
        """SELECT user_id, balance
           FROM users
           WHERE balance > ?
             AND EXISTS (
                 SELECT 1 FROM messages_day m
                 WHERE m.user_id = users.user_id
                   AND m.chat_id = ?
                   AND m.day >= date('now', ?)
             )""",
        [min_balance, chat_id, days_mod],
    )
    if getattr(rs, "error", False):
        logger.error("tax_active_balances: не удалось прочитать балансы")
        return 0

    collected = 0
    for row in rs.rows:
        user_id, raw_balance = row[0], row[1]
        balance = int(raw_balance or 0)
        if balance <= min_balance:
            continue
        tax = (balance * pct) // 100
        if tax <= 0:
            continue
        changed = await _exec(
            """UPDATE users SET balance = balance - ?
               WHERE user_id = ? AND balance = ? AND balance > ?
                 AND EXISTS (
                     SELECT 1 FROM messages_day m
                     WHERE m.user_id = users.user_id
                       AND m.chat_id = ?
                       AND m.day >= date('now', ?)
                 )""",
            [tax, user_id, balance, min_balance, chat_id, days_mod],
        )
        if getattr(changed, "error", False):
            logger.warning("tax_active_balances: ошибка user_id=%s", user_id)
            continue
        if (getattr(changed, "rows_affected", 0) or 0) > 0:
            collected += tax
    return collected


# ---------- transfers ----------

async def transfer_atomic(from_id: int, to_id: int, amount: int, fee: int) -> bool:
    """Перевод выполняется одним атомарным UPDATE, без промежуточного отката."""
    if amount <= 0 or from_id <= 0 or to_id <= 0 or to_id == from_id:
        return False
    if fee < 0 or fee >= amount:
        return False
    net = amount - fee
    rs = await _exec(
        """UPDATE users
           SET balance = CASE
               WHEN user_id = ? THEN COALESCE(balance, 10) - ?
               WHEN user_id = ? THEN COALESCE(balance, 10) + ?
               ELSE balance
           END
           WHERE user_id IN (?, ?)
             AND (SELECT COUNT(*) FROM users WHERE user_id IN (?, ?)) = 2
             AND EXISTS (
                 SELECT 1 FROM users
                 WHERE user_id = ? AND COALESCE(balance, 10) >= ?
             )""",
        [from_id, amount, to_id, net, from_id, to_id,
         from_id, to_id, from_id, amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) == 2


# ---------- pending_salary ----------

async def add_pending_salary(chat_id: int, user_id: int, day: str,
                             frac_delta: int) -> bool:
    if frac_delta <= 0:
        return True
    rs = await _exec(
        """INSERT INTO pending_salary (chat_id, user_id, day, frac_delta)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             frac_delta = pending_salary.frac_delta + excluded.frac_delta,
             updated_at = datetime('now')""",
        [chat_id, user_id, day, frac_delta],
    )
    return not getattr(rs, "error", False)


async def get_pending_salary(chat_id: int, user_id: int, day: str) -> int:
    rs = await _exec(
        """SELECT frac_delta FROM pending_salary
           WHERE chat_id = ? AND user_id = ? AND day = ?""",
        [chat_id, user_id, day],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def take_pending_salary(chat_id: int, user_id: int, day: str,
                              max_frac: Optional[int] = None) -> int:
    rs = await _exec(
        """SELECT frac_delta FROM pending_salary
           WHERE chat_id = ? AND user_id = ? AND day = ?""",
        [chat_id, user_id, day],
    )
    if not rs.rows:
        return 0
    cur = rs.rows[0][0] or 0
    if cur <= 0:
        return 0
    take = cur if max_frac is None else min(cur, max_frac)
    if take <= 0:
        return 0
    rs2 = await _exec(
        """UPDATE pending_salary
           SET frac_delta = frac_delta - ?,
               updated_at = datetime('now')
           WHERE chat_id = ? AND user_id = ? AND day = ? AND frac_delta >= ?""",
        [take, chat_id, user_id, day, take],
    )
    if getattr(rs2, "error", False):
        return 0
    if (getattr(rs2, "rows_affected", 0) or 0) == 0:
        return 0
    return take


async def return_pending_salary(chat_id: int, user_id: int, day: str,
                                frac: int) -> bool:
    if frac <= 0:
        return True
    return await add_pending_salary(chat_id, user_id, day, frac)


async def cleanup_pending_salary(keep_days: int = 2) -> int:
    rs = await _exec(
        "DELETE FROM pending_salary WHERE day < date('now', ?)",
        [f"-{int(keep_days)} days"],
    )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- salary ----------

async def add_salary_atomic(user_id: int, day: str, delta: int) -> bool:
    # salary_today учитывает начисленную зарплату, а не стартовые 10 монет.
    if delta <= 0:
        return True
    rs = await _exec(
        """INSERT INTO users (user_id, balance, salary_today, salary_day)
           VALUES (?, 10 + ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             balance = COALESCE(users.balance, 10) + ?,
             salary_today = CASE
                 WHEN users.salary_day = excluded.salary_day
                   THEN COALESCE(users.salary_today, 0) + ?
                 ELSE ?
             END,
             salary_day = excluded.salary_day""",
        [user_id, delta, delta, day, delta, delta, delta],
    )
    return not getattr(rs, "error", False)


async def add_salary_frac_atomic(
    user_id: int, day: str, frac_delta: int,
) -> Optional[int]:
    if frac_delta <= 0:
        return 0

    # Начисление и вычисление возвращаемого количества монет выполняются
    # одним UPSERT. RETURNING видит результат именно этого атомарного
    # изменения, поэтому параллельные начисления не искажают ответ.
    initial_coins = frac_delta // 100
    initial_frac = frac_delta % 100
    carry_expr = "((CASE WHEN users.salary_day = excluded.salary_day THEN COALESCE(users.salary_frac, 0) ELSE 0 END + ?) / 100)"
    frac_expr = "((CASE WHEN users.salary_day = excluded.salary_day THEN COALESCE(users.salary_frac, 0) ELSE 0 END + ?) % 100)"
    rs2 = await _exec(
        f"""INSERT INTO users
                 (user_id, balance, salary_frac, salary_day, salary_today)
               VALUES (?, 10 + ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 balance = COALESCE(users.balance, 10) + {carry_expr},
                 salary_frac = {frac_expr},
                 salary_day = excluded.salary_day,
                 salary_today = CASE
                   WHEN users.salary_day = excluded.salary_day
                     THEN COALESCE(users.salary_today, 0) + {carry_expr}
                   ELSE {carry_expr}
                 END
               RETURNING (
                 (? / 100) + (
                   (? % 100 + (((COALESCE(salary_frac, 0) - (? % 100)) % 100 + 100) % 100)) / 100
                 ) AS credited_coins""",
        [user_id, initial_coins, initial_frac, day, initial_coins,
         frac_delta, frac_delta, frac_delta, frac_delta,
         frac_delta, frac_delta, frac_delta],
    )
    if getattr(rs2, "error", False):
        return None
    if not getattr(rs2, "rows", None):
        logger.error("add_salary_frac_atomic: UPSERT не вернул credited_coins для user_id=%s", user_id)
        return None
    return int(rs2.rows[0][0] or 0)


async def pay_salary_from_treasury(
    chat_id: int, user_id: int, day: str, frac_delta: int,
) -> Optional[int]:
    """при сбое UPDATE users возвращаем и coins в казну,
    и new_frac в pending_salary, чтобы не терять дробную часть."""
    logger.info("salary: chat=%s user=%s frac_in=%s", chat_id, user_id, frac_delta)
    if frac_delta <= 0:
        return 0

    rs = await _exec(
        "SELECT salary_frac, salary_day FROM users WHERE user_id = ?",
        [user_id],
    )
    if getattr(rs, "error", False):
        return None
    if rs.rows:
        cur_frac, cur_day = rs.rows[0]
        if cur_day != day:
            cur_frac = 0
    else:
        cur_frac = 0

    total_frac = (cur_frac or 0) + frac_delta
    coins = total_frac // 100
    new_frac = total_frac % 100

    if coins > 0:
        cur_treasury = await treasury_get(chat_id)
        logger.info("salary: treasury_before=%s coins=%s", cur_treasury, coins)
        if cur_treasury <= 0:
            logger.info("salary: казна пуста, зарплата не платится")
            return None
        take = min(coins, cur_treasury)
        ok = await treasury_withdraw(chat_id, take)
        logger.info("salary: withdraw ok=%s take=%s", ok, take)
        if not ok:
            return None
        coins = take
        if coins < total_frac // 100:
            unpaid_coins = (total_frac // 100) - coins
            logger.warning(
                "salary: казна кончилась, выдано %s из %s; отложено %s",
                coins, total_frac // 100, unpaid_coins,
            )
            # salary_frac хранит только остаток 0..99; целые невыплаченные
            # монеты возвращаем в pending_salary в тех же сотых единицах.
            if unpaid_coins > 0:
                pending_ok = await return_pending_salary(
                    chat_id, user_id, day, unpaid_coins * 100,
                )
                if not pending_ok:
                    await treasury_add(chat_id, coins)
                    return None
            new_frac = total_frac % 100
        else:
            new_frac = total_frac % 100
    else:
        new_frac = total_frac % 100

    rs2 = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) + ?,
               salary_frac = ?,
               salary_day = ?,
               salary_today = CASE
                   WHEN salary_day = ? THEN COALESCE(salary_today, 0) + ?
                   ELSE ?
               END
           WHERE user_id = ?""",
        [coins, new_frac, day, day, coins, coins, user_id],
    )
    if getattr(rs2, "error", False):
        # компенсация: вернуть деньги в казну и дробную часть в pending
        if coins > 0:
            await treasury_add(chat_id, coins)
        if new_frac > 0:
            await return_pending_salary(chat_id, user_id, day, new_frac)
        return None
    return coins


async def get_salary_today(user_id: int, day: str) -> int:
    rs = await _exec(
        "SELECT salary_today, salary_day FROM users WHERE user_id = ?",
        [user_id],
    )
    if not rs.rows:
        return 0
    today_val, today_day = rs.rows[0]
    if today_day != day:
        return 0
    return today_val or 0


async def get_salary_frac(user_id: int, day: str) -> int:
    rs = await _exec(
        "SELECT salary_frac, salary_day FROM users WHERE user_id = ?",
        [user_id],
    )
    if not rs.rows:
        return 0
    frac, d = rs.rows[0]
    if d != day:
        return 0
    return frac or 0


# ---------- credit from treasury ----------

async def take_credit_from_treasury(
    chat_id: int, user_id: int, amount: int, today: str,
) -> bool:
    logger.info("credit_from_treasury: chat=%s user=%s amount=%s",
                chat_id, user_id, amount)
    if amount <= 0:
        return False

    cur = await treasury_get(chat_id)
    logger.info("credit_from_treasury: treasury_before=%s", cur)
    if cur < amount:
        logger.info("credit_from_treasury: казна меньше нужного")
        return False

    rs = await _exec(
        """UPDATE users
           SET credit  = COALESCE(credit, 0) + ?,
               balance = COALESCE(balance, 10) + ?,
               credit_taken_day = ?
           WHERE user_id = ?
             AND COALESCE(credit, 0) = 0
             AND (credit_taken_day IS NULL OR credit_taken_day != ?)""",
        [amount, amount, today, user_id, today],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        logger.warning(
            "credit_from_treasury: UPDATE users failed (условия не выполнены или ошибка)",
        )
        return False

    ok = await treasury_withdraw(chat_id, amount)
    logger.info("credit_from_treasury: withdraw ok=%s", ok)
    if not ok:
        await _exec(
            """UPDATE users
               SET credit  = COALESCE(credit, 0) - ?,
                   balance = COALESCE(balance, 10) - ?,
                   credit_taken_day = NULL
               WHERE user_id = ?
                 AND COALESCE(credit, 0) >= ?""",
            [amount, amount, user_id, amount],
        )
        return False

    logger.info("credit_from_treasury: успешно, казна=%s",
                await treasury_get(chat_id))
    return True


async def repay_credit_to_treasury(
    chat_id: int, user_id: int, amount: int,
) -> bool:
    if amount <= 0:
        return False
    rs = await _exec(
        """UPDATE users
           SET credit  = COALESCE(credit, 0) - ?,
               balance = COALESCE(balance, 10) - ?
           WHERE user_id = ?
             AND COALESCE(balance, 0) >= ?
             AND COALESCE(credit, 0) >= ?""",
        [amount, amount, user_id, amount, amount],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        return False
    ok = await treasury_add(chat_id, amount)
    if not ok:
        # вернуть пользователю, если казна не приняла
        await _exec(
            """UPDATE users
               SET credit  = COALESCE(credit, 0) + ?,
                   balance = COALESCE(balance, 10) + ?
               WHERE user_id = ?""",
            [amount, amount, user_id],
        )
        return False
    return True


async def apply_credit_interest_to_treasury(chat_id: int, percent: float) -> int:
    rs = await _exec(
        """SELECT COALESCE(SUM(CAST(credit * ? / 100 AS INTEGER)), 0)
           FROM users WHERE credit > 0""",
        [percent],
    )
    total = (rs.rows[0][0] if rs.rows else 0) or 0
    if total <= 0:
        return 0
    rs2 = await _exec(
        "UPDATE users SET credit = credit + CAST(credit * ? / 100 AS INTEGER) "
        "WHERE credit > 0",
        [percent],
    )
    affected = getattr(rs2, "rows_affected", 0) or 0
    if affected > 0:
        ok = await treasury_add(chat_id, total)
        if not ok:
            logger.error(
                "apply_credit_interest_to_treasury: проценты начислены, "
                "но казна не пополнена (chat=%s total=%s)", chat_id, total,
            )
    return affected


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


async def inc_message_by(chat_id: int, user_id: int, delta: int) -> None:
    if delta <= 0:
        return
    await _exec(
        """INSERT INTO messages (chat_id, user_id, count)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             count = count + excluded.count""",
        [chat_id, user_id, delta],
    )


async def inc_message_day_by(chat_id: int, user_id: int, day: str, delta: int) -> None:
    if delta <= 0:
        return
    await _exec(
        """INSERT INTO messages_day (chat_id, user_id, day, count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             count = count + excluded.count""",
        [chat_id, user_id, day, delta],
    )


async def inc_message_batch_idempotent(
    chat_id: int, user_id: int, day: str, delta: int, key: str,
) -> bool:
    """при сбое messages_day откатываем messages и ключ идемпотентности."""
    if delta <= 0:
        return True

    rs = await _exec(
        "INSERT OR IGNORE INTO messages_idempotency (key) VALUES (?)",
        [key],
    )
    if getattr(rs, "error", False):
        return False

    inserted_now = (getattr(rs, "rows_affected", 0) or 0) > 0
    if not inserted_now:
        return True

    rs1 = await _exec(
        """INSERT INTO messages (chat_id, user_id, count)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             count = count + excluded.count""",
        [chat_id, user_id, delta],
    )
    if getattr(rs1, "error", False):
        await _exec("DELETE FROM messages_idempotency WHERE key = ?", [key])
        return False

    rs2 = await _exec(
        """INSERT INTO messages_day (chat_id, user_id, day, count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             count = count + excluded.count""",
        [chat_id, user_id, day, delta],
    )
    if getattr(rs2, "error", False):
        # откат messages и ключа, чтобы ретрай не потерял сообщение
        await _exec(
            """UPDATE messages SET count = count - ?
               WHERE chat_id = ? AND user_id = ? AND count >= ?""",
            [delta, chat_id, user_id, delta],
        )
        await _exec("DELETE FROM messages_idempotency WHERE key = ?", [key])
        logger.warning(
            "inc_message_batch_idempotent: messages_day failed, откат (key=%s)", key,
        )
        return False

    return True


async def inc_message_and_day(chat_id: int, user_id: int, day: str) -> bool:
    rs1 = await _exec(
        """INSERT INTO messages (chat_id, user_id, count)
           VALUES (?, ?, 1)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             count = count + 1""",
        [chat_id, user_id],
    )
    if getattr(rs1, "error", False):
        return False
    rs2 = await _exec(
        """INSERT INTO messages_day (chat_id, user_id, day, count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             count = count + 1""",
        [chat_id, user_id, day],
    )
    if getattr(rs2, "error", False):
        await _exec(
            """UPDATE messages SET count = count - 1
               WHERE chat_id = ? AND user_id = ? AND count >= 1""",
            [chat_id, user_id],
        )
        return False
    return True


async def top_messages(chat_id: int, limit: int = 50) -> List[tuple]:
    rs = await _exec(
        """SELECT m.user_id, m.count
           FROM messages m
           WHERE m.chat_id = ?
           ORDER BY m.count DESC LIMIT ?""",
        [chat_id, limit],
    )
    return [(r[0], r[1]) for r in rs.rows]


async def top_balances(limit: int = 20) -> List[tuple]:
    rs = await _exec(
        "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?",
        [limit],
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


async def cleanup_old_days(keep_days: int = 1) -> int:
    if keep_days <= 0:
        rs = await _exec("DELETE FROM messages_day WHERE day < date('now')")
    else:
        rs = await _exec(
            "DELETE FROM messages_day WHERE day < date('now', ?)",
            [f"-{int(keep_days)} days"],
        )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- marriages ----------

def _new_client_token() -> str:
    return uuid.uuid4().hex


async def create_proposal(chat_id: int, user1_id: int, user2_id: int,
                          forced: bool = False) -> int:
    token = _new_client_token()
    rs = await _exec(
        """INSERT INTO marriages
             (chat_id, user1_id, user2_id, active, forced, client_token)
           SELECT ?, ?, ?, 0, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM marriages m2
               WHERE m2.chat_id = ?
                 AND m2.active = 1
                 AND (
                     m2.user1_id IN (?, ?)
                     OR m2.user2_id IN (?, ?)
                 )
           )""",
        [chat_id, user1_id, user2_id, 1 if forced else 0, token,
         chat_id, user1_id, user2_id, user1_id, user2_id],
    )
    if getattr(rs, "error", False):
        return 0
    rs2 = await _exec(
        "SELECT id FROM marriages WHERE client_token = ? LIMIT 1", [token],
    )
    if getattr(rs2, "error", False) or not rs2.rows:
        await _exec("DELETE FROM marriages WHERE client_token = ?", [token])
        return 0
    return rs2.rows[0][0]


async def accept_marriage(marriage_id: int) -> int:
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
    if getattr(rs, "error", False):
        return 0
    return getattr(rs, "rows_affected", 0) or 0


async def delete_marriage(marriage_id: int) -> int:
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
           ORDER BY id DESC LIMIT 1""",
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
                             event: str, by_user_id: Optional[int] = None) -> bool:
    rs = await _exec(
        """INSERT INTO marriage_history (chat_id, user1_id, user2_id, event, by_user_id)
           VALUES (?, ?, ?, ?, ?)""",
        [chat_id, user1_id, user2_id, event, by_user_id],
    )
    return not getattr(rs, "error", False)


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


# ---------- marriage xp / top ----------

async def marriage_add_xp(chat_id: int, user_id: int, xp: int) -> bool:
    """берём только последний активный брак юзера (LIMIT 1 по id)."""
    if xp <= 0:
        return True
    rs = await _exec(
        """UPDATE marriages
           SET xp = COALESCE(xp, 0) + ?
           WHERE id = (
               SELECT id FROM marriages
               WHERE chat_id = ? AND active = 1
                 AND (user1_id = ? OR user2_id = ?)
               ORDER BY id DESC LIMIT 1
           )""",
        [xp, chat_id, user_id, user_id],
    )
    return not getattr(rs, "error", False)


async def marriage_get_xp(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        """SELECT COALESCE(xp, 0) FROM marriages
           WHERE chat_id = ? AND active = 1
             AND (user1_id = ? OR user2_id = ?)
           ORDER BY id DESC LIMIT 1""",
        [chat_id, user_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def marriage_top(chat_id: int, limit: int = 20) -> List[dict]:
    rs = await _exec(
        f"""SELECT id, user1_id, user2_id, married_at, created_at,
                   COALESCE(xp, 0), forced
            FROM marriages
            WHERE chat_id = ? AND active = 1
            ORDER BY
              (
                julianday('now')
                - julianday(COALESCE(married_at, created_at))
                + COALESCE(xp, 0) / {XP_PER_LEVEL}.0
              ) DESC,
              COALESCE(married_at, created_at) ASC
            LIMIT ?""",
        [chat_id, limit],
    )
    return [
        {
            "id": r[0], "user1_id": r[1], "user2_id": r[2],
            "married_at": r[3], "created_at": r[4],
            "xp": r[5] or 0, "forced": bool(r[6]),
        }
        for r in rs.rows
    ]


async def marriage_rank(chat_id: int, user_id: int) -> Optional[tuple]:
    rs = await _exec(
        f"""SELECT
                julianday('now')
                - julianday(COALESCE(married_at, created_at))
                + COALESCE(xp, 0) / {XP_PER_LEVEL}.0
            FROM marriages
            WHERE chat_id = ? AND active = 1
              AND (user1_id = ? OR user2_id = ?)
            ORDER BY id DESC LIMIT 1""",
        [chat_id, user_id, user_id],
    )
    if not rs.rows:
        return None
    my_score = rs.rows[0][0] or 0.0

    rs2 = await _exec(
        f"""SELECT COUNT(*) FROM marriages
            WHERE chat_id = ? AND active = 1
              AND (
                julianday('now')
                - julianday(COALESCE(married_at, created_at))
                + COALESCE(xp, 0) / {XP_PER_LEVEL}.0
              ) > ?""",
        [chat_id, my_score],
    )
    rank = ((rs2.rows[0][0] if rs2.rows else 0) or 0) + 1

    rs3 = await _exec(
        "SELECT COUNT(*) FROM marriages WHERE chat_id = ? AND active = 1",
        [chat_id],
    )
    total = (rs3.rows[0][0] if rs3.rows else 0) or 0
    return rank, total


# ---------- warnings ----------

async def add_warning(chat_id: int, user_id: int, issued_by: int,
                      reason: Optional[str], expires_at: str) -> int:
    rs = await _exec(
        """INSERT INTO warnings (chat_id, user_id, issued_by, reason, expires_at)
           VALUES (?, ?, ?, ?, ?) RETURNING id""",
        [chat_id, user_id, issued_by, reason, expires_at],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return 0
    return rs.rows[0][0]


async def count_active_warnings(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        """SELECT COUNT(*) FROM warnings
           WHERE chat_id = ? AND user_id = ? AND active = 1
             AND expires_at > datetime('now')""",
        [chat_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def count_warnings_total(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        "SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ?",
        [chat_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def remove_last_active_warning(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        """UPDATE warnings SET active = 0
           WHERE id = (
               SELECT id FROM warnings
               WHERE chat_id = ? AND user_id = ? AND active = 1
                 AND expires_at > datetime('now')
               ORDER BY id DESC LIMIT 1
           )""",
        [chat_id, user_id],
    )
    if getattr(rs, "error", False):
        return 0
    return getattr(rs, "rows_affected", 0) or 0


async def deactivate_warnings(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        """UPDATE warnings SET active = 0
           WHERE chat_id = ? AND user_id = ? AND active = 1""",
        [chat_id, user_id],
    )
    return getattr(rs, "rows_affected", 0) or 0


# ---------- complaints ----------

async def add_complaint(chat_id: int, target_id: int, reporter_id: int,
                        message_id: Optional[int]) -> int:
    rs = await _exec(
        """INSERT INTO complaints (chat_id, target_id, reporter_id, message_id)
           VALUES (?, ?, ?, ?) RETURNING id""",
        [chat_id, target_id, reporter_id, message_id],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return 0
    return rs.rows[0][0]


async def get_complaint(complaint_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, chat_id, target_id, reporter_id, message_id,
                  created_at, status
           FROM complaints WHERE id = ?""",
        [complaint_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "chat_id": r[1], "target_id": r[2],
        "reporter_id": r[3], "message_id": r[4],
        "created_at": r[5], "status": r[6],
    }


async def resolve_complaint(complaint_id: int, status: str,
                            resolved_by: int) -> int:
    """валидация status, чтобы в БД не попадала произвольная строка."""
    if status not in _ALLOWED_COMPLAINT_STATUSES:
        logger.warning("resolve_complaint: недопустимый status=%r", status)
        return 0
    rs = await _exec(
        """UPDATE complaints
           SET status = ?, resolved_by = ?, resolved_at = datetime('now')
           WHERE id = ? AND status = 'pending'""",
        [status, resolved_by, complaint_id],
    )
    return getattr(rs, "rows_affected", 0) or 0


async def count_complaints_by(chat_id: int, reporter_id: int) -> int:
    rs = await _exec(
        "SELECT COUNT(*) FROM complaints WHERE chat_id = ? AND reporter_id = ?",
        [chat_id, reporter_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def count_complaints_against(chat_id: int, target_id: int) -> int:
    rs = await _exec(
        "SELECT COUNT(*) FROM complaints WHERE chat_id = ? AND target_id = ?",
        [chat_id, target_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


# ---------- aura ----------

async def aura_droch(chat_id: int, user_id: int) -> bool:
    """один атомарный INSERT ... ON CONFLICT DO UPDATE."""
    rs = await _exec(
        """INSERT INTO aura (chat_id, user_id, value, last_droch_at)
           VALUES (?, ?, 1, datetime('now'))
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             value = aura.value + 1,
             last_droch_at = datetime('now')
           WHERE aura.last_droch_at IS NULL
              OR aura.last_droch_at <= datetime('now', '-1 hour')""",
        [chat_id, user_id],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def aura_get(chat_id: int, user_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT value, last_droch_at FROM aura
           WHERE chat_id = ? AND user_id = ?""",
        [chat_id, user_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {"value": r[0] or 0, "last_droch_at": r[1]}


async def aura_top(chat_id: int, limit: int = 10) -> List[tuple]:
    rs = await _exec(
        """SELECT user_id, value FROM aura
           WHERE chat_id = ? AND value > 0
           ORDER BY value DESC, user_id ASC
           LIMIT ?""",
        [chat_id, limit],
    )
    return [(r[0], r[1]) for r in rs.rows]


async def aura_rank(chat_id: int, user_id: int) -> Optional[tuple]:
    rs = await _exec(
        "SELECT value FROM aura WHERE chat_id = ? AND user_id = ?",
        [chat_id, user_id],
    )
    if not rs.rows:
        return None
    value = rs.rows[0][0] or 0
    if value <= 0:
        return None

    rs2 = await _exec(
        """SELECT COUNT(*) FROM aura WHERE chat_id = ? AND value > ?""",
        [chat_id, value],
    )
    rank = ((rs2.rows[0][0] if rs2.rows else 0) or 0) + 1

    rs3 = await _exec(
        "SELECT COUNT(*) FROM aura WHERE chat_id = ? AND value > 0", [chat_id],
    )
    total = (rs3.rows[0][0] if rs3.rows else 0) or 0
    return value, rank, total


# ---------- awards ----------

async def add_award(chat_id: int, user_id: int, title: str, stars: int,
                    issued_by: int) -> int:
    rs = await _exec(
        """INSERT INTO awards (chat_id, user_id, title, stars, issued_by)
           VALUES (?, ?, ?, ?, ?) RETURNING id""",
        [chat_id, user_id, title, stars, issued_by],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return 0
    return rs.rows[0][0]


async def remove_award_by_title(chat_id: int, user_id: int, title: str) -> int:
    rs = await _exec(
        """DELETE FROM awards
           WHERE id = (
               SELECT id FROM awards
               WHERE chat_id = ? AND user_id = ? AND title = ?
               ORDER BY id DESC LIMIT 1
           )""",
        [chat_id, user_id, title],
    )
    if getattr(rs, "error", False):
        return 0
    return getattr(rs, "rows_affected", 0) or 0


async def list_awards(chat_id: int, user_id: int) -> List[dict]:
    rs = await _exec(
        """SELECT id, title, stars, issued_by, created_at
           FROM awards
           WHERE chat_id = ? AND user_id = ?
           ORDER BY id DESC""",
        [chat_id, user_id],
    )
    return [
        {"id": r[0], "title": r[1], "stars": r[2],
         "issued_by": r[3], "created_at": r[4]}
        for r in rs.rows
    ]


async def award_total_stars(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        "SELECT COALESCE(SUM(stars), 0) FROM awards "
        "WHERE chat_id = ? AND user_id = ?",
        [chat_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def count_awards(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        "SELECT COUNT(*) FROM awards WHERE chat_id = ? AND user_id = ?",
        [chat_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


# ---------- avatar ----------

async def set_avatar(user_id: int, file_id: Optional[str]) -> bool:
    rs = await _exec(
        "UPDATE users SET avatar_file_id = ? WHERE user_id = ?",
        [file_id, user_id],
    )
    return not getattr(rs, "error", False)


async def get_avatar(user_id: int) -> Optional[str]:
    rs = await _exec(
        "SELECT avatar_file_id FROM users WHERE user_id = ?", [user_id],
    )
    if not rs.rows:
        return None
    return rs.rows[0][0]


# ---------- action_stats ----------

async def inc_action_stat(chat_id: int, user_id: int, action: str,
                          delta: int = 1) -> bool:
    if delta <= 0:
        return True
    rs = await _exec(
        """INSERT INTO action_stats (chat_id, user_id, action, count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id, action) DO UPDATE SET
             count = action_stats.count + excluded.count,
             updated_at = datetime('now')""",
        [chat_id, user_id, action, delta],
    )
    return not getattr(rs, "error", False)


async def get_action_stat(chat_id: int, user_id: int, action: str) -> int:
    rs = await _exec(
        """SELECT count FROM action_stats
           WHERE chat_id = ? AND user_id = ? AND action = ?""",
        [chat_id, user_id, action],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


async def top_action_stats(chat_id: int, action: str,
                           limit: int = 10) -> List[tuple]:
    rs = await _exec(
        """SELECT user_id, count FROM action_stats
           WHERE chat_id = ? AND action = ?
           ORDER BY count DESC, user_id ASC LIMIT ?""",
        [chat_id, action, limit],
    )
    return [(r[0], r[1]) for r in rs.rows]


# ---------- user_cosmetics ----------

async def cosmetics_get(user_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT title, badge, frame, color, action_prefix, custom_trigger
           FROM user_cosmetics WHERE user_id = ?""",
        [user_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "title": r[0], "badge": r[1], "frame": r[2],
        "color": r[3], "action_prefix": r[4], "custom_trigger": r[5],
    }


async def cosmetics_set(user_id: int, field: str, value: Optional[str]) -> bool:
    allowed = {"title", "badge", "frame", "color", "action_prefix", "custom_trigger"}
    if field not in allowed:
        return False
    rs = await _exec(
        f"""INSERT INTO user_cosmetics (user_id, {field})
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET {field}=excluded.{field}""",
        [user_id, value],
    )
    return not getattr(rs, "error", False)


# ---------- coins ----------

_COIN_KEYS = [
    "id", "chat_id", "creator_id", "name", "description",
    "start_price", "total_supply", "sold", "yield_per_day",
    "photo_file_id", "photo_hash", "created_at", "active",
    "reserve", "circulating", "dividend_pool",
]


def _coin_row(r) -> dict:
    """dict(zip(...)), чтобы не ломалось при изменении _COIN_SELECT."""
    d = dict(zip(_COIN_KEYS, r))
    d["active"] = bool(d.get("active"))
    d["reserve"] = d.get("reserve") or 0
    d["circulating"] = d.get("circulating") or 0
    d["dividend_pool"] = d.get("dividend_pool") or 0
    return d


_COIN_SELECT = """SELECT id, chat_id, creator_id, name, description,
                         start_price, total_supply, sold, yield_per_day,
                         photo_file_id, photo_hash, created_at, active,
                         reserve, circulating, dividend_pool
                  FROM coins"""


async def coin_create(chat_id: int, creator_id: int, name: str,
                      description: str, start_price: int, total_supply: int,
                      yield_per_day: int, photo_file_id: Optional[str],
                      photo_hash: Optional[str]) -> int:
    rs = await _exec(
        """INSERT INTO coins (chat_id, creator_id, name, description,
             start_price, total_supply, yield_per_day, photo_file_id, photo_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        [chat_id, creator_id, name, description, start_price,
         total_supply, yield_per_day, photo_file_id, photo_hash],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return 0
    return rs.rows[0][0]


async def coin_get(coin_id: int) -> Optional[dict]:
    rs = await _exec(f"{_COIN_SELECT} WHERE id=?", [coin_id])
    if not rs.rows:
        return None
    return _coin_row(rs.rows[0])


async def coin_find_by_name(chat_id: int, name: str) -> Optional[int]:
    rs = await _exec(
        "SELECT id FROM coins WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1",
        [chat_id, name],
    )
    return rs.rows[0][0] if rs.rows else None


async def coin_find_by_photo_hash(chat_id: int, h: str) -> Optional[int]:
    rs = await _exec(
        "SELECT id FROM coins WHERE chat_id=? AND photo_hash=? LIMIT 1",
        [chat_id, h],
    )
    return rs.rows[0][0] if rs.rows else None


async def coins_list_by_chat(chat_id: int) -> List[dict]:
    rs = await _exec(
        f"{_COIN_SELECT} WHERE chat_id=? AND active=1 ORDER BY id ASC",
        [chat_id],
    )
    return [_coin_row(r) for r in rs.rows]


async def coins_list_active_all() -> List[dict]:
    rs = await _exec(f"{_COIN_SELECT} WHERE active=1")
    return [_coin_row(r) for r in rs.rows]


async def coins_by_creator(chat_id: int, creator_id: int) -> List[dict]:
    rs = await _exec(
        f"{_COIN_SELECT} WHERE chat_id=? AND creator_id=? AND active=1 "
        f"ORDER BY id ASC",
        [chat_id, creator_id],
    )
    return [_coin_row(r) for r in rs.rows]


# ── эмитенты

async def emitter_is_allowed(chat_id: int, user_id: int) -> bool:
    rs = await _exec(
        "SELECT 1 FROM coin_emitters WHERE chat_id=? AND user_id=? AND active=1 LIMIT 1",
        [chat_id, user_id],
    )
    return bool(rs.rows)


async def emitter_add(chat_id: int, user_id: int, granted_by: int) -> bool:
    rs = await _exec(
        """INSERT INTO coin_emitters (chat_id, user_id, granted_by, active)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             granted_by = excluded.granted_by,
             granted_at = datetime('now'),
             active = 1""",
        [chat_id, user_id, granted_by],
    )
    return not getattr(rs, "error", False)


async def emitter_remove(chat_id: int, user_id: int) -> bool:
    rs = await _exec(
        "UPDATE coin_emitters SET active=0 WHERE chat_id=? AND user_id=?",
        [chat_id, user_id],
    )
    return not getattr(rs, "error", False)


async def emitter_list(chat_id: int) -> List[int]:
    rs = await _exec(
        "SELECT user_id FROM coin_emitters WHERE chat_id=? AND active=1 "
        "ORDER BY granted_at",
        [chat_id],
    )
    return [r[0] for r in rs.rows]


async def user_active_coin_count(chat_id: int, user_id: int) -> int:
    rs = await _exec(
        "SELECT COUNT(*) FROM coins WHERE chat_id=? AND creator_id=? AND active=1",
        [chat_id, user_id],
    )
    if not rs.rows:
        return 0
    return rs.rows[0][0] or 0


# ── резерв / оборот / дивиденды

async def coin_reserve_add(coin_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    rs = await _exec(
        """UPDATE coins
           SET reserve = COALESCE(reserve, 0) + ?
           WHERE id = ? AND COALESCE(reserve, 0) <= ?""",
        [amount, coin_id, _MAX_SQLITE_INT - amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def coin_reserve_take(coin_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    rs = await _exec(
        """UPDATE coins SET reserve = reserve - ?
           WHERE id = ? AND reserve >= ?""",
        [amount, coin_id, amount],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def coin_circulating_change(coin_id: int, delta: int) -> bool:
    rs = await _exec(
        """UPDATE coins SET circulating = COALESCE(circulating, 0) + ?
           WHERE id = ? AND COALESCE(circulating, 0) + ? >= 0""",
        [delta, coin_id, delta],
    )
    if getattr(rs, "error", False):
        return False
    return (getattr(rs, "rows_affected", 0) or 0) > 0


async def coin_dividend_add(coin_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    rs = await _exec(
        "UPDATE coins SET dividend_pool = COALESCE(dividend_pool, 0) + ? "
        "WHERE id = ?",
        [amount, coin_id],
    )
    return not getattr(rs, "error", False)


async def coin_dividend_take(coin_id: int) -> int:
    """убрана мёртвая ветка rs3. При гонке возвращаем 0."""
    rs = await _exec(
        "SELECT COALESCE(dividend_pool, 0) FROM coins WHERE id = ?", [coin_id],
    )
    if not rs.rows:
        return 0
    cur = rs.rows[0][0] or 0
    if cur <= 0:
        return 0
    rs2 = await _exec(
        "UPDATE coins SET dividend_pool = 0 WHERE id = ? AND dividend_pool = ?",
        [coin_id, cur],
    )
    if getattr(rs2, "error", False):
        return 0
    if (getattr(rs2, "rows_affected", 0) or 0) == 0:
        # пул уже забрал кто-то другой
        return 0
    return cur


# ── покупка на первичке

async def coin_buy_atomic(coin_id: int, buyer_id: int, price: int,
                          to_reserve: int) -> bool:
    """дописана. Проверки + списание баланса + компенсация.

    - sold < total_supply;
    - 0 <= to_reserve <= price;
    - у покупателя хватает balance (списываем price);
    - to_reserve идёт в reserve, price идёт в invested холдера;
    - при сбое на любом шаге — откат.
    """
    if price <= 0 or to_reserve < 0 or to_reserve > price:
        return False
    if buyer_id <= 0:
        return False

    # 1. списываем деньги у покупателя
    rs_bal = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) - ?
           WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
        [price, buyer_id, price],
    )
    if getattr(rs_bal, "error", False) or (getattr(rs_bal, "rows_affected", 0) or 0) == 0:
        return False

    # 2. резервируем монету (sold + 1, circulating + 1)
    rs = await _exec(
        """UPDATE coins
           SET sold = sold + 1,
               circulating = COALESCE(circulating, 0) + 1
           WHERE id = ? AND sold < total_supply
             AND COALESCE(circulating, 0) < ?""",
        [coin_id, _MAX_SQLITE_INT],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        # откат баланса
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
            [price, buyer_id],
        )
        return False

    # 3. пополняем reserve
    if to_reserve > 0:
        rs_res = await _exec(
            """UPDATE coins SET reserve = COALESCE(reserve, 0) + ?
               WHERE id = ? AND COALESCE(reserve, 0) <= ?""",
            [to_reserve, coin_id, _MAX_SQLITE_INT - to_reserve],
        )
        if getattr(rs_res, "error", False) or (getattr(rs_res, "rows_affected", 0) or 0) == 0:
            # откат sold/circulating и баланса
            await _exec(
                """UPDATE coins
                   SET sold = sold - 1,
                       circulating = MAX(0, COALESCE(circulating, 0) - 1)
                   WHERE id = ?""",
                [coin_id],
            )
            await _exec(
                "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
                [price, buyer_id],
            )
            return False

    # 4. холдер
    rs_h = await _exec(
        """INSERT INTO coin_holders (coin_id, user_id, quantity, invested)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(coin_id, user_id) DO UPDATE SET
             quantity = coin_holders.quantity + 1,
             invested = coin_holders.invested + excluded.invested""",
        [coin_id, buyer_id, price],
    )
    if getattr(rs_h, "error", False):
        # откат reserve, sold, circulating и баланса
        if to_reserve > 0:
            await _exec(
                "UPDATE coins SET reserve = MAX(0, reserve - ?) WHERE id = ?",
                [to_reserve, coin_id],
            )
        await _exec(
            """UPDATE coins
               SET sold = sold - 1,
                   circulating = MAX(0, COALESCE(circulating, 0) - 1)
               WHERE id = ?""",
            [coin_id],
        )
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
            [price, buyer_id],
        )
        return False

    # 5. история покупки (не критично для баланса — не откатываем)
    history = await _exec(
        """INSERT INTO coin_purchases (coin_id, buyer_id, quantity, price_paid)
           VALUES (?, ?, 1, ?)""",
        [coin_id, buyer_id, price],
    )
    if getattr(history, "error", False):
        logger.error(
            "coin_buy_atomic: покупка совершена, но история не записана "
            "(coin=%s buyer=%s)", coin_id, buyer_id,
        )
    return True


async def coin_holders_by_user(chat_id: int, user_id: int) -> List[dict]:
    rs = await _exec(
        """SELECT h.coin_id, h.quantity, h.invested
           FROM coin_holders h JOIN coins c ON c.id = h.coin_id
           WHERE c.chat_id=? AND h.user_id=? AND h.quantity > 0
           ORDER BY h.coin_id ASC""",
        [chat_id, user_id],
    )
    return [{"coin_id": r[0], "quantity": r[1], "invested": r[2]} for r in rs.rows]


async def coin_holder_get(coin_id: int, user_id: int) -> Optional[dict]:
    rs = await _exec(
        "SELECT quantity, invested FROM coin_holders WHERE coin_id=? AND user_id=?",
        [coin_id, user_id],
    )
    if not rs.rows:
        return None
    return {"quantity": rs.rows[0][0], "invested": rs.rows[0][1]}


async def coin_holders_list(coin_id: int) -> List[dict]:
    rs = await _exec(
        "SELECT user_id, quantity, invested FROM coin_holders WHERE coin_id=?",
        [coin_id],
    )
    return [{"user_id": r[0], "quantity": r[1], "invested": r[2]} for r in rs.rows]


async def coin_price_history_add(coin_id: int, price: int, sold: int) -> bool:
    rs = await _exec(
        """INSERT INTO coin_price_history (coin_id, price, sold)
           VALUES (?, ?, ?)""",
        [coin_id, price, sold],
    )
    return not getattr(rs, "error", False)


async def coin_price_history_get(coin_id: int, limit: int = 60) -> List[dict]:
    rs = await _exec(
        """SELECT ts, price, sold FROM coin_price_history
           WHERE coin_id=? ORDER BY id DESC LIMIT ?""",
        [coin_id, limit],
    )
    rows = [{"ts": r[0], "price": r[1], "sold": r[2]} for r in rs.rows]
    return rows[::-1]


async def coin_income_log_add(coin_id: int, user_id: int, day: str,
                              amount: int) -> None:
    """Legacy: оставлено для истории, в новой модели не используется."""
    await _exec(
        """INSERT INTO coin_income_log (coin_id, user_id, day, amount)
           VALUES (?, ?, ?, ?)""",
        [coin_id, user_id, day, amount],
    )


# ── продажа в резерв (buyback)

async def coin_sell_to_reserve_atomic(coin_id: int, user_id: int,
                                      quantity: int, price: int) -> bool:
    """Продажа монет обратно в резерв.

    reserve -= price, circulating -= quantity, holder.quantity -= quantity.
    price — уже итоговая сумма за всё кол-во.
    """
    if quantity <= 0 or price <= 0:
        return False

    # 1. списываем монеты у холдера
    rs = await _exec(
        """UPDATE coin_holders SET quantity = quantity - ?
           WHERE coin_id=? AND user_id=? AND quantity >= ?""",
        [quantity, coin_id, user_id, quantity],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        return False

    # 2. списываем из резерва и оборота
    rs2 = await _exec(
        """UPDATE coins SET reserve = reserve - ?,
                           circulating = COALESCE(circulating, 0) - ?
           WHERE id = ? AND reserve >= ? AND COALESCE(circulating, 0) >= ?""",
        [price, quantity, coin_id, price, quantity],
    )
    if getattr(rs2, "error", False) or (getattr(rs2, "rows_affected", 0) or 0) == 0:
        # откат холдера
        await _exec(
            """UPDATE coin_holders SET quantity = quantity + ?
               WHERE coin_id=? AND user_id=?""",
            [quantity, coin_id, user_id],
        )
        return False
    return True


# ── рынок лотов

async def coin_market_lot_create(coin_id: int, seller_id: int,
                                 quantity: int, price: int) -> bool:
    """Выставить лот: списываем монеты у продавца, создаём запись лота.
    При сбое INSERT — возвращаем монеты продавцу."""
    if quantity <= 0 or price <= 0:
        return False
    rs = await _exec(
        """UPDATE coin_holders SET quantity = quantity - ?
           WHERE coin_id=? AND user_id=? AND quantity >= ?""",
        [quantity, coin_id, seller_id, quantity],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        return False
    rs2 = await _exec(
        """INSERT INTO coin_market_lots (coin_id, seller_id, quantity, price)
           VALUES (?, ?, ?, ?)""",
        [coin_id, seller_id, quantity, price],
    )
    if getattr(rs2, "error", False):
        # вернуть монеты продавцу
        await _exec(
            """UPDATE coin_holders SET quantity = quantity + ?
               WHERE coin_id=? AND user_id=?""",
            [quantity, coin_id, seller_id],
        )
        return False
    return True


async def coin_market_lot_get(lot_id: int) -> Optional[dict]:
    rs = await _exec(
        """SELECT id, coin_id, seller_id, quantity, price, active
           FROM coin_market_lots WHERE id=?""",
        [lot_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "coin_id": r[1], "seller_id": r[2],
        "quantity": r[3], "price": r[4], "active": bool(r[5]),
    }


async def coin_lots_list(chat_id: int, limit: int = 20) -> List[dict]:
    rs = await _exec(
        """SELECT l.id, l.coin_id, l.seller_id, l.quantity, l.price, l.active
           FROM coin_market_lots l JOIN coins c ON c.id = l.coin_id
           WHERE c.chat_id=? AND l.active=1
           ORDER BY l.id DESC LIMIT ?""",
        [chat_id, limit],
    )
    return [{
        "id": r[0], "coin_id": r[1], "seller_id": r[2],
        "quantity": r[3], "price": r[4], "active": bool(r[5]),
    } for r in rs.rows]


async def coin_market_buy_atomic(lot_id: int, buyer_id: int,
                                 quantity: int, price: int) -> bool:
    """полноценная вторичка.

    - проверяем, что лот активен и quantity <= lot.quantity;
    - списываем деньги у покупателя (price);
    - закрываем лот (active=0);
    - зачисляем деньги продавцу (price);
    - начисляем монеты покупателю (invested НЕ растёт — это вторичка);
    - при сбое на любом шаге — откат.
    """
    if quantity <= 0 or price <= 0 or buyer_id <= 0:
        return False

    lot = await coin_market_lot_get(lot_id)
    if not lot or not lot["active"]:
        return False
    if quantity > lot["quantity"]:
        return False
    if buyer_id == lot["seller_id"]:
        return False

    # 1. списываем деньги у покупателя
    rs_bal = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) - ?
           WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
        [price, buyer_id, price],
    )
    if getattr(rs_bal, "error", False) or (getattr(rs_bal, "rows_affected", 0) or 0) == 0:
        return False

    # 2. Резервируем купленное количество. Остаток лота остаётся доступным.
    rs_lot = await _exec(
        """UPDATE coin_market_lots
           SET quantity = quantity - ?,
               active = CASE WHEN quantity - ? = 0 THEN 0 ELSE 1 END
           WHERE id = ? AND active = 1 AND quantity >= ?""",
        [quantity, quantity, lot_id, quantity],
    )
    if getattr(rs_lot, "error", False) or (getattr(rs_lot, "rows_affected", 0) or 0) == 0:
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
            [price, buyer_id],
        )
        return False

    # 3. зачисляем деньги продавцу
    rs_sell = await _exec(
        "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
        [price, lot["seller_id"]],
    )
    if getattr(rs_sell, "error", False) or (getattr(rs_sell, "rows_affected", 0) or 0) == 0:
        # откат: лот снова активен, деньги покупателю
        await _exec(
            """UPDATE coin_market_lots
               SET quantity = quantity + ?, active = 1 WHERE id=?""",
            [quantity, lot_id],
        )
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
            [price, buyer_id],
        )
        return False

    # 4. начисляем монеты покупателю (invested=0 — вторичка)
    rs_h = await _exec(
        """INSERT INTO coin_holders (coin_id, user_id, quantity, invested)
           VALUES (?, ?, ?, 0)
           ON CONFLICT(coin_id, user_id) DO UPDATE SET
             quantity = coin_holders.quantity + excluded.quantity""",
        [lot["coin_id"], buyer_id, quantity],
    )
    if getattr(rs_h, "error", False):
        # откат: лот активен, деньги продавцу -price, покупателю +price
        await _exec(
            """UPDATE coin_market_lots
               SET quantity = quantity + ?, active = 1 WHERE id=?""",
            [quantity, lot_id],
        )
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) - ? WHERE user_id = ?",
            [price, lot["seller_id"]],
        )
        await _exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
            [price, buyer_id],
        )
        return False

    return True


# ---------- shop ----------

async def shop_pocket_upsert(pocket_id: int, name: str, price: int) -> bool:
    rs = await _exec(
        """INSERT INTO shop_pockets (id, chat_id, name, price, active)
           VALUES (?, NULL, ?, ?, 1)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, price=excluded.price, active=1""",
        [pocket_id, name, price],
    )
    return not getattr(rs, "error", False)


async def shop_command_add(trigger: str, template: str, pocket_id,
                           free: int) -> bool:
    rs = await _exec(
        """INSERT INTO shop_commands (trigger, template, pocket_id, free, active)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(trigger) DO UPDATE SET
             template=excluded.template, pocket_id=excluded.pocket_id,
             free=excluded.free, active=1""",
        [trigger, template, pocket_id, free],
    )
    return not getattr(rs, "error", False)


async def shop_pockets_list(chat_id: int) -> list[dict]:
    rs = await _exec(
        "SELECT id, name, price, active FROM shop_pockets "
        "WHERE active=1 AND (chat_id IS NULL OR chat_id=?) ORDER BY id",
        [chat_id],
    )
    return [{"id": r[0], "name": r[1], "price": r[2], "active": bool(r[3])}
            for r in rs.rows]


async def shop_pocket_get(pocket_id: int):
    rs = await _exec(
        "SELECT id, name, price, active FROM shop_pockets WHERE id=?",
        [pocket_id],
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {"id": r[0], "name": r[1], "price": r[2], "active": bool(r[3])}


async def shop_commands_by_pocket(pocket_id: int) -> list[dict]:
    rs = await _exec(
        "SELECT trigger, template FROM shop_commands "
        "WHERE pocket_id=? AND active=1 ORDER BY id",
        [pocket_id],
    )
    return [{"trigger": r[0], "template": r[1]} for r in rs.rows]


async def shop_user_has_pocket(chat_id: int, user_id: int, pocket_id: int) -> bool:
    rs = await _exec(
        "SELECT 1 FROM shop_purchases "
        "WHERE chat_id=? AND user_id=? AND pocket_id=? LIMIT 1",
        [chat_id, user_id, pocket_id],
    )
    return bool(rs.rows)


async def shop_purchase_add(chat_id: int, user_id: int, pocket_id: int,
                            price: int) -> bool:
    """сначала списываем деньги, потом пишем покупку.
    При сбое — возвращаем деньги. UNIQUE защищает от двойной покупки."""
    if price < 0:
        return False

    # если уже куплено — не списываем
    if await shop_user_has_pocket(chat_id, user_id, pocket_id):
        return False

    # 1. списываем деньги (если цена > 0)
    if price > 0:
        rs_bal = await _exec(
            """UPDATE users
               SET balance = COALESCE(balance, 10) - ?
               WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
            [price, user_id, price],
        )
        if getattr(rs_bal, "error", False) or (getattr(rs_bal, "rows_affected", 0) or 0) == 0:
            return False

    # 2. пишем покупку (INSERT OR IGNORE — UNIQUE по (chat,user,pocket))
    rs = await _exec(
        "INSERT OR IGNORE INTO shop_purchases "
        "(chat_id, user_id, pocket_id, price_paid) VALUES (?, ?, ?, ?)",
        [chat_id, user_id, pocket_id, price],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        # откат денег (если списывали)
        if price > 0:
            await _exec(
                "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
                [price, user_id],
            )
        return False
    return True
