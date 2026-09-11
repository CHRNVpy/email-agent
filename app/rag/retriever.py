"""Retrieval: natural-language date constraints + vector search + context formatting."""

import logging
from datetime import UTC, datetime

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.llm import run_chat
from app.rag import store

logger = logging.getLogger(__name__)

_DATE_PROMPT = ChatPromptTemplate.from_template(
    """Extract a date range from the user's request, if it has one
("last 7 days", "since March", "in Q4 2025", "before 2024"...).
Return ISO 8601 UTC timestamps (YYYY-MM-DDTHH:MM:SSZ). Leave both empty when the
request has no time constraint. Today is {today}.

Request: {query}"""
)


class DateRange(BaseModel):
    start: str | None = Field(default=None, description="Earliest timestamp, ISO 8601")
    end: str | None = Field(default=None, description="Latest timestamp, ISO 8601")


async def parse_date_range(query: str, model: str | None = None) -> DateRange:
    async def call(llm):
        chain = _DATE_PROMPT | llm.with_structured_output(DateRange)
        return await chain.ainvoke({"query": query, "today": datetime.now(UTC).date().isoformat()})

    try:
        return await run_chat(model, call)
    except Exception as exc:
        logger.warning("Date range parsing failed, searching without it: %s", exc)
        return DateRange()


async def retrieve_context(query: str, *, model: str | None = None, use_dates: bool = True) -> str:
    """Search the knowledge base and format hits as numbered, citable context."""
    dates = await parse_date_range(query, model) if use_dates else DateRange()
    if dates.start or dates.end:
        logger.info("Knowledge search restricted to %s .. %s", dates.start, dates.end)
    hits = await store.search(query, start=dates.start, end=dates.end)
    if not hits:
        return ""

    blocks = []
    for i, hit in enumerate(hits, 1):
        p = hit.payload or {}
        meta = " | ".join(x for x in (p.get("title"), p.get("date"), p.get("url")) if x)
        blocks.append(f"[{i}] {meta or p.get('source_id', '')}\n{p.get('text', '')}")
    return "\n\n".join(blocks)
