"""Resolving workflow sources into text for the stage prompts."""

import asyncio
import logging

from app.google import drive, sheets
from app.rag.retriever import retrieve_context
from app.workflows.models import ResolvedSource, SourceConfig, SourceKind

logger = logging.getLogger(__name__)

MAX_SOURCE_CHARS = 200_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    return text[:MAX_SOURCE_CHARS] + "\n\n[... truncated ...]"


async def resolve(source: SourceConfig) -> ResolvedSource:
    match source.kind:
        case SourceKind.DRIVE_FOLDER:
            files = await drive.load_folders([source.value])
            items = [f"=== {f.title} ===\n{f.text}" for f in files]
            content = "\n\n".join(items) or "(the folder has no readable documents)"
        case SourceKind.GOOGLE_DOC:
            content = await asyncio.to_thread(drive.export_doc_text, drive.document_id(source.value))
            items = [content]
        case SourceKind.GOOGLE_SHEET:
            sheet_id = sheets.spreadsheet_id(source.value)
            content = f"Spreadsheet id: {sheet_id}\n\n" + await asyncio.to_thread(sheets.export_text, sheet_id)
            items = [content]
        case SourceKind.KNOWLEDGE_QUERY:
            content = await retrieve_context(source.value) or "(no matching knowledge-base entries)"
            items = [content]
        case _:
            content = source.value
            items = [line.strip() for line in source.value.splitlines() if line.strip()]
    return ResolvedSource(content=_truncate(content), items=[_truncate(i) for i in items] if source.iterate else [])


async def resolve_all(sources: dict[str, SourceConfig]) -> dict[str, ResolvedSource]:
    resolved = {}
    for name, source in sources.items():
        logger.info("Resolving source %s (%s)", name, source.kind)
        try:
            resolved[name] = await resolve(source)
        except Exception as exc:
            logger.exception("Source %s failed", name)
            resolved[name] = ResolvedSource(content=f"(source could not be loaded: {exc})")
    return resolved
