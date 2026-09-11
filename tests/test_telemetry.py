import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app import telemetry
from app.config import Settings
from evals.e2e import aggregate


def test_trace_collects_spans_usage_and_cost():
    with telemetry.trace() as trace:
        with telemetry.span("router"):
            pass
        telemetry.record_usage("gemini-2.5-flash", 1_000_000, 100_000)
        telemetry.record_usage("gemini-2.5-flash", 0, 0)
    summary = trace.summary()
    assert "router" in summary["spans"]
    assert summary["tokens"]["gemini-2.5-flash"] == {"in": 1_000_000, "out": 100_000, "calls": 2, "estimated": False}
    assert summary["cost_usd"] == pytest.approx(0.30 + 0.25)


def test_unknown_model_price_gives_no_cost():
    with telemetry.trace() as trace:
        telemetry.record_usage("grok-4", 10, 10)
    assert trace.cost_usd() is None


def test_usage_outside_a_trace_is_ignored():
    telemetry.record_usage("gemini-2.5-flash", 5, 5)  # must not raise


def test_langchain_callback_forwards_usage():
    message = AIMessage(content="hi", usage_metadata={"input_tokens": 12, "output_tokens": 30, "total_tokens": 42})
    with telemetry.trace() as trace:
        telemetry.UsageCallback("gemini-2.5-flash").on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]])
        )
    assert trace.usage["gemini-2.5-flash"].output_tokens == 30


def test_e2e_aggregate_ignores_failed_requests():
    results = [
        {"total_s": 4.0, "cost_usd": 0.002, "tokens": 3000, "error": None},
        {"total_s": 9.0, "cost_usd": 0.004, "tokens": 5000, "error": None},
        {"total_s": 1.0, "cost_usd": None, "tokens": 0, "error": "boom"},
    ]
    totals = aggregate(results)
    assert totals["errors"] == 1
    assert totals["latency_p50_s"] == 4.0 and totals["latency_p95_s"] == 9.0
    assert totals["cost_mean_usd"] == pytest.approx(0.003)


def test_demo_env_file_parses(monkeypatch):
    for name in ("SQL_DATABASES", "ALLOWED_SENDERS", "AGENT_EMAIL", "ADMIN_EMAIL", "GEMINI_API_KEYS"):
        monkeypatch.delenv(name, raising=False)  # environment variables would override the file
    demo = Settings(_env_file="demo/.env.demo")
    assert demo.sql_databases["crm"].url.endswith("data/demo/crm.db")
    assert demo.allowed_senders == ["your-personal@gmail.com"]
