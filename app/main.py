"""FastAPI application: Gmail / Drive push endpoints and health check."""

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request

from app import __version__
from app.agents.specialists import available_specialists
from app.config import settings
from app.db import init_app_db
from app.google import drive
from app.google.pubsub import verify_pubsub_jwt
from app.permissions.service import create_user, is_registered, seed_defaults
from app.pipeline import handle_push
from app.rag.indexers import sync_drive_changes
from app.scheduler import create_scheduler

logger = logging.getLogger(__name__)


async def bootstrap() -> None:
    await init_app_db()
    await seed_defaults()
    if settings.admin_email and not await is_registered(settings.admin_email):
        await create_user(settings.admin_email, roles=["admin"])
        logger.info("Created admin user %s", settings.admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.agent_email:
        logger.warning("AGENT_EMAIL is not set; Gmail notifications will be ignored")
    await bootstrap()
    scheduler = create_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Email Agent", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "specialists": [s.name for s in available_specialists()],
        "workflows": settings.workflows_enabled,
        "drive_sync": settings.drive_sync_enabled,
    }


@app.post("/gmail/push", dependencies=[Depends(verify_pubsub_jwt)])
async def gmail_push(request: Request, background: BackgroundTasks) -> dict:
    """Pub/Sub push endpoint for Gmail `users.watch` notifications."""
    envelope = await request.json()
    try:
        notification = json.loads(base64.b64decode(envelope["message"]["data"]))
        history_id = int(notification["historyId"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub message") from exc

    if notification.get("emailAddress", "").lower() != settings.agent_email.lower():
        return {"status": "ignored"}
    # Acknowledge immediately; Pub/Sub would redeliver if we took longer than the ack deadline.
    background.add_task(handle_push, history_id)
    return {"status": "accepted"}


@app.post("/drive/push")
async def drive_push(request: Request, background: BackgroundTasks) -> dict:
    """Drive `changes.watch` webhook: re-index changed documents in the tracked folders."""
    state = drive.watch_state.load()
    headers = request.headers
    if not state or headers.get("X-Goog-Channel-ID") != state.get("channel_id"):
        raise HTTPException(status_code=403, detail="Unknown channel")
    if settings.drive_channel_token and not secrets.compare_digest(
        headers.get("X-Goog-Channel-Token", ""), settings.drive_channel_token
    ):
        raise HTTPException(status_code=403, detail="Invalid channel token")

    if headers.get("X-Goog-Resource-State") == "sync":  # handshake sent when a channel opens
        return {"status": "sync"}
    background.add_task(sync_drive_changes)
    return {"status": "accepted"}
