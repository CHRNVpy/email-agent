"""The SQL specialist end-to-end against SQLite, with a scripted LLM instead of Gemini."""

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import text

from app import db
from app.agents import specialists
from app.agents.state import AgentState
from app.config import SqlDatabase, settings
from app.messages import IncomingEmail
from app.permissions.policy import Principal
from app.permissions.service import set_principal


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


def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def echo_last_tool_result(messages) -> AIMessage:
    return AIMessage(content=f"RESULT: {messages[-1].content}")


@pytest.fixture
async def shop_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sql_databases", {"shop": SqlDatabase(url=f"sqlite+aiosqlite:///{tmp_path}/shop.db")})
    db.data_engine.cache_clear()
    async with db.data_engine("shop").begin() as conn:
        await conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)"))
        await conn.execute(text("INSERT INTO orders VALUES (1, 'Acme', 120.5), (2, 'Globex', 80)"))
    yield
    await db.data_engine("shop").dispose()
    db.data_engine.cache_clear()


@pytest.fixture
def scripted(monkeypatch):
    steps: list[Any] = []

    async def fake_run_chat(model, fn):
        return await fn(ScriptedModel(steps=steps))

    monkeypatch.setattr(specialists, "run_chat", fake_run_chat)
    return steps


@pytest.fixture
def as_user():
    def _set(*granted: str, denied: tuple[str, ...] = ()):
        set_principal(Principal("u@example.com", 1, frozenset(granted), frozenset(denied)))

    yield _set
    set_principal(None)


def state() -> AgentState:
    return AgentState(email=IncomingEmail(id="1", thread_id="t", sender="u@example.com", body="Top customers?"))


async def test_schema_then_select(shop_db, scripted, as_user):
    as_user("sql:*:read")
    scripted += [
        call("get_sql_schema", database="shop"),
        call(
            "run_sql_select",
            database="shop",
            query="SELECT customer FROM orders WHERE total > :min",
            params={"min": 100},
        ),
        echo_last_tool_result,
    ]
    answer = await specialists.sql(state())
    assert '"customer": "Acme"' in answer
    assert "Globex" not in answer


async def test_write_is_blocked_by_denial(shop_db, scripted, as_user):
    as_user("sql:*:read", "sql:*:write", denied=("sql:shop:write",))
    scripted += [call("run_sql_write", database="shop", query="DELETE FROM orders WHERE id = 1"), echo_last_tool_result]
    assert "not allowed" in await specialists.sql(state())


async def test_unsafe_select_is_rejected(shop_db, scripted, as_user):
    as_user("sql:*:read")
    scripted += [call("run_sql_select", database="shop", query="DROP TABLE orders"), echo_last_tool_result]
    assert "Only SELECT" in await specialists.sql(state())
    async with db.data_engine("shop").connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM orders"))).scalar() == 2
