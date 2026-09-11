import pytest

from app import llm
from app.llm import chat_model as real_chat_model  # captured before conftest blocks real LLM calls


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


def test_thinking_budget_only_for_gemini_25(monkeypatch):
    captured = {}

    class FakeGemini:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", FakeGemini)
    real_chat_model("gemini-2.5-flash", api_key="k", thinking_budget=512, max_output_tokens=100)
    assert captured["thinking_budget"] == 512 and captured["max_output_tokens"] == 100
    assert captured["timeout"] == llm.settings.llm_timeout_s
    captured.clear()
    real_chat_model("gemini-3.1-flash-lite", api_key="k", thinking_budget=512)
    assert "thinking_budget" not in captured and "max_output_tokens" not in captured


async def test_run_chat_times_out_hanging_calls(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_timeout_s", 0.05)
    monkeypatch.setattr(llm.settings, "xai_api_key", "x")
    monkeypatch.setattr(llm, "chat_model", lambda *args, **kwargs: "model")

    async def hangs(model):
        await llm.asyncio.sleep(5)

    with pytest.raises(TimeoutError):
        await llm.run_chat("grok-4", hangs)
