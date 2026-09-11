"""Per-request latency and token accounting.

    with telemetry.trace() as t:
        reply = await answer(email, principal)
    t.summary()  # {"total_s": 7.9, "spans": {...}, "tokens": {...}, "cost_usd": 0.0041}

Spans and token usage are attached to the trace of the current task via a
ContextVar, so instrumented code does not need to pass anything around.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Output includes thinking tokens.
# Source: https://ai.google.dev/gemini-api/docs/pricing (standard paid tier, page updated 2026-09-08).
PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),  # prompts <= 200k tokens
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-embedding-001": (0.15, 0.0),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    estimated: bool = False  # token counts approximated from text length


@dataclass
class Trace:
    started: float = field(default_factory=time.perf_counter)
    spans: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    usage: dict[str, Usage] = field(default_factory=lambda: defaultdict(Usage))
    tools: list[dict[str, Any]] = field(default_factory=list)
    finished: float | None = None

    @property
    def total_s(self) -> float:
        return (self.finished or time.perf_counter()) - self.started

    def cost_usd(self) -> float | None:
        """Estimated cost; None if a model without a known price was used."""
        total = 0.0
        for model, usage in self.usage.items():
            price = PRICES.get(model)
            if price is None:
                return None
            total += usage.input_tokens * price[0] / 1e6 + usage.output_tokens * price[1] / 1e6
        return total

    def summary(self) -> dict[str, Any]:
        cost = self.cost_usd()
        return {
            "total_s": round(self.total_s, 3),
            "spans": {name: round(sum(values), 3) for name, values in self.spans.items()},
            "tokens": {
                model: {"in": u.input_tokens, "out": u.output_tokens, "calls": u.calls, "estimated": u.estimated}
                for model, u in self.usage.items()
            },
            "cost_usd": round(cost, 6) if cost is not None else None,
            "tools": self.tools,
        }


_current: ContextVar[Trace | None] = ContextVar("trace", default=None)
TOOL_OUTPUT_PREVIEW = 400


@contextmanager
def trace() -> Iterator[Trace]:
    current = Trace()
    token = _current.set(current)
    try:
        yield current
    finally:
        current.finished = time.perf_counter()
        _current.reset(token)


@contextmanager
def span(name: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        current = _current.get()
        if current is not None:
            current.spans[name].append(time.perf_counter() - start)


def record_usage(model: str, input_tokens: int, output_tokens: int, *, estimated: bool = False) -> None:
    current = _current.get()
    if current is None:
        return
    usage = current.usage[model]
    usage.input_tokens += input_tokens or 0
    usage.output_tokens += output_tokens or 0
    usage.calls += 1
    usage.estimated |= estimated


def record_genai_usage(model: str, response: Any) -> None:
    """Token usage of a native google-genai response (thinking tokens are billed as output)."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return
    output = (meta.candidates_token_count or 0) + (getattr(meta, "thoughts_token_count", None) or 0)
    record_usage(model, meta.prompt_token_count or 0, output)


def record_tool(name: str, input: Any, *, output: Any = None, error: str | None = None) -> None:
    """Record a tool-like action executed by code (not through LangChain) in the current trace."""
    current = _current.get()
    if current is None:
        return
    entry: dict[str, Any] = {"name": name, "input": input}
    if error is not None:
        entry["error"] = error
    else:
        text = str(output)
        entry["output"] = text if len(text) <= TOOL_OUTPUT_PREVIEW else text[:TOOL_OUTPUT_PREVIEW] + "…"
    current.tools.append(entry)


class UsageCallback(BaseCallbackHandler):
    """LangChain callback that forwards chat-model token usage to the current trace."""

    run_inline = True  # keep the caller's context so the ContextVar trace is visible

    def __init__(self, model: str):
        self.model = model

    def on_llm_end(self, response, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    record_usage(self.model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


class ToolTraceCallback(BaseCallbackHandler):
    """Records every tool call (name, arguments, output preview, errors) in the current trace."""

    run_inline = True

    def __init__(self) -> None:
        self._pending: dict[Any, dict[str, Any]] = {}

    def on_tool_start(self, serialized, input_str: str, *, run_id, inputs=None, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        self._pending[run_id] = {"name": name, "input": inputs if inputs is not None else input_str}

    def _finish(self, run_id, **fields: Any) -> None:
        call = self._pending.pop(run_id, {"name": "tool"})
        current = _current.get()
        if current is not None:
            current.tools.append({**call, **fields})

    def on_tool_end(self, output: Any, *, run_id, **kwargs: Any) -> None:
        text = str(getattr(output, "content", output))
        preview = text if len(text) <= TOOL_OUTPUT_PREVIEW else text[:TOOL_OUTPUT_PREVIEW] + "…"
        self._finish(run_id, output=preview)

    def on_tool_error(self, error: BaseException, *, run_id, **kwargs: Any) -> None:
        self._finish(run_id, error=repr(error))
