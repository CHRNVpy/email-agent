"""Retrieval and routing metrics (pure functions, unit-tested)."""

import math
import statistics
from collections.abc import Sequence


def unique_in_order(items: Sequence[str]) -> list[str]:
    """Chunk hits -> document ids in rank order (a document counts once, at its best rank)."""
    seen, ordered = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def hit_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return float(any(doc in relevant for doc in ranked[:k]))


def reciprocal_rank(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    for rank, doc in enumerate(ranked[:k], 1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Standard P@k: relevant documents in the top k, divided by k.

    With a single relevant document the maximum is 1/k, which is why P@k is only
    reported for questions that have several relevant documents.
    """
    return sum(doc in relevant for doc in ranked[:k]) / k


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return sum(doc in relevant for doc in ranked[:k]) / len(relevant)


def mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100); fine for small samples, no interpolation."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered), max(1, math.ceil(q / 100 * len(ordered)))) - 1
    return ordered[index]


def plan_matches(plan: Sequence[str], accepted: Sequence[Sequence[str]]) -> bool:
    return any(list(plan) == list(option) for option in accepted)
