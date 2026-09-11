"""From a Gmail push notification to a reply in the same thread.

push(historyId) or poll ─► new message ids ─► sender allowed? ─► load email + thread
    ─► reply to a paused workflow?  ─► resume it
    ─► subject matches a workflow?  ─► start it
    ─► otherwise                    ─► multi-agent graph
─► send the answer as an in-thread reply
"""

import asyncio
import logging
from datetime import datetime
from email.utils import parseaddr
from fnmatch import fnmatchcase

from app import dedupe, telemetry
from app.agents.graph import run_agent
from app.config import settings
from app.google import gmail
from app.messages import IncomingEmail, parse_model_flag
from app.permissions.policy import Principal
from app.permissions.service import is_registered, resolve_principal, set_principal
from app.workflows import engine, store
from app.workflows.sheet import load_workflows, match_subject

logger = logging.getLogger(__name__)

_fetch_lock = asyncio.Lock()


async def is_authorized(sender: str) -> bool:
    """Senders must match ALLOWED_SENDERS (wildcards allowed) or be registered users."""
    if any(fnmatchcase(sender, pattern.lower()) for pattern in settings.allowed_senders):
        return True
    return settings.permissions_enabled and await is_registered(sender)


async def handle_push(history_id: int) -> None:
    async with _fetch_lock:  # history ids must be consumed in order
        message_ids = await asyncio.to_thread(gmail.list_new_message_ids, history_id)
    await process_messages(message_ids)


async def poll_inbox(since: datetime) -> int:
    """Polling mode (no Pub/Sub, no public URL): handle inbox messages received after `since`."""
    query = f"in:inbox after:{int(since.timestamp())}"
    message_ids = await asyncio.to_thread(gmail.search_message_ids, query)
    return await process_messages(message_ids)


async def process_messages(message_ids: list[str]) -> int:
    """Process each message once, even if it is reported again by a later push or poll."""
    processed = 0
    for message_id in message_ids:
        if not dedupe.claim(dedupe.make_key("incoming", message_id)):
            continue
        processed += 1
        try:
            await process_message(message_id)
        except Exception:
            logger.exception("Failed to process message %s", message_id)
        finally:
            dedupe.mark_done(dedupe.make_key("incoming", message_id))
    return processed


async def process_message(message_id: str) -> None:
    sender = parseaddr(await asyncio.to_thread(gmail.get_sender, message_id))[1].lower()
    if sender == settings.agent_email.lower():
        return
    if not await is_authorized(sender):
        logger.info("Ignoring message from unauthorised sender %s", sender)
        return

    email = await asyncio.to_thread(gmail.load_email, message_id)
    if email is None:
        return
    subject, model = parse_model_flag(email.subject)
    email = email.model_copy(update={"subject": subject})

    principal = await resolve_principal(sender)
    set_principal(principal)
    logger.info("Processing %s from %s (model=%s)", message_id, sender, model or settings.default_model)

    with dedupe.scope(f"msg:{email.id}"), telemetry.trace() as trace:
        reply = await answer(email, principal, model)
    logger.info("Answered %s: %s", message_id, trace.summary())
    if reply:
        await asyncio.to_thread(gmail.send_reply, email, reply)


async def answer(email: IncomingEmail, principal: Principal, model: str | None = None) -> str:
    """Produce the reply text for an email (also used by the `ask` CLI command)."""
    if settings.workflows_enabled:
        reply = await _workflow_reply(email, principal, model)
        if reply is not None:
            return reply
    return await run_agent(email, model)


async def _workflow_reply(email: IncomingEmail, principal: Principal, model: str | None) -> str | None:
    paused = await store.find_awaiting_reply(email.thread_id)
    if paused and paused.user_email == principal.email:
        run = await engine.resume_workflow(paused, email.latest_text)
        await store.save(run)
        return engine.final_message(run)

    try:
        workflows = await asyncio.to_thread(load_workflows)
    except Exception:
        logger.exception("Could not load the workflow sheet; handling the email with the agent")
        return None
    workflow = match_subject(email.subject, workflows)
    if workflow is None:
        return None
    if not principal.can("workflows:run"):
        return f"You don't have permission to run the **{workflow.name}** workflow."

    run = await engine.start_workflow(
        workflow, user_email=principal.email, thread_id=email.thread_id, request=email.latest_text, model=model
    )
    await store.save(run)
    return engine.final_message(run)
