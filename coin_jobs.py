"""Фоновые задачи по монетам.
да
"""
import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def daily_dividend_distribution_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в сутки раздать dividend_pool каждой активной монеты."""
    coins = await db.coins_list_active_all()
    if not coins:
        return
    for coin in coins:
        try:
            await _distribute_dividends(coin)
        except Exception as e:
            logger.warning("dividend %s: %s", coin.get("id"), e)


async def _distribute_dividends(coin: dict) -> None:
    coin_id = coin["id"]
    pool = int(coin.get("dividend_pool") or 0)
    if pool <= 0:
        return

    holders = await db.coin_holders_list(coin_id)
    # исключаем создателя из получателей дивидендов
    creator_id = int(coin["creator_id"])
    holders = [
        h for h in holders
        if int(h["user_id"]) != creator_id and int(h["quantity"]) > 0
    ]
    total_qty = sum(int(h["quantity"]) for h in holders)
    if total_qty <= 0:
        # некому платить — пул остаётся
        return

    # атомарно забираем весь пул; если кто-то успел — вернёт 0
    taken = await db.coin_dividend_take(coin_id)
    if taken <= 0:
        return

    paid_total = 0
    for h in holders:
        qty = int(h["quantity"])
        share = taken * qty // total_qty
        if share <= 0:
            continue
        ok = await db.add_balance(h["user_id"], share)
        if ok:
            paid_total += share
            try:
                await db.coin_income_log_add(
                    coin_id, h["user_id"], _today(), share,
                )
            except Exception as e:
                logger.warning("dividend log %s/%s: %s",
                               coin_id, h["user_id"], e)

    leftover = taken - paid_total
    if leftover > 0:
        # возвращаем округлительный остаток в пул
        try:
            await db.coin_dividend_add(coin_id, leftover)
        except Exception as e:
            logger.warning("dividend leftover %s: %s", coin_id, e)

    logger.info(
        "dividend coin=%s pool=%s paid=%s leftover=%s holders=%s",
        coin_id, taken, paid_total, leftover, len(holders),
    )


async def hourly_price_history_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Записать NAV-цену и оборот в историю (раз в час)."""
    import coins as coins_mod
    rows = await db.coins_list_active_all()
    for c in rows:
        try:
            price = coins_mod._current_price(c)
            # в истории поле sold остаётся как есть (для графика),
            # но пишем circulating, чтобы график отражал оборот
            sold_or_circ = int(c.get("circulating") or c.get("sold") or 0)
            await db.coin_price_history_add(c["id"], price, sold_or_circ)
        except Exception as e:
            logger.warning("price history %s: %s", c.get("id"), e)
