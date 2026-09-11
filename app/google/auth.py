"""OAuth credentials of the agent's Google account (Gmail, Drive, Docs, Sheets)."""

import logging
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

_lock = threading.Lock()
_credentials: Credentials | None = None


class GoogleAuthError(RuntimeError):
    pass


def get_credentials() -> Credentials:
    """Return cached credentials, refreshing (and persisting) the access token when needed."""
    global _credentials
    with _lock:
        if _credentials is None:
            token_file = settings.google_token_file
            if not token_file.exists():
                raise GoogleAuthError(
                    f"{token_file} not found. Run `python -m app.cli auth` to authorise the agent account."
                )
            _credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)

        if not _credentials.valid:
            if not _credentials.refresh_token:
                raise GoogleAuthError("Stored token cannot be refreshed; re-run `python -m app.cli auth`.")
            _credentials.refresh(Request())
            settings.google_token_file.write_text(_credentials.to_json())
            logger.info("Refreshed Google access token")
        return _credentials


def run_oauth_flow() -> Credentials:
    """Interactive consent flow (CLI only). Stores the token for the server to reuse."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secrets_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False)
    settings.google_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_file.write_text(creds.to_json())
    return creds
