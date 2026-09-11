"""LLM access: model factory, Gemini API-key rotation and quota-aware retries.

Models are addressed by name. `grok-*` models go to xAI (OpenAI-compatible API),
everything else to Google Gemini. Several Gemini keys can be configured; when one
hits its quota (HTTP 429 / RESOURCE_EXHAUSTED) it is parked for a minute and the
call is retried with the next key, falling back to an optional paid key last.
"""

import asyncio
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable

from google import genai
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from app.config import settings

logger = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
GEMINI_NATIVE_FALLBACK = "gemini-2.5-flash"
_QUOTA_MARKERS = ("429", "resource_exhausted", "quota", "rate_limit", "rate limit")


class KeyPool:
    """Round-robins over free-tier keys and falls back to a paid key when all are exhausted."""

    def __init__(self, keys: list[str], fallback: str = "", cooldown: float = 60.0):
        self.keys = [k for k in keys if k]
        self.fallback = fallback
        self.cooldown = cooldown
        self._parked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self) -> str:
        with self._lock:
            now = time.monotonic()
            available = [k for k in self.keys if self._parked_until.get(k, 0) <= now]
            if available:
                return random.choice(available)
            if self.fallback:
                return self.fallback
            if self.keys:
                # Everything is parked: pick the key that becomes available first.
                return min(self.keys, key=lambda k: self._parked_until.get(k, 0))
        raise RuntimeError("No Gemini API key configured (set GEMINI_API_KEYS).")

    def park(self, key: str) -> None:
        with self._lock:
            self._parked_until[key] = time.monotonic() + self.cooldown
        logger.warning("Gemini key %s… parked for %ss (quota exhausted)", key[:6], int(self.cooldown))


key_pool = KeyPool(settings.gemini_api_keys, settings.gemini_fallback_api_key)


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def is_xai_model(model: str) -> bool:
    return model.lower().startswith("grok")


def resolve_model(model: str | None) -> str:
    return model or settings.default_model


def gemini_model(model: str | None) -> str:
    """Model for Gemini-only features (search grounding, native files) when a non-Gemini model is selected."""
    model = resolve_model(model)
    return GEMINI_NATIVE_FALLBACK if is_xai_model(model) else model


async def with_key_rotation[T](fn: Callable[[str], Awaitable[T]], *, attempts: int = 5) -> T:
    """Call `fn(api_key)`, retrying with another key on quota errors."""
    last_error: BaseException | None = None
    for attempt in range(attempts):
        key = key_pool.acquire()
        try:
            return await fn(key)
        except Exception as exc:
            if not is_quota_error(exc):
                raise
            last_error = exc
            key_pool.park(key)
            logger.info("Retrying after quota error (attempt %d/%d)", attempt + 1, attempts)
            await asyncio.sleep(1)
    assert last_error is not None
    raise last_error


def chat_model(model: str | None = None, *, api_key: str | None = None, **kwargs) -> BaseChatModel:
    model = resolve_model(model)
    if is_xai_model(model):
        from langchain_openai import ChatOpenAI

        if not settings.xai_api_key:
            raise RuntimeError(f"Model '{model}' needs XAI_API_KEY to be set.")
        return ChatOpenAI(model=model, api_key=settings.xai_api_key, base_url=XAI_BASE_URL, **kwargs)
    # Retries are handled by `with_key_rotation`, so the client must fail fast on 429.
    return ChatGoogleGenerativeAI(model=model, google_api_key=api_key or key_pool.acquire(), max_retries=0, **kwargs)


async def run_chat[T](model: str | None, fn: Callable[[BaseChatModel], Awaitable[T]]) -> T:
    """Run `fn` against a chat model, rotating Gemini keys when needed."""
    model = resolve_model(model)
    if is_xai_model(model):
        return await fn(chat_model(model))
    return await with_key_rotation(lambda key: fn(chat_model(model, api_key=key)))


async def run_genai[T](fn: Callable[[genai.Client], Awaitable[T]]) -> T:
    """Run `fn` with a native google-genai client (search grounding, native file input)."""
    return await with_key_rotation(lambda key: fn(genai.Client(api_key=key)))


def embeddings_model(api_key: str) -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(model=settings.embedding_model, google_api_key=api_key)
