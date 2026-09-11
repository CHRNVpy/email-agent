"""Documents and chunking for the knowledge base."""

import uuid

from pydantic import BaseModel


class Document(BaseModel):
    """A unit of knowledge from any source (a Drive file, a SQL row, ...)."""

    source: str  # e.g. "drive" or "sql:crm.articles"
    source_id: str  # stable id inside the source
    title: str = ""
    text: str
    url: str = ""
    date: str | None = None  # ISO 8601, enables "last week"-style filtering


class Chunk(BaseModel):
    id: str
    document: Document
    index: int
    total: int
    text: str

    @property
    def payload(self) -> dict:
        payload = self.document.model_dump(exclude={"text"}, exclude_none=True)
        return {**payload, "text": self.text, "chunk_index": self.index, "chunk_total": self.total}


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into ~`size` character windows, preferring paragraph/sentence/word breaks."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            for separator in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(separator)
                if cut > size // 2:
                    end = start + cut + len(separator)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_document(document: Document, size: int, overlap: int) -> list[Chunk]:
    header = f"{document.title}\n\n" if document.title else ""
    pieces = chunk_text(document.text, size, overlap)
    return [
        Chunk(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document.source}:{document.source_id}:{i}")),
            document=document,
            index=i,
            total=len(pieces),
            # The title is repeated in every chunk so that title keywords are searchable.
            text=header + piece,
        )
        for i, piece in enumerate(pieces)
    ]
