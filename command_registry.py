"""Реестр динамических команд из commands_dop.py.

DSL:
  CREATE_POCKET("Название", price=200, id=68)
  CREATE_BUY("Триггер", "user1 сделал(а) с user2", pocket=68, marriage_xp=5)
  CREATE_FREE("Триггер", "user1 сделал(а) с user2", marriage_xp=2)

marriage_xp — необязательное количество XP брака. Оно начисляется только
если цель действия является супругом/супругой автора команды в этом чате.
Триггеры сравниваются без учёта регистра.
"""
import logging

logger = logging.getLogger(__name__)

_POCKETS: list[dict] = []
_COMMANDS: list[dict] = []


def CREATE_POCKET(name: str, price: int = 0, id: int = 0) -> None:
    _POCKETS.append({"id": int(id), "name": name, "price": int(price)})


def _make_command(trigger: str, template: str, pocket_id: int | None,
                  free: int, marriage_xp: int = 0) -> None:
    trigger = str(trigger).strip()
    template = str(template)
    xp = int(marriage_xp or 0)
    if xp < 0:
        raise ValueError("marriage_xp не может быть отрицательным")
    _COMMANDS.append({
        "trigger": trigger,
        "template": template,
        "pocket_id": pocket_id,
        "free": int(bool(free)),
        "marriage_xp": xp,
    })


def CREATE_BUY(trigger: str, template: str, pocket: int,
               marriage_xp: int = 0) -> None:
    _make_command(trigger, template, int(pocket), 0, marriage_xp)


def CREATE_FREE(trigger: str, template: str, marriage_xp: int = 0) -> None:
    _make_command(trigger, template, None, 1, marriage_xp)


def get_pockets() -> list[dict]:
    return [dict(p) for p in _POCKETS]


def get_commands() -> list[dict]:
    return [dict(c) for c in _COMMANDS]


def find_free(trigger: str) -> dict | None:
    t = trigger.strip().casefold()
    for c in _COMMANDS:
        if c["free"] and c["trigger"].strip().casefold() == t:
            return dict(c)
    return None


def find_buy(trigger: str) -> dict | None:
    t = trigger.strip().casefold()
    for c in _COMMANDS:
        if not c["free"] and c["trigger"].strip().casefold() == t:
            return dict(c)
    return None
