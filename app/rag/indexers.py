"""Loading sources into the knowledge base: SQL tables and Google Drive folders."""

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import MetaData, Table, select

from app.config import RagSqlTable, settings
from app.db import data_engine
from app.google import drive
from app.rag import store
from app.rag.documents import Document

logger = logging.getLogger(__name__)

DRIVE_SOURCE = "drive"
SQL_BATCH = 200

_drive_sync_lock = asyncio.Lock()


# --- SQL ----------------------------------------------------------------------------


def _iso(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    return str(value) if value else None


def row_to_document(spec: RagSqlTable, row: dict) -> Document:
    text = "\n\n".join(str(row[c]) for c in spec.text_columns if row.get(c) not in (None, ""))
    return Document(
        source=f"sql:{spec.database}.{spec.table}",
        source_id=str(row[spec.id_column]),
        title=str(row.get(spec.title_column) or "") if spec.title_column else "",
        url=str(row.get(spec.url_column) or "") if spec.url_column else "",
        date=_iso(row.get(spec.date_column)) if spec.date_column else None,
        text=text,
    )


async def index_sql_table(spec: RagSqlTable) -> int:
    engine = data_engine(spec.database)
    async with engine.connect() as conn:
        table = await conn.run_sync(lambda sync: Table(spec.table, MetaData(), autoload_with=sync))
        columns = {spec.id_column, *spec.text_columns}
        columns |= {c for c in (spec.title_column, spec.date_column, spec.url_column) if c}
        query = select(*(table.c[c] for c in columns)).order_by(table.c[spec.id_column])

        total, offset = 0, 0
        while True:
            rows = (await conn.execute(query.limit(SQL_BATCH).offset(offset))).mappings().all()
            if not rows:
                break
            documents = [row_to_document(spec, dict(r)) for r in rows]
            total += await store.upsert_documents([d for d in documents if d.text])
            offset += len(rows)
            logger.info("%s.%s: %d rows indexed", spec.database, spec.table, offset)
    return total


async def index_sql_tables() -> int:
    await store.ensure_collection()
    return sum([await index_sql_table(spec) for spec in settings.rag_sql_tables])


# --- Google Drive -------------------------------------------------------------------


def file_to_document(file: drive.DriveFile) -> Document:
    return Document(
        source=DRIVE_SOURCE,
        source_id=file.id,
        title=file.title,
        url=file.url,
        date=file.modified_time,
        text=file.text,
    )


async def index_drive_folders(folder_ids: list[str] | None = None) -> int:
    await store.ensure_collection()
    files = await drive.load_folders(folder_ids or settings.drive_folder_ids)
    return await store.upsert_documents([file_to_document(f) for f in files if f.text.strip()])


async def sync_drive_changes() -> dict:
    """Apply Drive changes since the stored page token (called from the Drive webhook)."""
    async with _drive_sync_lock:  # notifications arrive in bursts; process them one at a time
        return await _sync_drive_changes()


async def _sync_drive_changes() -> dict:
    state = drive.watch_state.load()
    page_token = state.get("page_token") or await asyncio.to_thread(drive.start_page_token)
    changes, new_token = await asyncio.to_thread(drive.list_changes, page_token)

    tracked = await asyncio.to_thread(drive.folder_tree, settings.drive_folder_ids)
    reindex: dict[str, dict] = {}
    removed: set[str] = set()
    for change in changes:
        meta = change.get("file") or {}
        file_id = change.get("fileId") or meta.get("id")
        if change.get("removed") or meta.get("trashed"):
            removed.add(file_id)
            continue
        if not tracked.intersection(meta.get("parents") or []):
            continue
        if meta.get("mimeType") == drive.SHORTCUT:
            meta = await asyncio.to_thread(drive.resolve_shortcut, meta)
        if meta and meta.get("mimeType") in drive.SUPPORTED:
            reindex[meta["id"]] = meta

    for file_id in removed:
        await store.delete_document(DRIVE_SOURCE, file_id)
    documents = []
    for meta in reindex.values():
        loaded = await drive.load_content(meta)
        if loaded and loaded.text.strip():
            documents.append(file_to_document(loaded))
    if documents:
        await store.upsert_documents(documents)

    if new_token:
        drive.watch_state.update(page_token=new_token)
    stats = {"changes": len(changes), "reindexed": len(documents), "removed": len(removed)}
    logger.info("Drive sync: %s", stats)
    return stats
