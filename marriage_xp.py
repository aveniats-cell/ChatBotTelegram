"""XP для брака: начисляется, когда супруги взаимодействуют.

Логика:
  - action даёт XP только если цель — супруг(а) актора;
  - XP складывается в marriages.xp;
  - уровень брака = дни_с_свадьбы + 1 + xp // XP_PER_LEVEL.

Не импортирует wedding.py, чтобы не было цикла. XP_PER_LEVEL
дублируется здесь и в wedding.py — держи их одинаковыми.
"""
import logging

import db

logger = logging.getLogger(__name__)

XP_PER_LEVEL = 50

# Сколько XP даёт действие супругу (если цель — супруг(а)).
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


async def award_if_spouse(
    chat_id: int, actor_id: int, target_id: int, action: str,
) -> int:
    """Начисляет XP браку, если target_id — супруг(а) actor_id.

    Возвращает начисленное XP (0, если не супруг или ошибка).
    """
    xp = XP_BY_ACTION.get(action, 0)
    if xp <= 0:
        return 0
    if actor_id == target_id:
        return 0

    try:
        m = await db.find_active_marriage(chat_id, actor_id)
    except Exception as e:
        logger.warning("award_if_spouse find: %s", e)
        return 0
    if not m:
        return 0

    spouse_id = m["user2_id"] if m["user1_id"] == actor_id else m["user1_id"]
    if spouse_id != target_id:
        return 0

    try:
        ok = await db.marriage_add_xp(chat_id, actor_id, xp)
    except Exception as e:
        logger.warning("award_if_spouse add_xp: %s", e)
        return 0

    return xp if ok else 0
