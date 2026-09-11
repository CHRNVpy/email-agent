"""Idempotency guard for side effects.

Gmail push notifications are delivered at-least-once and LLM agents sometimes
repeat tool calls, so every outbound side effect (reply, email, sheet append,
DB write) is keyed and recorded. A key is *processing* for a short while and
*done* for a week; repeated attempts inside those windows are suppressed.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from app.state import JsonState

PROCESSING_TTL = timedelta(minutes=15)
DONE_TTL = timedelta(days=7)

_store = JsonState("dedupe.json")
# Identifies the unit of work (an email, a workflow stage) that side effects belong to.
_scope: ContextVar[str | None] = ContextVar("dedupe_scope", default=None)


def _now() -> datetime:
    return datetime.now(UTC)


def _is_active(entry: dict, now: datetime) -> bool:
    try:
        age = now - datetime.fromisoformat(entry["at"])
    except (KeyError, TypeError, ValueError):
        return False
    ttl = DONE_TTL if entry.get("status") == "done" else PROCESSING_TTL
    return age <= ttl


def _load_pruned(now: datetime) -> dict[str, dict]:
    keys = _store.load().get("keys", {})
    return {key: entry for key, entry in keys.items() if _is_active(entry, now)}


def claim(key: str) -> bool:
    """Atomically claim a key. Returns False if it is already being processed or done."""
    now = _now()
    with _store.lock:
        keys = _load_pruned(now)
        if key in keys:
            return False
        keys[key] = {"status": "processing", "at": now.isoformat()}
        _store.save({"keys": keys})
        return True


def mark_done(key: str) -> None:
    now = _now()
    with _store.lock:
        keys = _load_pruned(now)
        keys[key] = {"status": "done", "at": now.isoformat()}
        _store.save({"keys": keys})


def release(key: str) -> None:
    """Forget a claim so the action can be retried (used when the side effect failed)."""
    with _store.lock:
        keys = _load_pruned(_now())
        keys.pop(key, None)
        _store.save({"keys": keys})


def is_done(key: str) -> bool:
    entry = _load_pruned(_now()).get(key)
    return bool(entry and entry.get("status") == "done")


def make_key(kind: str, *parts: object) -> str:
    """Build a stable key; long/variable parts are hashed."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]
    return f"{kind}:{digest}"


@contextmanager
def scope(value: str) -> Iterator[None]:
    """Attribute side effects inside the block to `value` (e.g. an email or a workflow stage)."""
    token = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def once(kind: str, *parts: object) -> Iterator[bool]:
    """Guard a side effect inside the current scope.

    Yields True when the action should run; False when an identical action already
    ran (or is running) for this scope. A failing action releases its claim.

        with dedupe.once("email", to, subject, body) as first:
            if not first:
                return "Already sent"
            send(...)
    """
    current = _scope.get()
    if current is None:
        yield True
        return
    key = make_key(kind, current, *parts)
    if not claim(key):
        yield False
        return
    try:
        yield True
    except BaseException:
        release(key)
        raise
    mark_done(key)
