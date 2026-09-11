"""Executing workflows: stage by stage, with iteration and email pause/resume.

Every stage is a tool-calling agent turn that sees the whole conversation so far,
so later stages can build on earlier answers ("now turn that into a report...").
"""

import logging
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from app import dedupe
from app.agents.content import message_text
from app.agents.prompts import today_line
from app.db import list_databases, utcnow
from app.llm import run_chat
from app.telemetry import ToolTraceCallback
from app.tools.finance import finance_tools
from app.tools.sheets import SHEETS_TOOLS
from app.tools.sql import SQL_TOOLS
from app.tools.workspace import DRIVE_TOOLS, EMAIL_TOOLS
from app.workflows.models import AWAIT_REPLY, RunStatus, StageConfig, WorkflowConfig, WorkflowRun
from app.workflows.sources import resolve_all

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 15
DEFAULT_ACTIONS = ["email", "drive"]


@dataclass(frozen=True)
class ActionGroup:
    tools: list
    guidance: str = ""


ACTION_GROUPS: dict[str, ActionGroup] = {
    "email": ActionGroup(EMAIL_TOOLS, "Send email only when the stage explicitly asks for it and names the recipient."),
    "drive": ActionGroup(DRIVE_TOOLS, "Save files or Google Docs to Drive when the stage asks for it."),
    "sheets": ActionGroup(SHEETS_TOOLS, "Read a worksheet before updating it; use the header map to pick cells."),
    "finance": ActionGroup(finance_tools(), "Use market-data tools for prices, fundamentals and news."),
    "sql": ActionGroup(SQL_TOOLS, "Inspect the schema before querying; use bind parameters."),
}

SYSTEM_PROMPT = """You are executing one stage of a multi-stage workflow on behalf of {user}. {today}
Follow the stage instructions precisely, using the conversation so far as context.
When the stage asks for an action (send an email, save a file, update a sheet), call the
matching tool — do not just describe it. Perform each action exactly once; if a tool reports
that the action was already done, do not repeat it. Finish with a short report of what you
produced or did.

{guidance}"""


def tools_for(config: WorkflowConfig) -> tuple[list, str]:
    tools, guidance = [], []
    for action in config.actions or DEFAULT_ACTIONS:
        group = ACTION_GROUPS.get(action)
        if group is None:
            logger.warning("Workflow '%s': unknown action '%s' ignored", config.name, action)
            continue
        if action == "sql" and not list_databases():
            continue
        tools += group.tools
        guidance.append(f"- {group.guidance}")
    return tools, "\n".join(guidance)


def render(template: str, run: WorkflowRun, item: str | None = None) -> str:
    text = template.replace(AWAIT_REPLY, "").strip()
    iteration_source = run.config.iteration_source
    for name, source in run.sources.items():
        value = item if (item is not None and name == iteration_source) else source.content
        text = text.replace("{" + name + "}", value)
    return text


def uses_iteration(stage: StageConfig, run: WorkflowRun) -> bool:
    name = run.config.iteration_source
    source = run.sources.get(name) if name else None
    return bool(source and source.items and name in stage.placeholders)


async def execute_stage(run: WorkflowRun, prompt: str, scope: str) -> str:
    tools, guidance = tools_for(run.config)
    system = SYSTEM_PROMPT.format(user=run.user_email, today=today_line(), guidance=guidance)
    messages = [SystemMessage(system)]
    for turn in run.history:
        messages.append(AIMessage(turn["content"]) if turn["role"] == "assistant" else HumanMessage(turn["content"]))
    messages.append(HumanMessage(prompt))

    async def call(llm):
        agent = create_react_agent(llm, tools)
        config = {"recursion_limit": RECURSION_LIMIT, "callbacks": [ToolTraceCallback()]}
        result = await agent.ainvoke({"messages": messages}, config=config)
        return message_text(result["messages"][-1])

    with dedupe.scope(scope):
        return await run_chat(run.model, call)


async def _advance(run: WorkflowRun, reply: str | None = None) -> WorkflowRun:
    run.status = RunStatus.RUNNING
    try:
        for stage in run.config.stages[run.completed_stages :]:
            if stage.awaits_reply and reply is None:
                run.pending_message = render(stage.template, run)
                run.status = RunStatus.AWAITING_REPLY
                logger.info("Run %s waits for a reply at stage %d", run.id, stage.number)
                return run

            if uses_iteration(stage, run):
                items = run.sources[run.config.iteration_source].items
                outputs = []
                for index, item in enumerate(items, 1):
                    logger.info("Run %s stage %d item %d/%d", run.id, stage.number, index, len(items))
                    prompt = render(stage.template, run, item)
                    outputs.append(await execute_stage(run, prompt, f"wf:{run.id}:{stage.number}:{index}"))
                prompt = f"{render(stage.template, run, '<each item>')}\n\n(Run once per item: {len(items)} items)"
                answer = "\n\n".join(f"### Item {i}\n{out}" for i, out in enumerate(outputs, 1))
            else:
                prompt = render(stage.template, run)
                if reply is not None:
                    prompt += f"\n\nThe user replied:\n{reply}"
                answer = await execute_stage(run, prompt, f"wf:{run.id}:{stage.number}")

            reply = None
            run.add_turn(prompt, answer)
            run.completed_stages += 1
            run.updated_at = utcnow()
        run.status = RunStatus.COMPLETED
    except Exception as exc:
        logger.exception("Run %s failed", run.id)
        run.status, run.error = RunStatus.FAILED, str(exc)
    run.pending_message = ""
    run.updated_at = utcnow()
    return run


async def start_workflow(
    config: WorkflowConfig, *, user_email: str, thread_id: str, request: str, model: str | None = None
) -> WorkflowRun:
    run = WorkflowRun(
        workflow=config.name,
        user_email=user_email,
        thread_id=thread_id,
        model=model or config.model,
        config=config,
    )
    logger.info("Starting workflow '%s' (run %s) for %s", config.name, run.id, user_email)
    run.sources = await resolve_all(config.sources)
    if request.strip():
        run.history.append({"role": "user", "content": f"The workflow was triggered by this email:\n{request}"})
    return await _advance(run)


async def resume_workflow(run: WorkflowRun, reply: str) -> WorkflowRun:
    logger.info("Resuming run %s with the user's reply", run.id)
    return await _advance(run, reply=reply)


def final_message(run: WorkflowRun) -> str:
    match run.status:
        case RunStatus.AWAITING_REPLY:
            return run.pending_message
        case RunStatus.FAILED:
            return f"The workflow **{run.workflow}** failed: {run.error}"
        case _:
            return run.last_answer or f"The workflow **{run.workflow}** finished."
