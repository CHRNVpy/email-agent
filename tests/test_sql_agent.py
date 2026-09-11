"""The SQL specialist end-to-end against SQLite, with a scripted LLM instead of Gemini.

The model only proposes a plan; these tests check that code executes it, repairs
failures, refuses unsafe or unauthorised statements and builds the reply from the
rows that were actually returned.
"""

from datetime import date
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import text

from app import db, telemetry
from app.agents import specialists, sql_agent
from app.agents.state import AgentState
from app.config import SqlDatabase, settings
from app.messages import IncomingEmail
from app.permissions.policy import Principal
from app.permissions.service import set_principal
from app.tools import sql as sql_tools


class ScriptedModel(BaseChatModel):
    """Replays a list of responses; a callable step receives the conversation so far."""

    steps: list[Any]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        step = self.steps.pop(0)
        message = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted"


def plan(*queries: str, clarification: str = "") -> AIMessage:
    args = {
        "queries": [{"database": "shop", "sql": q, "purpose": "test query"} for q in queries],
        "clarification": clarification,
    }
    return AIMessage(content="", tool_calls=[{"name": "SqlPlan", "args": args, "id": "plan"}])


def echo_results(messages) -> AIMessage:
    """Answer step: repeat the query results the code passed in (proves what the model saw)."""
    return AIMessage(content="RESULTS " + messages[-1].content.split("Query results:", 1)[1])


def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


@pytest.fixture
async def shop_db(tmp_path, monkeypatch):
    shop = SqlDatabase(url=f"sqlite+aiosqlite:///{tmp_path}/shop.db", description="Shop", notes="Revenue = SUM(total).")
    sql_tools._schema_cache.clear()
    monkeypatch.setattr(settings, "sql_databases", {"shop": shop})
    db.data_engine.cache_clear()
    async with db.data_engine("shop").begin() as conn:
        await conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL, status TEXT)"))
        await conn.execute(text("INSERT INTO orders VALUES (1, 'Acme', 120.5, 'open'), (2, 'Globex', 80, 'paid')"))
    yield
    await db.data_engine("shop").dispose()
    db.data_engine.cache_clear()


@pytest.fixture
def scripted(monkeypatch):
    model = ScriptedModel(steps=[])  # one instance: pydantic would copy a shared list per model

    async def fake_run_chat(_model_name, fn, **limits):
        return await fn(model)

    monkeypatch.setattr(sql_agent, "run_chat", fake_run_chat)
    monkeypatch.setattr(specialists, "run_chat", fake_run_chat)
    return model.steps


@pytest.fixture
def as_user():
    def _set(*granted: str, denied: tuple[str, ...] = ()):
        set_principal(Principal("u@example.com", 1, frozenset(granted), frozenset(denied)))

    yield _set
    set_principal(None)


def state() -> AgentState:
    return AgentState(email=IncomingEmail(id="1", thread_id="t", sender="u@example.com", body="Top customers?"))


async def count_orders() -> int:
    async with db.data_engine("shop").connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM orders"))).scalar()


async def test_code_executes_the_plan_and_reply_uses_real_rows(shop_db, scripted, as_user):
    as_user("sql:*:read")
    seen_system = []

    def planner(messages):
        seen_system.append(messages[0].content)
        return plan("SELECT customer, total FROM orders WHERE total > 100")

    scripted += [planner, echo_results]
    with telemetry.trace() as trace:
        reply = await specialists.sql(state())

    answer, appendix = reply.split("**Query used**")
    assert '"customer": "Acme"' in answer and "Globex" not in answer
    assert "```sql\nSELECT customer, total FROM orders WHERE total > 100\n```" in appendix  # added by code
    assert "### shop [sqlite]" in seen_system[0]
    assert "status: text, values: 'open' | 'paid'" in seen_system[0]  # low-cardinality sample values
    assert "Notes: Revenue = SUM(total)." in seen_system[0]
    assert f"Today is {date.today():%A, %d %B %Y}" in seen_system[0]
    assert trace.tools[0]["name"] == "select_rows" and "Acme" in trace.tools[0]["output"]


async def test_failed_query_gets_one_repair(shop_db, scripted, as_user):
    as_user("sql:*:read")
    scripted += [plan("SELECT nope FROM orders"), plan("SELECT customer FROM orders ORDER BY id"), echo_results]
    with telemetry.trace() as trace:
        reply = await specialists.sql(state())
    assert '"customer": "Acme"' in reply
    assert "no such column" in trace.tools[0]["error"]
    assert trace.tools[1]["name"] == "select_rows"


async def test_unsafe_statement_never_runs(shop_db, scripted, as_user):
    as_user("sql:*:read", "sql:*:write")
    scripted += [plan("DROP TABLE orders"), plan(), echo_results]
    reply = await specialists.sql(state())
    assert "Only SELECT" in reply  # DROP is not a write statement, so the read-only guard rejects it
    assert await count_orders() == 2


async def test_write_denied_by_override_is_reported(shop_db, scripted, as_user):
    as_user("sql:*:read", "sql:*:write", denied=("sql:shop:write",))
    scripted += [plan("DELETE FROM orders WHERE id = 1"), echo_results]
    reply = await specialists.sql(state())
    assert "not allowed" in reply
    assert await count_orders() == 2


async def test_allowed_write_reports_rows_affected(shop_db, scripted, as_user):
    as_user("sql:*:read", "sql:*:write")
    scripted += [plan("UPDATE orders SET total = 99 WHERE id = 2"), echo_results]
    reply = await specialists.sql(state())
    assert '"rows_affected": 1' in reply and "1 row(s) changed" in reply


async def test_clarification_when_tables_cannot_answer(shop_db, scripted, as_user):
    as_user("sql:*:read")
    scripted.append(plan(clarification="Which date range do you mean?"))
    assert await specialists.sql(state()) == "Which date range do you mean?"


async def test_no_readable_database(shop_db, scripted, as_user):
    as_user("web:search")
    assert "don't have access" in await specialists.sql(state())


async def test_tool_agent_refuses_answers_made_without_tools(scripted, as_user):
    as_user("sheets:read")
    scripted += [AIMessage(content="I added 5 rows to your sheet!"), AIMessage(content="Done, trust me.")]
    assert await specialists.sheets(state()) == specialists.UNGROUNDED_REPLY


async def test_tool_agent_accepts_answer_after_reminder(scripted, as_user):
    as_user("sheets:read")
    scripted += [
        AIMessage(content="Your sheet has 3 tabs."),
        call("list_spreadsheets"),  # fails without Google credentials, but the tool was used
        AIMessage(content="The tool reported an authorisation error."),
    ]
    assert await specialists.sheets(state()) == "The tool reported an authorisation error."
