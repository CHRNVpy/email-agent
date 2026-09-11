"""Qdrant-backed vector store with Gemini embeddings."""

import logging
from functools import cache

from qdrant_client import AsyncQdrantClient, models

from app.config import settings
from app.llm import embeddings_model, with_key_rotation
from app.rag.documents import Chunk, Document, chunk_document
from app.telemetry import record_usage

logger = logging.getLogger(__name__)

EMBED_BATCH = 50


@cache
def qdrant() -> AsyncQdrantClient:
    if settings.qdrant_url == ":memory:":  # embedded mode, used by tests and offline evals
        return AsyncQdrantClient(location=":memory:")
    return AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def _estimate_tokens(texts: list[str]) -> int:
    return sum(len(t) for t in texts) // 4


async def embed_texts(texts: list[str]) -> list[list[float]]:
    async def call(key: str):
        return await embeddings_model(key).aembed_documents(texts, output_dimensionality=settings.embedding_dimension)

    vectors = await with_key_rotation(call)
    record_usage(settings.embedding_model, _estimate_tokens(texts), 0, estimated=True)
    return vectors


async def embed_query(text: str) -> list[float]:
    async def call(key: str):
        return await embeddings_model(key).aembed_query(text, output_dimensionality=settings.embedding_dimension)

    return await with_key_rotation(call)


async def ensure_collection() -> None:
    client, name = qdrant(), settings.qdrant_collection
    if not await client.collection_exists(name):
        await client.create_collection(
            name,
            vectors_config=models.VectorParams(size=settings.embedding_dimension, distance=models.Distance.COSINE),
        )
        logger.info("Created Qdrant collection %s", name)
    for field, schema in (
        ("source", models.PayloadSchemaType.KEYWORD),
        ("source_id", models.PayloadSchemaType.KEYWORD),
        ("date", models.PayloadSchemaType.DATETIME),
    ):
        await client.create_payload_index(name, field_name=field, field_schema=schema)


def _source_filter(source: str, source_id: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=source)),
            models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id)),
        ]
    )


async def delete_document(source: str, source_id: str) -> None:
    await qdrant().delete(
        settings.qdrant_collection,
        points_selector=models.FilterSelector(filter=_source_filter(source, source_id)),
    )


async def upsert_documents(documents: list[Document], *, replace: bool = True) -> int:
    """Chunk, embed and store documents. With `replace`, stale chunks of each document are removed."""
    chunks: list[Chunk] = []
    for document in documents:
        if replace:
            await delete_document(document.source, document.source_id)
        chunks.extend(
            chunk_document(
                document, settings.rag_chunk_size, settings.rag_chunk_overlap, with_title=settings.rag_title_in_chunks
            )
        )

    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors = await embed_texts([c.text for c in batch])
        await qdrant().upsert(
            settings.qdrant_collection,
            points=[
                models.PointStruct(id=c.id, vector=v, payload=c.payload) for c, v in zip(batch, vectors, strict=True)
            ],
        )
    return len(chunks)


async def _query(vector: list[float], limit: int, threshold: float, start: str | None, end: str | None):
    date_filter = None
    if start or end:
        condition = models.FieldCondition(key="date", range=models.DatetimeRange(gte=start, lte=end))
        date_filter = models.Filter(must=[condition])
    response = await qdrant().query_points(
        settings.qdrant_collection,
        query=vector,
        query_filter=date_filter,
        limit=limit,
        score_threshold=threshold,
        with_payload=True,
    )
    return response.points


async def search(
    query: str,
    *,
    top_k: int | None = None,
    start: str | None = None,
    end: str | None = None,
    score_threshold: float | None = None,
    date_mode: str = "soft",
) -> list[models.ScoredPoint]:
    """Vector search with an optional date window on the document date.

    date_mode="soft" (default) ranks documents inside the window first and then fills
    up with the best documents outside it: a question such as "our ARR target for H1
    2026" mentions a period without asking for documents written in it, so a hard
    filter would drop the answer. "hard" keeps only documents inside the window.
    """
    limit = top_k or settings.rag_top_k
    threshold = settings.rag_score_threshold if score_threshold is None else score_threshold
    vector = await embed_query(query)
    if not (start or end) or date_mode == "none":
        return await _query(vector, limit, threshold, None, None)
    in_window = await _query(vector, limit, threshold, start, end)
    if date_mode == "hard":
        return in_window
    seen = {point.id for point in in_window}
    rest = [p for p in await _query(vector, limit, threshold, None, None) if p.id not in seen]
    return (in_window + rest)[:limit]
