import pytest

from app import llm


def test_key_pool_skips_parked_keys_and_uses_fallback():
    pool = llm.KeyPool(["a", "b"], fallback="paid")
    pool.park("a")
    assert pool.acquire() == "b"
    pool.park("b")
    assert pool.acquire() == "paid"


def test_key_pool_without_keys_fails_clearly():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEYS"):
        llm.KeyPool([]).acquire()


async def test_rotation_retries_on_quota_errors(monkeypatch):
    monkeypatch.setattr(llm, "key_pool", llm.KeyPool(["k1", "k2"]))
    monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
    used = []

    async def call(key):
        used.append(key)
        if len(used) == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"

    assert await llm.with_key_rotation(call) == "ok"
    assert len(used) == 2 and used[0] != used[1]


async def test_non_quota_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "key_pool", llm.KeyPool(["k1"]))
    calls = 0

    async def call(key):
        nonlocal calls
        calls += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        await llm.with_key_rotation(call)
    assert calls == 1


def test_model_routing():
    assert llm.is_xai_model("grok-4-fast")
    assert llm.gemini_model("grok-4") == llm.GEMINI_NATIVE_FALLBACK
    assert llm.gemini_model("gemini-2.5-pro") == "gemini-2.5-pro"


async def _no_sleep(_):
    return None
