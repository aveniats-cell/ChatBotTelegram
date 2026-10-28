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


async def _exec(
    sql: str,
    params: Optional[list] = None,
    retries: int = 2,
) -> _EmptyResult:
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
            logger.exception("db exec error (sql: %.80s): %s", sql, e)
            return _EmptyResult(error=True)

    if last_exc is not None:
        logger.warning("db exec failed (sql: %.80s): %s", sql, last_exc)
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
            client_token TEXT
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
        ("marriages", "married_at", "TEXT"),
        ("marriages", "client_token", "TEXT"),
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
    ]
    for sql in idx_stmts:
        rs = await _exec(sql)
        if getattr(rs, "error", False):
            logger.warning("migrate: не удалось выполнить: %s", sql)


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
                  credit_taken_day
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
            "credit_taken_day"]
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
    rs = await _exec(
        "UPDATE users SET balance = COALESCE(balance, 10) + ? WHERE user_id = ?",
        [delta, user_id],
    )
    if getattr(rs, "error", False):
        return False
    if (getattr(rs, "rows_affected", 0) or 0) == 0:
        rs2 = await _exec(
            """INSERT INTO users (user_id, balance)
               VALUES (?, 10 + ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 balance = COALESCE(users.balance, 10) + excluded.balance - 10""",
            [user_id, delta],
        )
        if getattr(rs2, "error", False):
            return False
    return True


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
    if not rs.rows:
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
    if getattr(rs2, "error", False):
        return 0
    if (getattr(rs2, "rows_affected", 0) or 0) == 0:
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
    """Обнуляет глобальные балансы всех юзеров (balance > 0).

    ВНИМАНИЕ: баланс в таблице users глобальный, а не по чатам.
    Поэтому дефолт в одном чате обнулит балансы во всех чатах.
    Возвращает реально списанное.
    """
    rs = await _exec(
        "SELECT COALESCE(SUM(balance), 0) FROM users WHERE balance > 0",
    )
    total_before = (rs.rows[0][0] if rs.rows else 0) or 0
    if total_before <= 0:
        return 0

    rs2 = await _exec("UPDATE users SET balance = 0 WHERE balance > 0")
    if getattr(rs2, "error", False):
        logger.error(
            "treasury_default_collect: UPDATE failed, казна не пополнена",
        )
        return 0

    rs3 = await _exec(
        "SELECT COALESCE(SUM(balance), 0) FROM users WHERE balance > 0",
    )
    total_after = (rs3.rows[0][0] if rs3.rows else 0) or 0

    real = total_before - total_after
    if real <= 0:
        logger.error(
            "treasury_default_collect: расхождение before=%s after=%s",
            total_before, total_after,
        )
        return 0

    logger.info(
        "treasury_default_collect: обнулено %s (before=%s after=%s)",
        real, total_before, total_after,
    )
    return real


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

async def tax_active_balances(min_balance: int, pct: int, days: int = 1) -> int:
    rs = await _exec(
        f"""SELECT COALESCE(SUM(CAST(u.balance * ? / 100 AS INTEGER)), 0)
            FROM users u
            WHERE u.balance > ?
              AND EXISTS (
                  SELECT 1 FROM messages_day m
                  WHERE m.user_id = u.user_id
                    AND m.day >= date('now', '-{int(days)} days')
              )""",
        [pct, min_balance],
    )
    total = (rs.rows[0][0] if rs.rows else 0) or 0
    if total <= 0:
        return 0
    await _exec(
        f"""UPDATE users
            SET balance = balance - CAST(balance * ? / 100 AS INTEGER)
            WHERE balance > ?
              AND EXISTS (
                  SELECT 1 FROM messages_day m
                  WHERE m.user_id = users.user_id
                    AND m.day >= date('now', '-{int(days)} days')
              )""",
        [pct, min_balance],
    )
    return total


# ---------- transfers ----------

async def transfer_atomic(from_id: int, to_id: int, amount: int, fee: int) -> bool:
    if amount <= 0 or to_id <= 0 or from_id == to_id:
        return False
    if fee < 0 or fee >= amount:
        return False
    net = amount - fee
    rs1 = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) - ?
           WHERE user_id = ? AND COALESCE(balance, 10) >= ?""",
        [amount, from_id, amount],
    )
    if getattr(rs1, "error", False):
        return False
    if (getattr(rs1, "rows_affected", 0) or 0) == 0:
        return False
    rs2 = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) + ?
           WHERE user_id = ?""",
        [net, to_id],
    )
    if getattr(rs2, "error", False) or (getattr(rs2, "rows_affected", 0) or 0) == 0:
        await _exec(
            """UPDATE users SET balance = COALESCE(balance, 10) + ?
               WHERE user_id = ?""",
            [amount, from_id],
        )
        return False
    return True


# ---------- salary ----------

async def add_salary_atomic(user_id: int, day: str, delta: int) -> bool:
    if delta <= 0:
        return True
    rs = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) + ?,
               salary_today = CASE
                   WHEN salary_day = ? THEN COALESCE(salary_today, 0) + ?
                   ELSE ?
               END,
               salary_day = ?
           WHERE user_id = ?""",
        [delta, day, delta, delta, day, user_id],
    )
    if getattr(rs, "error", False):
        return False
    if (getattr(rs, "rows_affected", 0) or 0) > 0:
        return True
    rs2 = await _exec(
        """INSERT INTO users (user_id, balance, salary_today, salary_day)
           VALUES (?, 10 + ?, ?, ?)""",
        [user_id, delta, delta, day],
    )
    return not getattr(rs2, "error", False)


async def add_salary_frac_atomic(
    user_id: int, day: str, frac_delta: int,
) -> Optional[int]:
    if frac_delta <= 0:
        return 0

    rs = await _exec(
        "SELECT salary_frac, salary_day FROM users WHERE user_id = ?",
        [user_id],
    )
    if not rs.rows:
        coins = frac_delta // 100
        new_frac = frac_delta % 100
        rs2 = await _exec(
            """INSERT INTO users
                 (user_id, balance, salary_frac, salary_day, salary_today)
               VALUES (?, 10 + ?, ?, ?, ?)""",
            [user_id, coins, new_frac, day, coins],
        )
        if getattr(rs2, "error", False):
            return None
        return coins

    cur_frac, cur_day = rs.rows[0]
    if cur_day != day:
        cur_frac = 0

    total = (cur_frac or 0) + frac_delta
    coins = total // 100
    new_frac = total % 100

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
        return None
    return coins


async def pay_salary_from_treasury(
    chat_id: int, user_id: int, day: str, frac_delta: int,
) -> Optional[int]:
    if frac_delta <= 0:
        return 0
    coins = frac_delta // 100
    frac = frac_delta % 100

    if coins > 0:
        cur = await treasury_get(chat_id)
        if cur <= 0:
            return None
        take = min(coins, cur)
        ok = await treasury_withdraw(chat_id, take)
        if not ok:
            return None
        coins = take

    rs = await _exec(
        """UPDATE users
           SET balance = COALESCE(balance, 10) + ?,
               salary_frac = ?,
               salary_day = ?,
               salary_today = CASE
                   WHEN salary_day = ? THEN COALESCE(salary_today, 0) + ?
                   ELSE ?
               END
           WHERE user_id = ?""",
        [coins, frac, day, day, coins, coins, user_id],
    )
    if getattr(rs, "error", False):
        if coins > 0:
            await treasury_add(chat_id, coins)
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
    if amount <= 0:
        return False
    cur = await treasury_get(chat_id)
    if cur < amount:
        return False
    ok = await treasury_withdraw(chat_id, amount)
    if not ok:
        return False
    rs = await _exec(
        """UPDATE users
           SET credit  = COALESCE(credit, 0) + ?,
               balance = COALESCE(balance, 10) + ?,
               credit_taken_day = ?
           WHERE user_id = ?
             AND COALESCE(balance, 10) >= 0
             AND COALESCE(credit, 0) = 0
             AND (credit_taken_day IS NULL OR credit_taken_day != ?)""",
        [amount, amount, today, user_id, today],
    )
    if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) == 0:
        await treasury_add(chat_id, amount)
        return False
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
    await treasury_add(chat_id, amount)
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
        await treasury_add(chat_id, total)
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
        logger.warning(
            "inc_message_batch_idempotent: messages_day failed (key=%s)", key,
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
    if getattr(rs2, "error", False):
        await _exec("DELETE FROM marriages WHERE client_token = ?", [token])
        return 0
    if not rs2.rows:
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
    if getattr(rs, "error", False):
        return False
    return True


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


# ---------- warnings ----------

async def add_warning(chat_id: int, user_id: int, issued_by: int,
                      reason: Optional[str], expires_at: str) -> int:
    rs = await _exec(
        """INSERT INTO warnings (chat_id, user_id, issued_by, reason, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        [chat_id, user_id, issued_by, reason, expires_at],
    )
    if getattr(rs, "error", False):
        return 0
    rs2 = await _exec(
        """SELECT id FROM warnings
           WHERE chat_id = ? AND user_id = ? AND issued_by = ?
           ORDER BY id DESC LIMIT 1""",
        [chat_id, user_id, issued_by],
    )
    if not rs2.rows:
        return 0
    return rs2.rows[0][0]


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
           VALUES (?, ?, ?, ?)""",
        [chat_id, target_id, reporter_id, message_id],
    )
    if getattr(rs, "error", False):
        return 0
    rs2 = await _exec(
        """SELECT id FROM complaints
           WHERE chat_id = ? AND target_id = ? AND reporter_id = ?
           ORDER BY id DESC LIMIT 1""",
        [chat_id, target_id, reporter_id],
    )
    if not rs2.rows:
        return 0
    return rs2.rows[0][0]


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
    await _exec(
        """INSERT INTO aura (chat_id, user_id, value, last_droch_at)
           VALUES (?, ?, 0, NULL)
           ON CONFLICT(chat_id, user_id) DO NOTHING""",
        [chat_id, user_id],
    )
    rs = await _exec(
        """UPDATE aura
           SET value = value + 1,
               last_droch_at = datetime('now')
           WHERE chat_id = ? AND user_id = ?
             AND (last_droch_at IS NULL
                  OR last_droch_at <= datetime('now', '-1 hour'))""",
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
           VALUES (?, ?, ?, ?, ?)""",
        [chat_id, user_id, title, stars, issued_by],
    )
    if getattr(rs, "error", False):
        return 0
    rs2 = await _exec(
        """SELECT id FROM awards
           WHERE chat_id = ? AND user_id = ? AND title = ? AND issued_by = ?
           ORDER BY id DESC LIMIT 1""",
        [chat_id, user_id, title, issued_by],
    )
    if not rs2.rows:
        return 0
    return rs2.rows[0][0]


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
