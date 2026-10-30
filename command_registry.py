"""Реестр динамических команд из commands_dop.py.

DSL:
  CREATE_POCKET("Название", price=200, id=68)
  CREATE_BUY("Триггер", "user1 сделал(а) с user2", pocket=68)
  CREATE_FREE("Триггер", "user1 сделал(а) с user2")

Триггеры сравниваются без учёта регистра.
"""
import logging

logger = logging.getLogger(__name__)

_POCKETS: list[dict] = []
_COMMANDS: list[dict] = []


def CREATE_POCKET(name: str, price: int = 0, id: int = 0) -> None:
    _POCKETS.append({"id": int(id), "name": name, "price": int(price)})


def CREATE_BUY(trigger: str, template: str, pocket: int) -> None:
    _COMMANDS.append({
        "trigger": trigger,
        "template": template,
        "pocket_id": int(pocket),
        "free": 0,
    })


def CREATE_FREE(trigger: str, template: str) -> None:
    _COMMANDS.append({
        "trigger": trigger,
        "template": template,
        "pocket_id": None,
        "free": 1,
    })


def get_pockets() -> list[dict]:
    return list(_POCKETS)


def get_commands() -> list[dict]:
    return list(_COMMANDS)


def find_free(trigger: str) -> dict | None:
    t = trigger.strip().lower()
    for c in _COMMANDS:
        if c["free"] and c["trigger"].strip().lower() == t:
            return c
    return None


def find_buy(trigger: str) -> dict | None:
    t = trigger.strip().lower()
    for c in _COMMANDS:
        if not c["free"] and c["trigger"].strip().lower() == t:
            return c
    return None
