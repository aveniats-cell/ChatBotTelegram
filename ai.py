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

logger = logging.getLogger(__name__)

# Оба параметра обязательны — берутся из Values на Render.
AI_TOKEN = os.getenv("AI_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "gemini-flash-lite-3.1"  # бесплатная модель без ключа, но с ключом тоже работает

COOLDOWN_SEC = 5.0

_last_request: dict[int, float] = {}


def _api_url() -> str:
    """Собирает URL к OpenAI-совместимому endpoint.

    BASE_URL из Values должен быть https://gen.pollinations.ai
    (или с /v1 на конце). Легаси text.pollinations.ai больше не используется.
    """
    if not BASE_URL:
        logger.error("ai: BASE_URL не задан в переменных окружения")
        return ""

    base = BASE_URL
    # Уже полный путь
    if base.endswith("/v1/chat/completions"):
        return base
    # Уже с /v1 — добавляем только хвост
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    # Голый хост — добавляем /v1/chat/completions
    return f"{base}/v1/chat/completions"


async def ask_ai(prompt: str) -> Optional[str]:
    """Отправляет запрос в gemini. Возвращает текст или None."""
    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан в переменных окружения")
        return None

    url = _api_url()
    if not url:
        return None

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
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_TOKEN}",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401:
                    logger.error("ai: 401 Unauthorized — проверь AI_TOKEN")
                    return None
                if resp.status == 402:
                    logger.error("ai: 402 Payment Required — закончились Pollen credits")
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("ai: HTTP %s — %s", resp.status, body[:300])
                    return None

                data = await resp.json()
                choices = data.get("choices") or []
                if not choices:
                    logger.warning("ai: пустой ответ — %s", str(data)[:300])
                    return None
                msg = choices[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                return content or None

    except aiohttp.ClientError as e:
        logger.warning("ai: сеть — %s", e)
        return None
    except Exception as e:
        logger.exception("ai: неожиданная ошибка — %s", e)
        return None


def check_cooldown(user_id: int) -> float:
    """Возвращает оставшиеся секунды (0, если можно)."""
    now = time.monotonic()
    last = _last_request.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < COOLDOWN_SEC:
        return COOLDOWN_SEC - elapsed
    return 0.0


def touch_cooldown(user_id: int) -> None:
    _last_request[user_id] = time.monotonic()
