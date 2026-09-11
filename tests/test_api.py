import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.google import drive


@pytest.fixture
def client(monkeypatch, data_dir):
    pushes = []

    async def fake_handle_push(history_id: int):
        pushes.append(history_id)

    monkeypatch.setattr(main, "handle_push", fake_handle_push)
    with TestClient(main.app) as test_client:
        test_client.pushes = pushes
        yield test_client


def envelope(payload: dict) -> dict:
    return {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}


def test_health_lists_available_specialists(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "assistant" in body["specialists"]
    assert "sql" not in body["specialists"]  # no databases configured in tests


def test_gmail_push_schedules_processing(client):
    response = client.post("/gmail/push", json=envelope({"emailAddress": "agent@example.com", "historyId": 42}))
    assert response.json() == {"status": "accepted"}
    assert client.pushes == [42]


def test_gmail_push_for_other_mailbox_is_ignored(client):
    response = client.post("/gmail/push", json=envelope({"emailAddress": "else@example.com", "historyId": 1}))
    assert response.json() == {"status": "ignored"}
    assert client.pushes == []


def test_malformed_push_is_rejected(client):
    assert client.post("/gmail/push", json={"message": {"data": "not-base64!"}}).status_code == 400


def test_drive_push_requires_known_channel(client):
    assert client.post("/drive/push", headers={"X-Goog-Channel-ID": "nope"}).status_code == 403

    drive.watch_state.save({"channel_id": "chan-1"})
    response = client.post("/drive/push", headers={"X-Goog-Channel-ID": "chan-1", "X-Goog-Resource-State": "sync"})
    assert response.json() == {"status": "sync"}


async def test_poll_processes_each_message_once(monkeypatch, data_dir):
    from datetime import UTC, datetime

    from app import pipeline

    queries, handled = [], []
    monkeypatch.setattr(pipeline.gmail, "search_message_ids", lambda q: queries.append(q) or ["m1", "m2"])

    async def fake_process(message_id):
        handled.append(message_id)

    monkeypatch.setattr(pipeline, "process_message", fake_process)
    since = datetime(2026, 9, 1, tzinfo=UTC)
    assert await pipeline.poll_inbox(since) == 2
    assert await pipeline.poll_inbox(since) == 0  # already handled
    assert handled == ["m1", "m2"]
    assert queries[0] == f"in:inbox after:{int(since.timestamp())}"
