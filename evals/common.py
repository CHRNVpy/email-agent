"""Shared helpers for the evaluation scripts."""

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.rag import store

EVALS_DIR = Path(__file__).parent
CORPUS_DIR = EVALS_DIR / "corpus"
RESULTS_DIR = EVALS_DIR / "results"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash_embedding(text: str) -> list[float]:
    """Deterministic bag-of-words vector — lets the harness run without an API key (not a real result)."""
    vector = [0.0] * settings.embedding_dimension
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vector[digest % len(vector)] += 1.0 if digest & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def use_offline_backends() -> None:
    """In-memory Qdrant + hashing embeddings: checks the plumbing, numbers are meaningless."""
    settings.qdrant_url = ":memory:"
    store.qdrant.cache_clear()

    async def embed_texts(texts: list[str]) -> list[list[float]]:
        return [_hash_embedding(t) for t in texts]

    async def embed_query(text: str) -> list[float]:
        return _hash_embedding(text)

    store.embed_texts = embed_texts
    store.embed_query = embed_query


def results_path(name: str, offline: bool, suffix: str) -> Path:
    directory = RESULTS_DIR / ("offline" if offline else "")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.{suffix}"


def run_metadata(offline: bool, **extra) -> dict:
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "offline": offline,
        "embedding_model": "hashing (offline)" if offline else settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "default_model": settings.default_model,
        **extra,
    }


def fmt(value: float, digits: int = 2) -> str:
    return "–" if value != value else f"{value:.{digits}f}"  # NaN -> dash
