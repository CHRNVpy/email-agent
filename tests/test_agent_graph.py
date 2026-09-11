import pytest

from app.agents import graph
from app.agents.specialists import Specialist
from app.agents.state import AgentState, RoutingDecision
from app.messages import IncomingEmail
from app.permissions.policy import Principal
from app.permissions.service import reset_principal, set_principal

EMAIL = IncomingEmail(id="m1", thread_id="t1", sender="a@example.com", subject="Task", body="Do the thing")


async def finance_run(state: AgentState) -> str:
    return "AAPL EPS 1.52"


async def sheets_run(state: AgentState) -> str:
    previous = state.results[-1].output if state.results else "nothing"
    return f"Wrote '{previous}' to the tracker"


FAKE_SPECIALISTS = [
    Specialist("finance", "market data", finance_run, is_allowed=lambda p: p.can("finance:read")),
    Specialist("sheets", "spreadsheets", sheets_run, is_allowed=lambda p: p.can("sheets:write")),
]


@pytest.fixture
def routed(monkeypatch):
    """Patch the router LLM to return a given decision, and use fake specialists."""
    decision = {}

    async def fake_run_chat(model, fn, **limits):
        return RoutingDecision(**decision)

    monkeypatch.setattr(graph, "run_chat", fake_run_chat)
    monkeypatch.setattr(graph, "available_specialists", lambda: FAKE_SPECIALISTS)
    graph.build_graph.cache_clear()
    yield decision
    graph.build_graph.cache_clear()


@pytest.fixture
def principal():
    token = set_principal(Principal("a@example.com", granted=frozenset({"finance:read", "sheets:write"})))
    yield
    reset_principal(token)


async def test_chain_passes_results_between_specialists(routed, principal):
    routed.update(agents=["finance", "sheets"], confidence=0.9, reasoning="needs data then a write")
    assert await graph.run_agent(EMAIL) == "Wrote 'AAPL EPS 1.52' to the tracker"


async def test_low_confidence_returns_clarifying_question(routed, principal):
    routed.update(agents=["finance"], confidence=0.2, reasoning="?", clarification="Which ticker do you mean?")
    assert await graph.run_agent(EMAIL) == "Which ticker do you mean?"


async def test_unknown_agents_are_ignored(routed, principal):
    routed.update(agents=["teleport"], confidence=0.95, reasoning="?")
    assert await graph.run_agent(EMAIL) == graph.FALLBACK_REPLY


async def test_permission_denied_step_is_reported(routed):
    token = set_principal(Principal("guest@example.com", granted=frozenset({"finance:read"})))
    try:
        routed.update(agents=["sheets"], confidence=0.9, reasoning="write")
        assert "don't have permission" in await graph.run_agent(EMAIL)
    finally:
        reset_principal(token)


async def test_router_failure_gives_a_polite_reply(monkeypatch):
    from app.agents import graph as graph_module

    async def broken(state):
        raise RuntimeError("2 validation errors for RoutingDecision")

    monkeypatch.setattr(graph_module, "plan_request", broken)
    result = await graph_module.route(AgentState(email=IncomingEmail(id="1", thread_id="t", sender="a@example.com")))
    assert result == {"plan": [], "response": graph_module.ROUTING_FAILED_REPLY}
