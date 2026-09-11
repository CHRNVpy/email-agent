"""Background jobs keeping Gmail and Drive push channels alive."""

import logging
import time
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.google import drive, gmail

logger = logging.getLogger(__name__)

RENEW_BEFORE_MS = 48 * 3600 * 1000


def renew_gmail_watch() -> None:
    expiration = gmail.watch_state.load().get("expiration_ms", 0)
    if expiration - time.time() * 1000 < RENEW_BEFORE_MS:
        gmail.renew_watch()


def renew_drive_watch() -> None:
    if drive.watch_needs_renewal():
        drive.renew_watch()
        logger.info("Drive changes channel renewed")


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    now = datetime.now(UTC)
    if settings.gmail_pubsub_topic:
        scheduler.add_job(renew_gmail_watch, IntervalTrigger(hours=12), next_run_time=now, id="gmail-watch")
    if settings.drive_sync_enabled:
        scheduler.add_job(renew_drive_watch, IntervalTrigger(hours=6), next_run_time=now, id="drive-watch")
    return scheduler
