"""ИИ-модуль: запросы к бесплатной модели gemini через Pollinations.

Rate limit: 1 запрос в 5 секунд на пользователя (in-memory).
Модель "gemini" входит в Free Universe — не списывает Pollen.
"""
import os
import time
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Ключ опционален для бесплатной модели gemini.
# Если задан — используем gen.pollinations.ai (тоже бесплатно для gemini).
AI_TOKEN = os.getenv("AI_TOKEN", "").strip()

# Если BASE_URL не задан — код выберет endpoint сам.
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "gemini"

COOLDOWN_SEC = 5.0

_last_request: dict[int, float] = {}


def _api_url() -> str:
    """Собирает полный URL к /v1/chat/completions или /openai."""
    base = BASE_URL
    if not base:
        # Без ключа — старый бесплатный endpoint text.pollinations.ai
        # С ключом — gen.pollinations.ai (тоже бесплатно для gemini)
        base = "https://gen.pollinations.ai" if AI_TOKEN else "https://text.pollinations.ai"

    if base.endswith("/v1/chat/completions") or base.endswith("/openai"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    # text.pollinations.ai принимает /openai, gen.pollinations.ai — /v1/chat/completions
    if "text.pollinations.ai" in base:
        return f"{base}/openai"
    return f"{base}/v1/chat/completions"


async def ask_ai(prompt: str) -> Optional[str]:
    """Отправляет запрос в gemini. Возвращает текст или None."""
    url = _api_url()

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

    headers = {"Content-Type": "application/json"}
    if AI_TOKEN:
        headers["Authorization"] = f"Bearer {AI_TOKEN}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 402:
                    logger.error("ai: 402 Payment Required (модель платная?)")
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
