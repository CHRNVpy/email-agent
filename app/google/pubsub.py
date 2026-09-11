"""Authentication of Google Cloud Pub/Sub push requests (OIDC JWT)."""

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import settings


async def verify_pubsub_jwt(request: Request) -> dict:
    """FastAPI dependency: validate the bearer token Pub/Sub attaches to push requests."""
    if not settings.verify_pubsub_jwt:
        return {}

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        claims = id_token.verify_oauth2_token(
            auth.removeprefix("Bearer "), google_requests.Request(), audience=settings.pubsub_audience
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid token") from exc

    if settings.pubsub_service_account and claims.get("email") != settings.pubsub_service_account:
        raise HTTPException(status_code=403, detail="Unexpected token issuer")
    return claims
