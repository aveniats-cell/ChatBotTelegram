"""Начисление XP браку за взаимодействия супругов.

XP добавляется только если цель действия — действующий супруг/супруга
автора в том же чате. Уровень брака по-прежнему рассчитывается в wedding.py.
"""
import logging

import db

logger = logging.getLogger(__name__)

XP_PER_LEVEL = 50

# Стандартные действия, уже поддерживавшиеся ботом.
XP_BY_ACTION = {
    "kiss": 2,
    "kiss_deep": 3,
    "hug": 1,
    "pat": 1,
    "handshake": 1,
    "dance": 2,
    "poke": 1,
    "tickle": 1,
    "slap": 1,
    "hit": 1,
    "kiss_all": 2,
    "hug_all": 1,
    "pat_all": 1,
    "slap_all": 1,
}


async def award_custom_if_spouse(
    chat_id: int, actor_id: int, target_id: int, xp: int,
) -> int:
    """Начислить произвольное число XP за кастомную команду.

    Возвращает фактически начисленное число XP или 0, если это не супруг(а),
    сумма некорректна либо произошла ошибка.
    """
    try:
        amount = int(xp)
    except (TypeError, ValueError):
        return 0
    if amount <= 0 or actor_id == target_id:
        return 0

    try:
        marriage = await db.find_active_marriage(chat_id, actor_id)
        if not marriage:
            return 0

        if int(marriage["user1_id"]) == actor_id:
            spouse_id = int(marriage["user2_id"])
        elif int(marriage["user2_id"]) == actor_id:
            spouse_id = int(marriage["user1_id"])
        else:
            return 0

        if spouse_id != target_id:
            return 0

        ok = await db.marriage_add_xp(chat_id, actor_id, amount)
        return amount if ok else 0
    except Exception as e:
        logger.warning("award_custom_if_spouse failed: %s", e)
        return 0


async def award_if_spouse(
    chat_id: int, actor_id: int, target_id: int, action: str,
) -> int:
    """Начислить стандартное XP по ключу действия, если цель — супруг(а)."""
    xp = XP_BY_ACTION.get(action, 0)
    if xp <= 0:
        return 0
    return await award_custom_if_spouse(chat_id, actor_id, target_id, xp)
