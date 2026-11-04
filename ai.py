"""ИИ-модуль: запросы к gemini через Pollinations + контекст диалога."""
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

MODEL = "openai-fast"

INSUFFICIENT_TOKENS_PREFIX = "🧠 Недостаточно токенов:"
MAX_PROMPT_CHARS = 16000
MAX_OUTPUT_TOKENS = 4096

COOLDOWN_SEC = 5.0

MAX_HISTORY = 10

_last_request: TTLCache = TTLCache(maxsize=10000, ttl=COOLDOWN_SEC * 2)
_cooldown_lock = asyncio.Lock()

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
    # __getitem__ в TTLCache обновляет TTL; setdefault — нет.
    try:
        return _history[user_id]
    except KeyError:
        _history[user_id] = []
        return _history[user_id]


def _append(user_id: int, role: str, content: str) -> None:
    h = _get_history(user_id)
    h.append({"role": role, "content": content})
    # Режем парами user/assistant, чтобы не оставить «висячего» user.
    while len(h) > MAX_HISTORY:
        h.pop(0)


def count_words(text: str) -> int:
    """Считает слова по пробельным разделителям; повторные пробелы не считаются."""
    return len((text or "").split())


async def ask_ai(prompt: str, user_id: Optional[int] = None) -> Optional[str]:
    """Отправляет запрос в API. 1 токен списывается за каждое слово запроса.

    Списание централизовано здесь, поэтому все модули, вызывающие ask_ai с
    user_id, используют одну и ту же тарификацию. При любой ошибке API токены
    возвращаются в исходном количестве.
    """
    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан в переменных окружения")
        return None

    url = _api_url()
    if not url:
        return None

    prompt = (prompt or "").strip()
    if not prompt:
        return None
    if len(prompt) > MAX_PROMPT_CHARS:
        logger.info("ai: запрос обрезан с %s до %s символов", len(prompt), MAX_PROMPT_CHARS)
        prompt = prompt[:MAX_PROMPT_CHARS]

    # Считаем только новое сообщение пользователя, не историю диалога.
    token_cost = max(1, count_words(prompt))
    charged = False
    if user_id is not None:
        charged = await db.consume_ai_tokens(user_id, token_cost)
        if not charged:
            balance = await db.get_ai_tokens(user_id)
            return (
                f"{INSUFFICIENT_TOKENS_PREFIX} нужно {token_cost}, "
                f"у тебя {balance}.\n"
                "Купи токены в «Магазине» → «Токены ИИ»."
            )

    success = False
    try:
        messages = [{
            "role": "system",
            "content": "Ты умный ассистент женского пола по имени Ксения."
                       "Пиши в дружеском стиле. Не нужен пафос. Будь на лайтах.",
        }]
        if user_id is not None:
            messages.extend(dict(m) for m in _get_history(user_id))
        messages.append({"role": "user", "content": prompt})

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
                logger.warning("ai: HTTP %s — %s", resp.status, body[:500])
                return None

            data = await resp.json(content_type=None)
            choices = data.get("choices") or []
            if not choices:
                logger.warning("ai: пустой ответ — %s", repr(data)[:500])
                return None

            choice = choices[0]
            if choice.get("finish_reason") == "length":
                logger.warning("ai: ответ достиг лимита max_tokens=%s", MAX_OUTPUT_TOKENS)

            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                return None

            if user_id is not None:
                _append(user_id, "user", prompt)
                _append(user_id, "assistant", content)
            success = True
            return content

    except asyncio.TimeoutError as exc:
        logger.warning("ai: таймаут запроса: %s", exc)
        return None
    except aiohttp.ClientError as exc:
        logger.warning("ai: сеть — %s", exc)
        return None
    except Exception as exc:
        logger.exception("ai: неожиданная ошибка — %s", exc)
        return None
    finally:
        if charged and not success:
            try:
                refunded = await db.refund_ai_tokens(user_id, token_cost)
                if not refunded:
                    logger.error("ai: НЕ УДАЛОСЬ вернуть %s токенов user=%s", token_cost, user_id)
            except Exception:
                logger.exception("ai: ошибка возврата %s токенов user=%s", token_cost, user_id)


async def try_acquire(user_id: int) -> float:
    """Атомарно проверяет и занимает cooldown.

    Возвращает 0, если можно выполнять запрос, иначе — оставшиеся секунды.
    """
    async with _cooldown_lock:
        now = time.monotonic()
        last = _last_request.get(user_id)
        if last is not None:
            elapsed = now - last
            if elapsed < COOLDOWN_SEC:
                return COOLDOWN_SEC - elapsed
        _last_request[user_id] = now
        return 0.0
