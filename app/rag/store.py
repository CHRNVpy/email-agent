"""Qdrant-backed vector store with Gemini embeddings."""

import logging
from functools import cache

from qdrant_client import AsyncQdrantClient, models

from app.config import settings
from app.llm import embeddings_model, with_key_rotation
from app.rag.documents import Chunk, Document, chunk_document

logger = logging.getLogger(__name__)

EMBED_BATCH = 50


@cache
def qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    async def call(key: str):
        return await embeddings_model(key).aembed_documents(texts, output_dimensionality=settings.embedding_dimension)

    return await with_key_rotation(call)


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
        chunks.extend(chunk_document(document, settings.rag_chunk_size, settings.rag_chunk_overlap))

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


async def search(
    query: str,
    *,
    top_k: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[models.ScoredPoint]:
    conditions = []
    if start or end:
        conditions.append(models.FieldCondition(key="date", range=models.DatetimeRange(gte=start, lte=end)))
    response = await qdrant().query_points(
        settings.qdrant_collection,
        query=await embed_query(query),
        query_filter=models.Filter(must=conditions) if conditions else None,
        limit=top_k or settings.rag_top_k,
        score_threshold=settings.rag_score_threshold,
        with_payload=True,
    )
    return response.points
