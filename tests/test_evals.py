"""Eval harness: metric definitions, corpus loading and the date filter in Qdrant."""

import pytest

from app.config import settings
from app.rag import indexers, store
from evals import common
from evals.metrics import (
    hit_at_k,
    percentile,
    plan_matches,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    unique_in_order,
)


def test_ranking_metrics():
    ranked = ["a", "b", "c", "d", "e"]
    assert hit_at_k(ranked, {"c"}, 3) == 1.0 and hit_at_k(ranked, {"c"}, 2) == 0.0
    assert reciprocal_rank(ranked, {"c"}, 5) == pytest.approx(1 / 3)
    assert reciprocal_rank(ranked, {"z"}, 5) == 0.0
    assert precision_at_k(ranked, {"a", "e", "z"}, 5) == pytest.approx(0.4)
    assert recall_at_k(ranked, {"a", "e", "z"}, 5) == pytest.approx(2 / 3)
    assert unique_in_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_percentile_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile([7], 95) == 7


def test_plan_matches_any_accepted_option():
    assert plan_matches(["sql", "sheets"], [["sql", "sheets"]])
    assert not plan_matches(["sheets", "sql"], [["sql", "sheets"]])
    assert plan_matches(["knowledge"], [["knowledge"], ["knowledge", "assistant"]])


def test_corpus_front_matter():
    doc = indexers.file_to_local_document(common.CORPUS_DIR / "product-spec-anomaly-alerts.md")
    assert doc.title == "Spec: Anomaly Alerts"
    assert doc.date == "2026-03-05T00:00:00Z"
    assert doc.source_id == "product-spec-anomaly-alerts"
    assert not doc.text.startswith("---")


def test_questions_reference_existing_documents():
    ids = {p.stem for p in common.CORPUS_DIR.glob("*.md")}
    for q in common.load_jsonl(common.EVALS_DIR / "retrieval_questions.jsonl"):
        assert set(q["relevant"]) <= ids, q["id"]
    for case in common.load_jsonl(common.EVALS_DIR / "routing_cases.jsonl"):
        assert case["accept"], case["id"]


@pytest.fixture
async def offline_kb(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_collection", "test_kb")
    common.use_offline_backends()
    await indexers.index_local_files(common.CORPUS_DIR)
    yield
    store.qdrant.cache_clear()


async def test_date_filter_restricts_results(offline_kb):
    hits = await store.search(
        "meeting notes decisions",
        top_k=50,
        start="2026-06-01T00:00:00Z",
        end="2026-06-30T23:59:59Z",
        score_threshold=0,
        date_mode="hard",
    )
    documents = {h.payload["source_id"] for h in hits}
    assert documents == {"meeting-2026-06-04-hiring", "meeting-2026-06-18-globex-escalation", "release-notes-2026-06"}


def test_temporal_cue_prefilter():
    from app.rag.retriever import has_temporal_cue

    assert has_temporal_cue("Were there any incidents in May?")
    assert has_temporal_cue("notes from Q3") and has_temporal_cue("since 2025")
    assert not has_temporal_cue("Who needs to approve that?")
    assert not has_temporal_cue("What did we decide about the market?")


async def test_soft_date_filter_keeps_out_of_window_documents(offline_kb):
    window = {"start": "2026-06-01T00:00:00Z", "end": "2026-06-30T23:59:59Z"}
    soft = await store.search("refund policy annual plan", top_k=10, score_threshold=0, **window)
    hard = await store.search("refund policy annual plan", top_k=10, score_threshold=0, date_mode="hard", **window)
    soft_docs = [h.payload["source_id"] for h in soft]
    assert "refund-policy" in soft_docs  # dated 2025, outside the window, still found
    assert "refund-policy" not in {h.payload["source_id"] for h in hard}
    in_window = {"meeting-2026-06-04-hiring", "meeting-2026-06-18-globex-escalation", "release-notes-2026-06"}
    assert set(soft_docs[:3]) == in_window  # documents inside the window are ranked first


def test_score_overlap_detects_inseparable_scores():
    from evals.retrieval import score_overlap

    questions = [{"id": "a", "relevant": ["x"]}, {"id": "u", "relevant": []}]
    overlap = score_overlap(questions, {"a": [("x", 0.74), ("y", 0.9)], "u": [("z", 0.81)]})
    assert overlap["relevant_min"] == 0.74 and overlap["unanswerable_max"] == 0.81


def test_routing_errors_count_as_wrong_even_for_vague_cases():
    from evals.routing import evaluate

    cases = [{"id": "a", "accept": [["sql"]]}, {"id": "v", "accept": [[]]}]
    base = {"confidence": 0.0, "reasoning": "", "latency_s": 1.0, "cost_usd": 0.0, "tokens": 0}
    run = {"a": {**base, "plan": ["sql"], "error": None}, "v": {**base, "plan": [], "error": "validation error"}}
    result = evaluate(cases, [run])
    assert result["errors"] == 1
    assert result["exact_plan_accuracy"] == 0.5  # the failed vague case must not count as a clarification
    assert result["clarification_accuracy"] == 0.0
