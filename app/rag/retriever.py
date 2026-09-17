"""Retrieval: natural-language date constraints + vector search + context formatting."""

import calendar
import logging
import re
from datetime import UTC, date, datetime

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from qdrant_client.models import ScoredPoint

from app.llm import run_chat
from app.rag import store
from app.telemetry import span

logger = logging.getLogger(__name__)

_DATE_PROMPT = ChatPromptTemplate.from_template(
    """Extract a date range from the user's request, if it has one
("last 7 days", "since March", "in Q4 2025", "before 2024"...).
Return ISO 8601 UTC timestamps (YYYY-MM-DDTHH:MM:SSZ). Leave both empty when the
request has no time constraint. Today is {today}.

Request: {query}"""
)


_MONTH_NAMES = {name.lower() for name in (*calendar.month_name[1:], *calendar.month_abbr[1:], "Sept")}
_MONTHS = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
_TEMPORAL_CUE = re.compile(
    rf"\b(?:(?:19|20)\d{{2}}|q[1-4]|h[12]|ytd|{_MONTHS}"
    r"|today|yesterday|tomorrow|tonight|days?|weeks?|months?|quarters?|years?|weekly|monthly|daily"
    r"|recent|recently|since|ago|last|past|previous|next)\b",
    re.IGNORECASE,
)


def has_temporal_cue(query: str) -> bool:
    """Cheap pre-check: only queries that mention time go to the LLM date parser."""
    return bool(_TEMPORAL_CUE.search(query))


class DateRange(BaseModel):
    start: str | None = Field(default=None, description="Earliest timestamp, ISO 8601")
    end: str | None = Field(default=None, description="Latest timestamp, ISO 8601")


async def parse_date_range(query: str, model: str | None = None, today: date | None = None) -> DateRange:
    if not has_temporal_cue(query):
        return DateRange()
    today = today or datetime.now(UTC).date()

    async def call(llm):
        chain = _DATE_PROMPT | llm.with_structured_output(DateRange)
        return await chain.ainvoke({"query": query, "today": today.isoformat()})

    try:
        with span("rag.parse_dates"):
            return await run_chat(model, call, thinking_budget=0, max_output_tokens=4096)
    except Exception as exc:
        logger.warning("Date range parsing failed, searching without it: %s", exc)
        return DateRange()


async def search(
    query: str, *, model: str | None = None, use_dates: bool = True, today: date | None = None
) -> tuple[DateRange, list[ScoredPoint]]:
    dates = await parse_date_range(query, model, today) if use_dates else DateRange()
    if dates.start or dates.end:
        logger.info("Knowledge search restricted to %s .. %s", dates.start, dates.end)
    with span("rag.search"):
        hits = await store.search(query, start=dates.start, end=dates.end, date_mode="soft")
    return dates, hits


def format_context(hits: list[ScoredPoint]) -> str:
    """Hits as numbered, citable context."""
    blocks = []
    for i, hit in enumerate(hits, 1):
        p = hit.payload or {}
        meta = " | ".join(x for x in (p.get("title"), p.get("date"), p.get("url")) if x)
        blocks.append(f"[{i}] {meta or p.get('source_id', '')}\n{p.get('text', '')}")
    return "\n\n".join(blocks)


async def retrieve_context(query: str, *, model: str | None = None, use_dates: bool = True) -> str:
    _, hits = await search(query, model=model, use_dates=use_dates)
    return format_context(hits)
