
"""ИИ-модуль: запросы к Pollinations + контекст диалога."""
import os
import asyncio
import time
import logging
from typing import Optional

import aiohttp
import db
from cachetools import TTLCache

logger = logging.getLogger(__name__)

AI_TOKEN = os.getenv("AI_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "llama"

INSUFFICIENT_TOKENS_PREFIX = "🧠 Недостаточно токенов:"
MAX_PROMPT_CHARS = 16000
MAX_OUTPUT_TOKENS = 4096

COOLDOWN_SEC = 5.0
MAX_HISTORY = 10

_last_request = TTLCache(maxsize=10000, ttl=COOLDOWN_SEC * 2)
_cooldown_lock = asyncio.Lock()

_history = TTLCache(maxsize=5000, ttl=3600)

# Не допускаем одновременную обработку запросов одного пользователя.
_user_locks = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


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
                total=120,
                connect=15,
                sock_read=90,
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
        logger.error("ai: BASE_URL не задан")
        return ""

    if not BASE_URL.startswith(("http://", "https://")):
        logger.error("ai: неправильный BASE_URL")
        return ""

    if BASE_URL.endswith("/v1/chat/completions"):
        return BASE_URL

    if BASE_URL.endswith("/v1"):
        return f"{BASE_URL}/chat/completions"

    return f"{BASE_URL}/v1/chat/completions"


def reset_history(user_id: int) -> None:
    _history.pop(user_id, None)


def _get_history(user_id: int) -> list:
    try:
        return _history[user_id]
    except KeyError:
        _history[user_id] = []
        return _history[user_id]


def _append(user_id: int, role: str, content: str) -> None:
    history = _get_history(user_id)

    history.append({
        "role": role,
        "content": content,
    })

    while len(history) > MAX_HISTORY:
        history.pop(0)


def count_words(text: str) -> int:
    return len((text or "").split())


async def ask_ai(
    prompt: str,
    user_id: Optional[int] = None,
) -> Optional[str]:

    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан")
        return None

    url = _api_url()

    if not url:
        return None

    prompt = (prompt or "").strip()

    if not prompt:
        return None

    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS]

    if user_id is not None:
        async with _get_user_lock(user_id):
            return await _ask_ai_locked(prompt, user_id, url)

    return await _ask_ai_locked(prompt, None, url)


async def _ask_ai_locked(
    prompt: str,
    user_id: Optional[int],
    url: str,
) -> Optional[str]:

    token_cost = max(1, count_words(prompt))
    charged = False
    success = False

    if user_id is not None:
        charged = await db.consume_ai_tokens(
            user_id,
            token_cost,
        )

        if not charged:
            balance = await db.get_ai_tokens(user_id)

            return (
                f"{INSUFFICIENT_TOKENS_PREFIX} нужно {token_cost}, "
                f"у тебя {balance}.\n"
                "Купи токены в «Магазине» → «Токены ИИ»."
            )

    try:
        messages = [{
            "role": "system",
            "content": (
                "Ты умный ассистент женского пола по имени Ксения. "
                "Пиши в дружеском стиле. Не нужен пафос. "
                "Будь на лайтах. Пиши максимально коротко и не сухо!"
            ),
        }]

        if user_id is not None:
            messages.extend(
                dict(message)
                for message in _get_history(user_id)
            )

        messages.append({
            "role": "user",
            "content": prompt,
        })

        payload = {
            "model": MODEL,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_TOKEN}",
        }

        session = await _get_session()

        async with session.post(
            url,
            json=payload,
            headers=headers,
        ) as resp:

            if resp.status == 401:
                logger.error("ai: 401 Unauthorized")
                return None

            if resp.status == 402:
                logger.error("ai: 402 Payment Required")
                return None

            if resp.status == 429:
                logger.warning(
                    "ai: 429 Rate Limit: %s",
                    resp.headers.get("Retry-After", "?"),
                )
                return None

            if resp.status != 200:
                body = await resp.text()

                logger.warning(
                    "ai: HTTP %s — %s",
                    resp.status,
                    body[:500],
                )

                return None

            data = await resp.json(content_type=None)

            choices = data.get("choices") or []

            if not choices:
                logger.warning("ai: пустой ответ")
                return None

            choice = choices[0]

            if choice.get("finish_reason") == "length":
                logger.warning("ai: достигнут лимит ответа")

            message = choice.get("message") or {}
            content = (message.get("content") or "").strip()

            if not content:
                return None

            output_cost = count_words(content)

            if user_id is not None and output_cost > 0:
                output_charged = await db.consume_ai_tokens(
                    user_id,
                    output_cost,
                )

                if not output_charged:
                    try:
                        await db.refund_ai_tokens(
                            user_id,
                            token_cost,
                        )
                    except Exception:
                        logger.exception(
                            "ai: ошибка возврата токенов"
                        )

                    charged = False
                    success = True

                    balance = await db.get_ai_tokens(user_id)
                    total_needed = token_cost + output_cost

                    return (
                        f"{INSUFFICIENT_TOKENS_PREFIX} нужно "
                        f"{total_needed} "
                        f"(запрос: {token_cost}, ответ: {output_cost}), "
                        f"у тебя {balance}.\n"
                        "Купи токены в «Магазине» → «Токены ИИ»."
                    )

            if user_id is not None:
                _append(user_id, "user", prompt)
                _append(user_id, "assistant", content)

            success = True

            return content

    except asyncio.TimeoutError:
        logger.warning("ai: таймаут запроса")
        return None

    except aiohttp.ClientError as exc:
        logger.warning("ai: ошибка сети: %s", exc)
        return None

    except Exception as exc:
        logger.exception("ai: неожиданная ошибка: %s", exc)
        return None

    finally:
        if charged and not success:
            try:
                refunded = await db.refund_ai_tokens(
                    user_id,
                    token_cost,
                )

                if not refunded:
                    logger.error(
                        "ai: не удалось вернуть токены user=%s",
                        user_id,
                    )

            except Exception:
                logger.exception(
                    "ai: ошибка возврата токенов user=%s",
                    user_id,
                )


async def try_acquire(user_id: int) -> float:
    async with _cooldown_lock:
        now = time.monotonic()
        last = _last_request.get(user_id)

        if last is not None:
            elapsed = now - last

            if elapsed < COOLDOWN_SEC:
                return COOLDOWN_SEC - elapsed

        _last_request[user_id] = now

        return 0.0
