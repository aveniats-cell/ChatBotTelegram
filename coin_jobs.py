"""Ежедневный доход коинов + часовая запись истории цен.

Доход: min(yield_per_day, казна_доступно) делится между холдерами
пропорционально их доле. Платятся только целые монеты. Источник — казна.
Если в казне меньше — платим сколько есть (или 0, если меньше 1).
"""
import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def daily_coin_income_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    coins = await db.coins_list_active_all()
    if not coins:
        return
    for coin in coins:
        try:
            await _payout_coin(coin)
        except Exception as e:
            logger.warning("coin income %s: %s", coin.get("id"), e)


async def _payout_coin(coin: dict) -> None:
    chat_id = coin["chat_id"]
    coin_id = coin["id"]
    ypd = int(coin["yield_per_day"] or 0)
    if ypd <= 0:
        return
    holders = await db.coin_holders_list(coin_id)
    total_qty = sum(int(h["quantity"]) for h in holders)
    if total_qty <= 0:
        return
    available = await db.treasury_get(chat_id)
    pool = min(ypd, max(0, available))
    if pool <= 0:
        return
    ok = await db.treasury_withdraw(chat_id, pool)
    if not ok:
        return
    day = _today()
    paid_total = 0
    for h in holders:
        qty = int(h["quantity"])
        share = pool * qty // total_qty
        if share <= 0:
            continue
        ok2 = await db.add_balance(h["user_id"], share)
        if ok2:
            paid_total += share
            await db.coin_income_log_add(coin_id, h["user_id"], day, share)
    leftover = pool - paid_total
    if leftover > 0:
        await db.treasury_add(chat_id, leftover)


async def hourly_price_history_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    import coins as coins_mod
    rows = await db.coins_list_active_all()
    for c in rows:
        try:
            price = coins_mod._current_price(c)
            await db.coin_price_history_add(c["id"], price, int(c["sold"] or 0))
        except Exception as e:
            logger.warning("price history %s: %s", c.get("id"), e)
