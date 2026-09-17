"""Specialist agents. Each one is a node of the agent graph.

A specialist declares when it is available (e.g. only if SQL databases are
configured) and whether the acting user may use it. The router only ever sees
specialists that are available, so the system degrades gracefully with a
partial configuration.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from google.genai import types
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from app.agents import grounding, prompts
from app.agents.content import genai_parts, human_content, message_text, request_text
from app.agents.sql_agent import run_sql
from app.agents.state import AgentState
from app.config import settings
from app.db import list_databases
from app.llm import gemini_model, run_chat, run_genai
from app.permissions.policy import Principal
from app.rag.retriever import retrieve_context
from app.telemetry import ToolTraceCallback, record_genai_usage, record_grounding
from app.tools.finance import finance_tools
from app.tools.sheets import SHEETS_TOOLS

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 25


@dataclass(frozen=True)
class Specialist:
    name: str
    description: str
    run: Callable[[AgentState], Awaitable[str]]
    is_available: Callable[[], bool] = lambda: True
    is_allowed: Callable[[Principal], bool] = lambda principal: True


# --- Helpers ------------------------------------------------------------------------


TOOL_REMINDER = (
    "You answered without calling any tool. Do not answer from memory: use the tools to get "
    "the real data or perform the requested action, then answer from their results."
)
UNGROUNDED_REPLY = "I couldn't get real data for this request, so I won't guess. Please rephrase it or try again later."


def _used_tools(messages: list) -> bool:
    return any(isinstance(m, ToolMessage) for m in messages)


def _grounding_evidence(state: AgentState, *sources: str) -> set[float]:
    """Numbers the model legitimately had: the request (incl. earlier specialists' results), today, data."""
    return grounding.evidence_numbers(request_text(state), prompts.today_line(), *sources)


def _tool_outputs(messages: list) -> list[str]:
    return [message_text(m) for m in messages if isinstance(m, ToolMessage)]


async def run_tool_agent(state: AgentState, system_prompt: str, tools: list, *, name: str = "tools") -> str:
    """Run a ReAct tool-calling loop.

    An answer that used no tool at all is not trusted. An answer whose numbers are not in the tool
    results gets one retry with the list of those numbers; if they still don't match, the answer is
    kept (derived values such as growth rates are legitimate here) and the mismatch is recorded.
    """
    messages = [SystemMessage(prompts.for_email(system_prompt)), HumanMessage(human_content(state))]
    config = {"recursion_limit": RECURSION_LIMIT, "callbacks": [ToolTraceCallback()]}

    async def call(llm):
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke({"messages": messages}, config=config)
        if not _used_tools(result["messages"]):
            logger.warning("Specialist answered without tools; retrying with a reminder")
            retry = [*result["messages"], HumanMessage(TOOL_REMINDER)]
            result = await agent.ainvoke({"messages": retry}, config=config)
            if not _used_tools(result["messages"][len(retry) :]):
                return UNGROUNDED_REPLY

        reply = message_text(result["messages"][-1])
        unverified = grounding.unverified_numbers(reply, _grounding_evidence(state, *_tool_outputs(result["messages"])))
        final = unverified
        if unverified:
            logger.warning("%s reply has numbers not in the tool results %s; retrying", name, unverified)
            retry = [*result["messages"], HumanMessage(grounding.feedback(unverified))]
            result = await agent.ainvoke({"messages": retry}, config=config)
            reply = message_text(result["messages"][-1])
            final = grounding.unverified_numbers(reply, _grounding_evidence(state, *_tool_outputs(result["messages"])))
        record_grounding(name, unverified, final, retried=bool(unverified))
        return reply

    return await run_chat(state.model, call)


async def run_plain(
    state: AgentState, system_prompt: str, *, grounding_sources: list[str] | None = None, name: str = "plain"
) -> str:
    """One LLM call. With `grounding_sources`, numbers in the reply must appear in those sources
    (or in the request); otherwise the reply is retried once and the result is recorded."""

    async def call(llm):
        messages = [SystemMessage(prompts.for_email(system_prompt)), HumanMessage(human_content(state))]
        reply = message_text(await llm.ainvoke(messages))
        if grounding_sources is None:
            return reply
        evidence = _grounding_evidence(state, *grounding_sources)
        unverified = grounding.unverified_numbers(reply, evidence)
        final = unverified
        if unverified:
            logger.warning("%s reply has numbers not in its sources %s; retrying", name, unverified)
            retry = [*messages, AIMessage(reply), HumanMessage(grounding.feedback(unverified))]
            reply = message_text(await llm.ainvoke(retry))
            final = grounding.unverified_numbers(reply, evidence)
        record_grounding(name, unverified, final, retried=bool(unverified))
        return reply

    return await run_chat(state.model, call)


def describe(specialists: list[Specialist]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in specialists)


def add_citations(response) -> str:
    """Append numbered source links from Google Search grounding metadata."""
    text = response.text or ""
    metadata = response.candidates[0].grounding_metadata if response.candidates else None
    chunks = (metadata.grounding_chunks or []) if metadata else []
    sources = [(c.web.title or c.web.uri, c.web.uri) for c in chunks if c.web and c.web.uri]
    if not sources:
        return text
    links = "\n".join(f"{i}. [{title}]({uri})" for i, (title, uri) in enumerate(sources, 1))
    return f"{text}\n\n**Sources**\n{links}"


# --- Specialists ----------------------------------------------------------------------


async def assistant(state: AgentState) -> str:
    prompt = prompts.ASSISTANT.format(specialists=describe(available_specialists()))
    if state.results:  # e.g. drafting an email from SQL results: its numbers must come from them
        return await run_plain(state, prompt, grounding_sources=[], name="assistant")
    return await run_plain(state, prompt)


async def sql(state: AgentState) -> str:
    return await run_sql(state)


async def knowledge(state: AgentState) -> str:
    query = state.email.latest_text or state.email.subject
    context = await retrieve_context(query, model=state.model)
    if not context:
        return "I could not find anything relevant in the knowledge base for this request."
    return await run_plain(
        state, prompts.KNOWLEDGE.format(context=context), grounding_sources=[context], name="knowledge"
    )


async def finance(state: AgentState) -> str:
    return await run_tool_agent(state, prompts.FINANCE, finance_tools(), name="finance")


async def sheets(state: AgentState) -> str:
    return await run_tool_agent(state, prompts.SHEETS, SHEETS_TOOLS, name="sheets")


async def web(state: AgentState) -> str:
    config = types.GenerateContentConfig(
        system_instruction=prompts.for_email(prompts.WEB),
        tools=[types.Tool(google_search=types.GoogleSearch()), types.Tool(url_context=types.UrlContext())],
    )

    model = gemini_model(state.model)

    async def call(client):
        return await client.aio.models.generate_content(model=model, contents=genai_parts(state), config=config)

    response = await run_genai(call)
    record_genai_usage(model, response)
    return add_citations(response)


async def files(state: AgentState) -> str:
    if not state.email.attachments:
        return "The email has no attachments to analyse."

    model = gemini_model(state.model)

    async def call(client):
        return await client.aio.models.generate_content(
            model=model,
            contents=genai_parts(state),
            config=types.GenerateContentConfig(system_instruction=prompts.for_email(prompts.FILES)),
        )

    response = await run_genai(call)
    record_genai_usage(model, response)
    return response.text or ""


SPECIALISTS: list[Specialist] = [
    Specialist(
        "assistant",
        "General questions, writing and summarising the email thread; explains what this agent can do.",
        assistant,
    ),
    Specialist(
        "sql",
        "Queries and updates the company SQL databases: listing, filtering, aggregating records.",
        sql,
        is_available=lambda: bool(list_databases()),
        is_allowed=lambda p: any(p.can(f"sql:{name}:read") for name in list_databases()),
    ),
    Specialist(
        "knowledge",
        "Semantic search over the knowledge base (indexed documents, notes, reports) with citations.",
        knowledge,
        is_allowed=lambda p: p.can("knowledge:read"),
    ),
    Specialist(
        "finance",
        "Stock quotes, company fundamentals, financial statements, earnings, transcripts and market news.",
        finance,
        is_available=lambda: settings.finance_enabled,
        is_allowed=lambda p: p.can("finance:read"),
    ),
    Specialist(
        "sheets",
        "Reads and updates Google Sheets: find spreadsheets, read tables, update cells, append rows.",
        sheets,
        is_allowed=lambda p: p.can("sheets:read"),
    ),
    Specialist(
        "web",
        "Up-to-date web research with Google Search and reading web pages from links.",
        web,
        is_allowed=lambda p: p.can("web:search"),
    ),
    Specialist(
        "files",
        "Reads email attachments (PDF, images, documents, audio): extract, summarise, compare.",
        files,
        is_allowed=lambda p: p.can("files:read"),
    ),
]


def available_specialists() -> list[Specialist]:
    return [s for s in SPECIALISTS if s.is_available()]


def get_specialist(name: str) -> Specialist:
    return next(s for s in SPECIALISTS if s.name == name)
