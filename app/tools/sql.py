"""SQL tools over the databases declared in `SQL_DATABASES`."""

import json
import re
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import inspect, text

from app.config import settings
from app.db import data_engine, get_database, list_databases
from app.permissions.service import require

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_READ_ONLY_START = ("select", "with", "show", "describe", "desc", "explain")
_WRITE_KEYWORDS = re.compile(r"\b(insert|update|delete|merge|replace|drop|alter|create|truncate|grant)\b", re.I)


class UnsafeQuery(ValueError):
    pass


def _normalise(query: str) -> str:
    statement = _COMMENTS.sub(" ", query).strip().rstrip(";").strip()
    if ";" in statement:
        raise UnsafeQuery("Only a single SQL statement is allowed.")
    return statement


def check_read_only(query: str) -> str:
    statement = _normalise(query)
    if not statement.lower().startswith(_READ_ONLY_START):
        raise UnsafeQuery("Only SELECT / WITH / SHOW / DESCRIBE / EXPLAIN queries are allowed here.")
    if statement.lower().startswith("with") and _WRITE_KEYWORDS.search(statement):
        raise UnsafeQuery("Data-modifying CTEs are not allowed in read-only queries.")
    return statement


def check_write(query: str) -> str:
    statement = _normalise(query)
    if not statement.lower().startswith(("insert", "update", "delete")):
        raise UnsafeQuery("Only INSERT / UPDATE / DELETE statements are allowed.")
    if re.match(r"(?is)^(update|delete)\b", statement) and not re.search(r"(?i)\bwhere\b", statement):
        raise UnsafeQuery("UPDATE / DELETE without a WHERE clause is not allowed.")
    return statement


def _params(params: Any) -> dict:
    if params in (None, "", {}):
        return {}
    if isinstance(params, str):
        params = json.loads(params)
    if not isinstance(params, dict):
        raise ValueError('params must be a JSON object, e.g. {"id": 42}')
    return params


def describe_schema_sync(conn) -> str:
    inspector = inspect(conn)
    lines = []
    for table in inspector.get_table_names():
        lines.append(f"Table {table}:")
        foreign = {
            col: f"{fk['referred_table']}.{ref}"
            for fk in inspector.get_foreign_keys(table)
            for col, ref in zip(fk["constrained_columns"], fk["referred_columns"], strict=False)
        }
        primary = set(inspector.get_pk_constraint(table).get("constrained_columns") or [])
        for column in inspector.get_columns(table):
            notes = [str(column["type"]).lower()]
            if column["name"] in primary:
                notes.append("primary key")
            if not column.get("nullable", True):
                notes.append("not null")
            if column["name"] in foreign:
                notes.append(f"-> {foreign[column['name']]}")
            lines.append(f"  - {column['name']}: {', '.join(notes)}")
    return "\n".join(lines)


@tool
def list_sql_databases() -> str:
    """List the SQL databases you can query, with their descriptions."""
    return json.dumps(
        {name: {"description": db.description, "read_only": db.read_only} for name, db in list_databases().items()},
        indent=2,
    )


@tool
async def get_sql_schema(database: str) -> str:
    """Return tables, columns, types and keys of a database. Always call before writing SQL."""
    require(f"sql:{database}:read")
    async with data_engine(database).connect() as conn:
        return await conn.run_sync(describe_schema_sync)


@tool
async def run_sql_select(database: str, query: str, params: dict | None = None) -> str:
    """Run a read-only SQL query (SELECT/WITH/SHOW/DESCRIBE) with named bind parameters.

    Args:
        database: Database name from list_sql_databases.
        query: One SQL statement using :name placeholders, e.g. "SELECT * FROM orders WHERE id = :id".
        params: JSON object with values for the placeholders, e.g. {"id": 42}.
    """
    require(f"sql:{database}:read")
    statement = check_read_only(query)
    async with data_engine(database).connect() as conn:
        result = await conn.execute(text(statement), _params(params))
        rows = [dict(r) for r in result.mappings().fetchmany(settings.sql_max_rows + 1)]
    truncated = len(rows) > settings.sql_max_rows
    payload = {"row_count": min(len(rows), settings.sql_max_rows), "rows": rows[: settings.sql_max_rows]}
    if truncated:
        payload["note"] = f"Result truncated to {settings.sql_max_rows} rows; refine the query."
    return json.dumps(payload, indent=2, default=str)


@tool
async def run_sql_write(database: str, query: str, params: dict | None = None) -> str:
    """Run a single INSERT/UPDATE/DELETE statement with named bind parameters.

    Only call this after the user explicitly asked for the change.

    Args:
        database: Database name from list_sql_databases.
        query: One statement using :name placeholders. UPDATE/DELETE must have a WHERE clause.
        params: JSON object with values for the placeholders.
    """
    require(f"sql:{database}:write")
    if get_database(database).read_only:
        raise PermissionError(f"Database '{database}' is configured as read-only.")
    statement = check_write(query)
    async with data_engine(database).begin() as conn:
        result = await conn.execute(text(statement), _params(params))
    return json.dumps({"rows_affected": result.rowcount})


SQL_TOOLS = [list_sql_databases, get_sql_schema, run_sql_select, run_sql_write]
