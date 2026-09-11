import os
import tempfile

# Isolate tests from any local .env before the app settings are created.
_tmp = tempfile.mkdtemp(prefix="email-agent-tests-")
os.environ.update(
    {
        "DATA_DIR": _tmp,
        "APP_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "AGENT_EMAIL": "agent@example.com",
        "ALLOWED_SENDERS": "*@example.com",
        "GEMINI_API_KEYS": "test-key-1,test-key-2",
        "GMAIL_PUBSUB_TOPIC": "",
        "WORKFLOW_SHEET_ID": "",
        "DRIVE_FOLDER_IDS": "",
        "DRIVE_WEBHOOK_URL": "",
        "SQL_DATABASES": "{}",
        "ADMIN_EMAIL": "",
        "VERIFY_PUBSUB_JWT": "false",
        "PERMISSIONS_ENABLED": "true",
        "DEFAULT_USER_ROLE": "guest",
    }
)

import pytest  # noqa: E402

from app.config import settings  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture
async def app_db():
    """A fresh in-memory app database with default roles."""
    from app.db import Base, app_engine, init_app_db
    from app.permissions import service

    await init_app_db()
    await service.seed_defaults()
    yield
    service.invalidate_cache()
    async with app_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
