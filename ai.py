"""ИИ-модуль: запросы к gemini через Pollinations + контекст диалога.

Использует AI_TOKEN и BASE_URL из переменных окружения.
BASE_URL должен указывать на https://gen.pollinations.ai (актуальный gateway).
Легаси-хост text.pollinations.ai отключён и возвращает 404.

Контекст: на пользователя хранится до MAX_HISTORY сообщений (user+assistant)
в TTLCache на час. Сброс — ai.reset_history(user_id) (/restart).
"""
import os
import time
import logging
from typing import Optional

import aiohttp
from cachetools import TTLCache

logger = logging.getLogger(__name__)

AI_TOKEN = os.getenv("AI_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "qwen-coder"

COOLDOWN_SEC = 5.0

MAX_HISTORY = 10  # сообщений (5 пар user/assistant)

_last_request: TTLCache = TTLCache(maxsize=10000, ttl=COOLDOWN_SEC * 2)

# user_id -> list[{role, content}]
_history: TTLCache = TTLCache(maxsize=5000, ttl=3600)

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                ttl_dns_cache=300,
            ),
            timeout=aiohttp.ClientTimeout(
                total=30,
                connect=10,
                sock_read=20,
            ),
        )
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _api_url() -> str:
    if not BASE_URL:
        logger.error("ai: BASE_URL не задан в переменных окружения")
        return ""
    if not BASE_URL.startswith(("http://", "https://")):
        logger.error("ai: BASE_URL должен начинаться с http:// или https://")
        return ""
    base = BASE_URL
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def reset_history(user_id: int) -> None:
    _history.pop(user_id, None)


def _get_history(user_id: int) -> list:
    return _history.setdefault(user_id, [])


def _append(user_id: int, role: str, content: str) -> None:
    h = _history.setdefault(user_id, [])
    h.append({"role": role, "content": content})
    if len(h) > MAX_HISTORY:
        del h[: len(h) - MAX_HISTORY]


async def ask_ai(prompt: str, user_id: Optional[int] = None) -> Optional[str]:
    """Отправляет запрос в gemini. Возвращает текст или None.

    Если user_id задан — учитывает историю диалога и дописывает пару
    (user, assistant) в историю после успешного ответа.
    """
    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан в переменных окружения")
        return None

    url = _api_url()
    if not url:
        return None

    if len(prompt) > 8000:
        prompt = prompt[:8000]

    messages = [{
        "role": "system",
        "content": "Ты умный чат-бот в Telegram. Пиши максимально кратко.",
    }]
    if user_id is not None:
        messages.extend(_get_history(user_id))
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 2048,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_TOKEN}",
    }

    session = await _get_session()

    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status == 401:
                logger.error("ai: 401 Unauthorized — проверь AI_TOKEN")
                return None
            if resp.status == 402:
                logger.error("ai: 402 Payment Required — закончились Pollen credits")
                return None
            if resp.status == 429:
                retry_after = resp.headers.get("Retry-After", "?")
                logger.warning("ai: 429 Rate Limit — Retry-After: %s", retry_after)
                return None
            if resp.status != 200:
                body = await resp.text()
                logger.warning("ai: HTTP %s — %s", resp.status, body[:300])
                return None

            data = await resp.json(content_type=None)
            choices = data.get("choices") or []
            if not choices:
                logger.warning("ai: пустой ответ — %s", repr(data)[:300])
                return None

            choice = choices[0]
            finish = choice.get("finish_reason")
            if finish == "length":
                logger.warning("ai: ответ обрезан по max_tokens")

            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                return None

            if user_id is not None:
                _append(user_id, "user", prompt)
                _append(user_id, "assistant", content)
            return content

    except aiohttp.ClientError as e:
        logger.warning("ai: сеть — %s", e)
        return None
    except Exception as e:
        logger.exception("ai: неожиданная ошибка — %s", e)
        return None


def try_acquire(user_id: int) -> float:
    """Атомарно проверяет и занимает cooldown.

    Возвращает 0, если можно выполнять запрос, иначе — оставшиеся секунды.
    """
    now = time.monotonic()
    last = _last_request.get(user_id)
    if last is not None:
        elapsed = now - last
        if elapsed < COOLDOWN_SEC:
            return COOLDOWN_SEC - elapsed
    _last_request[user_id] = now
    return 0.0
