"""ИИ-модуль: запросы к gemini через Pollinations.

Использует AI_TOKEN и BASE_URL из переменных окружения.
BASE_URL должен указывать на https://gen.pollinations.ai (актуальный gateway).
Легаси-хост text.pollinations.ai отключён и возвращает 404.
"""
import os
import time
import logging
from typing import Optional

import aiohttp
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Оба параметра обязательны — берутся из Values на Render.
AI_TOKEN = os.getenv("AI_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "mistral"

COOLDOWN_SEC = 5.0

# Ограниченный кэш вместо безразмерного dict, чтобы не текла память.
# maxsize=10000 — при большем числе активных пользователей старые записи вытесняются.
_last_request: TTLCache = TTLCache(maxsize=10000, ttl=COOLDOWN_SEC * 2)

# Одна сессия на модуль — переиспользование соединений, DNS-кэша и TLS.
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Возвращает (и при необходимости создаёт) общую aiohttp-сессию."""
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
    """Закрывает сессию. Вызывать при shutdown приложения."""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _api_url() -> str:
    """Собирает URL к OpenAI-совместимому endpoint."""
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


async def ask_ai(prompt: str) -> Optional[str]:
    """Отправляет запрос в gemini. Возвращает текст или None."""
    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан в переменных окружения")
        return None

    url = _api_url()
    if not url:
        return None

    # Ограничиваем длину prompt, чтобы не ловить 400/413 от API.
    if len(prompt) > 8000:
        prompt = prompt[:8000]

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты чат-бот в группе Telegram, тебя разработали Aveniats, "
                    "а твое имя Brainy."
                ),
            },
            {"role": "user", "content": prompt},
        ],
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
            return content or None

    except aiohttp.ClientError as e:
        logger.warning("ai: сеть — %s", e)
        return None
    except Exception as e:
        logger.exception("ai: неожиданная ошибка — %s", e)
        return None


def try_acquire(user_id: int) -> float:
    """Атомарно проверяет и занимает cooldown.

    Возвращает 0, если можно выполнять запрос (и сразу записывает метку времени),
    иначе — оставшиеся секунды.
    """
    now = time.monotonic()
    last = _last_request.get(user_id)
    if last is not None:
        elapsed = now - last
        if elapsed < COOLDOWN_SEC:
            return COOLDOWN_SEC - elapsed
    _last_request[user_id] = now
    return 0.0
