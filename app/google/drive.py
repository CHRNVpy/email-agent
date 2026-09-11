"""Google Drive: folder traversal, content export, uploads and change notifications."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Iterable

from google.genai import types
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from pydantic import BaseModel, Field

from app.config import settings
from app.google import sheets
from app.google.auth import get_credentials
from app.llm import run_genai
from app.state import JsonState

logger = logging.getLogger(__name__)

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SHORTCUT = "application/vnd.google-apps.shortcut"
PDF = "application/pdf"
SUPPORTED = {DOC, SHEET, PDF}

PDF_EXTRACTION_MODEL = "gemini-2.5-flash"
_FILE_FIELDS = "id, name, mimeType, webViewLink, modifiedTime, parents, shortcutDetails"
_FOLDER_URL = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_DOC_URL = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")

watch_state = JsonState("drive_watch.json")


class DriveFile(BaseModel):
    id: str
    title: str
    mime_type: str
    url: str = ""
    modified_time: str | None = None
    parents: list[str] = Field(default_factory=list)
    text: str = ""


def drive_service():
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)


def folder_id(url_or_id: str) -> str:
    match = _FOLDER_URL.search(url_or_id)
    return match.group(1) if match else url_or_id.strip()


def document_id(url_or_id: str) -> str:
    match = _DOC_URL.search(url_or_id)
    return match.group(1) if match else url_or_id.strip()


# --- Listing ------------------------------------------------------------------


def _list(query: str, fields: str = _FILE_FIELDS) -> list[dict]:
    service, items, token = drive_service(), [], None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields=f"nextPageToken, files({fields})",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        items.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            return items


def folder_tree(root_ids: Iterable[str]) -> set[str]:
    """All folder ids below (and including) the given roots."""
    seen: set[str] = set()
    queue = [folder_id(f) for f in root_ids if f]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        children = _list(f"'{current}' in parents and trashed=false and mimeType='{FOLDER}'", "id")
        queue.extend(child["id"] for child in children)
    return seen


def get_file(file_id: str) -> dict:
    return drive_service().files().get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True).execute()


def resolve_shortcut(meta: dict) -> dict | None:
    """Follow a Drive shortcut to its target if the target type is supported."""
    details = meta.get("shortcutDetails") or {}
    if details.get("targetMimeType") not in SUPPORTED:
        return None
    try:
        return get_file(details["targetId"])
    except Exception as exc:
        logger.warning("Cannot resolve shortcut %s: %s", meta.get("name"), exc)
        return None


def list_supported_files(folder_ids: Iterable[str]) -> list[dict]:
    mime_filter = " or ".join(f"mimeType='{m}'" for m in (*SUPPORTED, SHORTCUT))
    files: list[dict] = []
    for fid in folder_ids:
        for meta in _list(f"'{fid}' in parents and trashed=false and ({mime_filter})"):
            if meta["mimeType"] == SHORTCUT:
                meta = resolve_shortcut(meta)
            if meta:
                files.append(meta)
    return files


# --- Content ------------------------------------------------------------------


def export_doc_text(doc_id: str) -> str:
    data = drive_service().files().export_media(fileId=doc_id, mimeType="text/plain").execute()
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


def download_bytes(file_id: str) -> bytes:
    return drive_service().files().get_media(fileId=file_id).execute()


async def extract_pdf_text(pdf: bytes, title: str) -> str:
    """Use Gemini's native PDF understanding to get text (tables and scans included)."""
    prompt = "Extract ALL text from this PDF. Keep the structure, render tables clearly, do not summarise."

    async def call(client):
        return await client.aio.models.generate_content(
            model=PDF_EXTRACTION_MODEL,
            contents=[types.Part.from_bytes(data=pdf, mime_type=PDF), prompt],
        )

    try:
        response = await run_genai(call)
        return response.text or ""
    except Exception as exc:
        logger.error("PDF extraction failed for %s: %s", title, exc)
        return f"[PDF '{title}': text extraction failed]"


async def load_content(meta: dict) -> DriveFile | None:
    mime = meta.get("mimeType", "")
    if mime == DOC:
        text = await asyncio.to_thread(export_doc_text, meta["id"])
    elif mime == SHEET:
        text = await asyncio.to_thread(sheets.export_text, meta["id"])
    elif mime == PDF:
        text = await extract_pdf_text(await asyncio.to_thread(download_bytes, meta["id"]), meta["name"])
    else:
        return None
    return DriveFile(
        id=meta["id"],
        title=meta.get("name", ""),
        mime_type=mime,
        url=meta.get("webViewLink", ""),
        modified_time=meta.get("modifiedTime"),
        parents=meta.get("parents") or [],
        text=text,
    )


async def load_folders(root_ids: Iterable[str]) -> list[DriveFile]:
    """Load every supported file (Docs, Sheets, PDFs, shortcuts) under the given folders."""
    folders = await asyncio.to_thread(folder_tree, root_ids)
    metas = await asyncio.to_thread(list_supported_files, folders)
    logger.info("Found %d files in %d folders", len(metas), len(folders))
    files = []
    for meta in metas:
        try:
            loaded = await load_content(meta)
        except Exception as exc:
            logger.warning("Skipping %s: %s", meta.get("name"), exc)
            continue
        if loaded:
            files.append(loaded)
    return files


# --- Writing ------------------------------------------------------------------


def upload_text(folder: str, filename: str, content: str) -> str:
    mime = {"html": "text/html", "md": "text/markdown", "json": "application/json", "csv": "text/csv"}
    media = MediaInMemoryUpload(content.encode(), mimetype=mime.get(filename.rsplit(".", 1)[-1].lower(), "text/plain"))
    created = (
        drive_service()
        .files()
        .create(
            body={"name": filename, "parents": [folder_id(folder)]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created.get("webViewLink", "")


def create_doc(folder: str, title: str, content: str) -> str:
    created = (
        drive_service()
        .files()
        .create(
            body={"name": title, "mimeType": DOC, "parents": [folder_id(folder)]},
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    if content:
        docs = build("docs", "v1", credentials=get_credentials(), cache_discovery=False)
        docs.documents().batchUpdate(
            documentId=created["id"],
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute()
    return created.get("webViewLink", "")


# --- Change notifications -----------------------------------------------------


def start_page_token() -> str:
    return str(drive_service().changes().getStartPageToken(supportsAllDrives=True).execute()["startPageToken"])


def renew_watch() -> dict:
    """Open a new `changes.watch` channel pointed at DRIVE_WEBHOOK_URL."""
    state = watch_state.load()
    body = {"id": str(uuid.uuid4()), "type": "web_hook", "address": settings.drive_webhook_url}
    if settings.drive_channel_token:
        body["token"] = settings.drive_channel_token
    page_token = state.get("page_token") or start_page_token()
    response = (
        drive_service()
        .changes()
        .watch(pageToken=page_token, body=body, supportsAllDrives=True, includeItemsFromAllDrives=True)
        .execute()
    )
    return watch_state.update(
        channel_id=body["id"],
        resource_id=response.get("resourceId"),
        expiration_ms=int(response.get("expiration", 0)),
        page_token=page_token,
        webhook_url=settings.drive_webhook_url,
    )


def watch_needs_renewal(within_seconds: int = 24 * 3600) -> bool:
    state = watch_state.load()
    if not state or state.get("webhook_url") != settings.drive_webhook_url:
        return True
    return state.get("expiration_ms", 0) - time.time() * 1000 < within_seconds * 1000


def list_changes(page_token: str) -> tuple[list[dict], str | None]:
    """All changes since `page_token`; returns them with the next start token."""
    service, changes, token, new_start = drive_service(), [], page_token, None
    fields = f"nextPageToken, newStartPageToken, changes(fileId, removed, file({_FILE_FIELDS}, trashed))"
    while token:
        response = (
            service.changes()
            .list(
                pageToken=token,
                fields=fields,
                includeRemoved=True,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        changes.extend(response.get("changes", []))
        new_start = response.get("newStartPageToken") or new_start
        token = response.get("nextPageToken")
    return changes, new_start
