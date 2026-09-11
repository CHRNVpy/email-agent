"""SQL specialist: the model writes the query, code executes it, the reply is built from real rows.

A tool-calling loop lets the model skip the tool and answer from imagination —
with Gemini this produced a plausible but invented "top customers" table. Here the
model cannot bypass execution: it only returns a plan, the queries are executed by
code under the read-only guard and permissions, and the reply is written from the
returned rows. The executed SQL is appended by code, not by the model.
"""

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy.engine import make_url

from app import telemetry
from app.agents import prompts
from app.agents.content import message_text, request_text
from app.agents.state import AgentState
from app.config import settings
from app.db import list_databases
from app.llm import run_chat
from app.permissions.service import PermissionDenied, current_principal
from app.tools.sql import execute_write, get_schema, is_write, select_rows

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
MAX_ROWS_IN_PROMPT = 50


class SqlQuery(BaseModel):
    database: str = Field(description="Name of the database to run the query on")
    sql: str = Field(description="One SQL statement in that database's dialect, with literal values")
    purpose: str = Field(description="What this query answers, in a few words")


class SqlPlan(BaseModel):
    queries: list[SqlQuery] = Field(
        default_factory=list, description=f"1-{MAX_QUERIES} queries; empty if the tables cannot answer the request"
    )
    clarification: str = Field(default="", description="Question for the user when the request is unclear")


PLAN_PROMPT = """You translate a business request into SQL over these databases:

{schemas}

Rules:
- Use only the tables and columns above, in the dialect shown for each database.
- Resolve relative dates ("this quarter", "last 30 days") from today's date. {today}
- Prefer a single query that answers the whole request (CTEs are fine); at most {max_queries}.
- Write SELECT queries. Only if the user explicitly asks to change data, write one
  INSERT/UPDATE/DELETE with a WHERE clause.
- Aggregate where possible, add LIMIT (at most {max_rows} rows) and readable column aliases.
- Format the SQL for reading: one clause per line (SELECT, FROM, JOIN, WHERE, GROUP BY, ...).
- If the tables cannot answer the request, return no queries and a short clarification."""

REPAIR_NOTE = """Your previous query failed:
{sql}
Error: {error}
Return a corrected plan."""

ANSWER_PROMPT = """Write the email reply to the request using ONLY the query results below.
- Every number and name in the reply must come from the results. Never add rows or values.
- If a result is empty or failed, say so plainly and suggest what could be checked.
- Show key rows as a Markdown table. Do not include the SQL; it is appended automatically."""


def _dialect(url: str) -> str:
    return make_url(url).get_backend_name()


async def _schemas() -> dict[str, str]:
    principal = current_principal()
    readable = {name: db for name, db in list_databases().items() if principal.can(f"sql:{name}:read")}
    schemas = {}
    for name, db in readable.items():
        schema = await get_schema(name)
        header = f"### {name} [{_dialect(db.url)}]{' (read-only)' if db.read_only else ''} — {db.description}"
        schemas[name] = f"{header}\n{schema}"
    return schemas


async def _plan(state: AgentState, schemas: dict[str, str], repair: str = "") -> SqlPlan:
    system = PLAN_PROMPT.format(
        schemas="\n\n".join(schemas.values()),
        today=prompts.today_line(),
        max_queries=MAX_QUERIES,
        max_rows=settings.sql_max_rows,
    )
    messages = [SystemMessage(system), HumanMessage(request_text(state) + (f"\n\n{repair}" if repair else ""))]

    async def call(llm):
        return await llm.with_structured_output(SqlPlan).ainvoke(messages)

    with telemetry.span("sql.plan"):
        return await run_chat(
            state.model, call, thinking_budget=settings.planning_thinking_budget, max_output_tokens=8192
        )


async def _execute(query: SqlQuery) -> dict[str, Any]:
    run = execute_write if is_write(query.sql) else select_rows
    with telemetry.span("sql.execute"):
        result = await run(query.database, query.sql)
    telemetry.record_tool(run.__name__, {"database": query.database, "sql": query.sql}, output=result)
    return result


async def run_sql(state: AgentState) -> str:
    schemas = await _schemas()
    if not schemas:
        return "You don't have access to any of the configured SQL databases."

    plan = await _plan(state, schemas)
    if not plan.queries:
        return plan.clarification or "I couldn't map this request to the available database tables."

    executed: list[tuple[SqlQuery, dict[str, Any]]] = []
    for query in plan.queries[:MAX_QUERIES]:
        try:
            result = await _execute(query)
        except PermissionDenied as exc:
            result = {"error": str(exc)}
        except Exception as exc:
            logger.info("SQL failed, asking for a fix: %s", exc)
            telemetry.record_tool("sql", {"database": query.database, "sql": query.sql}, error=str(exc))
            repaired = await _plan(state, schemas, REPAIR_NOTE.format(sql=query.sql, error=exc))
            if not repaired.queries:
                result = {"error": str(exc)}
            else:
                query = repaired.queries[0]
                try:
                    result = await _execute(query)
                except Exception as retry_exc:
                    result = {"error": str(retry_exc)}
        executed.append((query, result))

    results_text = json.dumps(
        [{"purpose": q.purpose, "database": q.database, **_trim(r)} for q, r in executed],
        indent=2,
        default=str,
    )
    messages = [
        SystemMessage(prompts.for_email(ANSWER_PROMPT)),
        HumanMessage(f"{request_text(state)}\n\nQuery results:\n{results_text}"),
    ]

    async def answer(llm):
        return message_text(await llm.ainvoke(messages))

    with telemetry.span("sql.answer"):
        reply = await run_chat(state.model, answer, max_output_tokens=8192)
    return reply.rstrip() + "\n\n" + _sql_appendix(executed)


def _trim(result: dict[str, Any]) -> dict[str, Any]:
    if "rows" in result and len(result["rows"]) > MAX_ROWS_IN_PROMPT:
        return {**result, "rows": result["rows"][:MAX_ROWS_IN_PROMPT], "note": "only the first rows are shown"}
    return result


def _sql_appendix(executed: list[tuple[SqlQuery, dict[str, Any]]]) -> str:
    blocks = []
    for query, result in executed:
        outcome = result.get("error") or (
            f"{result['row_count']} row(s)"
            if "row_count" in result
            else f"{result.get('rows_affected', 0)} row(s) changed"
        )
        blocks.append(f"*{query.purpose} — `{query.database}`, {outcome}*\n\n```sql\n{query.sql.strip()}\n```")
    return "---\n**Query used**\n\n" + "\n\n".join(blocks)
