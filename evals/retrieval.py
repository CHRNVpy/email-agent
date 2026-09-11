"""Retrieval evaluation of the knowledge base.

Indexes `evals/corpus` (20 documents of a fictional company) and asks 29 labelled
questions of four kinds: single-document facts, multi-document questions,
date-constrained questions and questions the corpus cannot answer.

    python -m evals.retrieval            # real Gemini embeddings + Qdrant (QDRANT_URL)
    python -m evals.retrieval --offline  # in-memory Qdrant + hashing embeddings: plumbing check only

Reported, per configuration (ablations: soft / hard / no date filter, chunk size, title-in-chunk):
  * Hit@1/3/5 and MRR@5 — questions with one relevant document (single + date);
  * P@5 and R@5 — questions with several relevant documents;
  * correct abstentions — unanswerable questions that return nothing above the score threshold;
  * false abstentions — answerable questions that return nothing above the threshold;
  * a threshold sweep, because the abstention/recall trade-off is set by that one number.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date

from app.config import settings
from app.rag import store
from app.rag.indexers import index_local_files
from app.rag.retriever import DateRange, has_temporal_cue, parse_date_range
from evals import common
from evals.metrics import hit_at_k, mean, percentile, precision_at_k, recall_at_k, reciprocal_rank

TODAY = date(2026, 6, 30)  # fixed so that "this month" in the questions is reproducible
K = 5
FETCH_CHUNKS = 25
THRESHOLDS = [0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75]


@dataclass(frozen=True)
class Config:
    chunk_size: int = 1000
    chunk_overlap: int = 200
    title_in_chunks: bool = True
    date_mode: str = "soft"  # production default; see app.rag.store.search

    @property
    def index_key(self) -> tuple:
        return (self.chunk_size, self.chunk_overlap, self.title_in_chunks)


CONFIGS = {
    "baseline (soft date filter)": Config(),
    "hard date filter": Config(date_mode="hard"),
    "no date filter": Config(date_mode="none"),
    "chunk 500": Config(chunk_size=500, chunk_overlap=100),
    "no title in chunks": Config(title_in_chunks=False),
}
BASELINE = "baseline (soft date filter)"


async def build_index(config: Config) -> str:
    collection = "eval_{}_{}_{}".format(*config.index_key).lower()
    settings.qdrant_collection = collection
    settings.rag_chunk_size, settings.rag_chunk_overlap = config.chunk_size, config.chunk_overlap
    settings.rag_title_in_chunks = config.title_in_chunks
    if await store.qdrant().collection_exists(collection):
        await store.qdrant().delete_collection(collection)
    chunks = await index_local_files(common.CORPUS_DIR)
    print(f"  indexed {chunks} chunks into {collection}")
    return collection


async def rank_documents(question: str, dates: DateRange, date_mode: str) -> tuple[list[tuple[str, float]], float]:
    """Documents in rank order with their best chunk score, and the search latency."""
    started = time.perf_counter()
    hits = await store.search(
        question, top_k=FETCH_CHUNKS, start=dates.start, end=dates.end, score_threshold=0.0, date_mode=date_mode
    )
    latency = time.perf_counter() - started
    ranked: dict[str, float] = {}  # keep the search order (soft mode puts in-window documents first)
    for hit in hits:
        doc = hit.payload["source_id"]
        ranked[doc] = max(ranked.get(doc, 0.0), hit.score)
    return list(ranked.items()), latency


def score(questions: list[dict], rankings: dict[str, list[tuple[str, float]]], threshold: float) -> dict:
    by_type: dict[str, list[dict]] = {}
    for q in questions:
        by_type.setdefault(q["type"], []).append(q)

    def ranked(q) -> list[str]:
        return [doc for doc, s in rankings[q["id"]] if s >= threshold]

    single = by_type.get("single", []) + by_type.get("date", [])
    multi = by_type.get("multi", [])
    answerable = single + multi
    unanswerable = by_type.get("unanswerable", [])
    return {
        "hit@1": mean([hit_at_k(ranked(q), set(q["relevant"]), 1) for q in single]),
        "hit@3": mean([hit_at_k(ranked(q), set(q["relevant"]), 3) for q in single]),
        "hit@5": mean([hit_at_k(ranked(q), set(q["relevant"]), 5) for q in single]),
        "mrr@5": mean([reciprocal_rank(ranked(q), set(q["relevant"]), K) for q in single]),
        "date_hit@3": mean([hit_at_k(ranked(q), set(q["relevant"]), 3) for q in by_type.get("date", [])]),
        "p@5_multi": mean([precision_at_k(ranked(q), set(q["relevant"]), K) for q in multi]),
        "r@5_multi": mean([recall_at_k(ranked(q), set(q["relevant"]), K) for q in multi]),
        "answerable_hit@5": mean([hit_at_k(ranked(q), set(q["relevant"]), K) for q in answerable]),
        "correct_abstentions": mean([float(not ranked(q)) for q in unanswerable]),
        "false_abstentions": mean([float(not ranked(q)) for q in answerable]),
    }


def misses(questions: list[dict], rankings: dict, threshold: float) -> list[str]:
    lines = []
    for q in questions:
        docs = [doc for doc, s in rankings[q["id"]] if s >= threshold]
        relevant = set(q["relevant"])
        if q["type"] == "unanswerable":
            if docs:
                top_doc, top_score = rankings[q["id"]][0]
                lines.append(f"- `{q['id']}` should abstain, top hit `{top_doc}` ({top_score:.2f})")
        elif not any(d in relevant for d in docs[:3]):
            rank = next((i for i, d in enumerate(docs, 1) if d in relevant), None)
            if not docs:
                where = "nothing above the threshold"
            elif rank:
                where = f"first relevant at rank {rank}"
            else:
                where = f"no relevant document among {len(docs)} results"
            lines.append(f"- `{q['id']}` {q['question']} → {where}")
    return lines


def score_overlap(questions: list[dict], rankings: dict) -> dict:
    """Best relevant-document score for answerable questions vs. top score for unanswerable ones."""
    relevant_best, unanswerable_top = [], []
    for q in questions:
        docs = rankings[q["id"]]
        if q["relevant"]:
            scores = [s for d, s in docs if d in set(q["relevant"])]
            if scores:
                relevant_best.append(max(scores))
        elif docs:
            unanswerable_top.append(max(s for _, s in docs))
    return {
        "relevant_min": min(relevant_best, default=float("nan")),
        "relevant_median": percentile(relevant_best, 50),
        "unanswerable_min": min(unanswerable_top, default=float("nan")),
        "unanswerable_max": max(unanswerable_top, default=float("nan")),
    }


def to_markdown(meta: dict, table: dict, sweep: list[dict], failures: list[str], latency: dict, overlap: dict) -> str:
    f = common.fmt
    out = [
        "# Retrieval evaluation",
        "",
        f"Run {meta['run_at']} · corpus {meta['documents']} docs · {meta['questions']} questions "
        f"({meta['by_type']}) · embeddings `{meta['embedding_model']}` ({meta['embedding_dimension']}d) · "
        f"score threshold {meta['threshold']}",
        "",
    ]
    if meta["offline"]:
        out += ["> **Offline plumbing check** — hashing embeddings, numbers are not meaningful.", ""]
    out += [
        "| Config | Hit@1 | Hit@3 | Hit@5 | MRR@5 | Date Hit@3 | P@5 multi | R@5 multi "
        "| Correct abstain | False abstain |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in table.items():
        out.append(
            f"| {name} | {f(m['hit@1'])} | {f(m['hit@3'])} | {f(m['hit@5'])} | {f(m['mrr@5'])} | "
            f"{f(m['date_hit@3'])} | {f(m['p@5_multi'])} | {f(m['r@5_multi'])} | "
            f"{f(m['correct_abstentions'])} | {f(m['false_abstentions'])} |"
        )
    out += [
        "",
        "**Score-threshold sweep (baseline):**",
        "",
        "| Threshold | Answerable Hit@5 | Correct abstain | False abstain |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {row['threshold']} | {f(row['answerable_hit@5'])} | {f(row['correct_abstentions'])} | "
        f"{f(row['false_abstentions'])} |"
        for row in sweep
    ]
    separable = overlap["unanswerable_max"] < overlap["relevant_min"]
    out += [
        "",
        f"**Score overlap:** relevant documents score {f(overlap['relevant_min'])}–… (median "
        f"{f(overlap['relevant_median'])}); the top hit of unanswerable questions scores "
        f"{f(overlap['unanswerable_min'])}–{f(overlap['unanswerable_max'])}. "
        + (
            "A score threshold can separate them."
            if separable
            else "The ranges overlap, so no score threshold can separate them: abstention has to happen "
            "in the answer step (measured by `kb-unanswerable` in the end-to-end eval)."
        ),
    ]
    out += [
        "",
        f"**Latency:** query embedding + vector search p50 {f(latency['search_p50'] * 1000, 0)} ms, "
        f"p95 {f(latency['search_p95'] * 1000, 0)} ms; "
        f"LLM date parsing (only the {latency['dates_n']} questions with a time cue) "
        f"p50 {f(latency['dates_p50'], 2)} s, p95 {f(latency['dates_p95'], 2)} s.",
        "",
        "**Baseline misses** (no relevant document in the top 3, or no abstention):",
        "",
        *(failures or ["- none"]),
    ]
    return "\n".join(out) + "\n"


async def main(offline: bool) -> None:
    if offline:
        common.use_offline_backends()
    questions = common.load_jsonl(common.EVALS_DIR / "retrieval_questions.jsonl")
    threshold = settings.rag_score_threshold

    # Date ranges come from one LLM call per question; parse once, reuse across configs.
    date_ranges, date_latency = {}, []
    for q in questions:
        if offline:
            date_ranges[q["id"]] = DateRange()
            continue
        started = time.perf_counter()
        date_ranges[q["id"]] = await parse_date_range(q["question"], today=TODAY)
        if has_temporal_cue(q["question"]):  # other questions skip the LLM call entirely
            date_latency.append(time.perf_counter() - started)

    table, rankings_by_config, search_latency, indexed = {}, {}, [], {}
    for name, config in CONFIGS.items():
        print(f"[{name}]")
        if config.index_key not in indexed:
            indexed[config.index_key] = await build_index(config)
        settings.qdrant_collection = indexed[config.index_key]
        rankings = {}
        for q in questions:
            rankings[q["id"]], latency = await rank_documents(q["question"], date_ranges[q["id"]], config.date_mode)
            search_latency.append(latency)
        rankings_by_config[name] = rankings
        table[name] = score(questions, rankings, threshold)

    baseline = rankings_by_config[BASELINE]
    sweep = [{"threshold": t, **score(questions, baseline, t)} for t in THRESHOLDS]
    latency = {
        "search_p50": percentile(search_latency, 50),
        "search_p95": percentile(search_latency, 95),
        "dates_p50": percentile(date_latency, 50),
        "dates_p95": percentile(date_latency, 95),
        "dates_n": len(date_latency),
    }
    types = {}
    for q in questions:
        types[q["type"]] = types.get(q["type"], 0) + 1
    meta = common.run_metadata(
        offline,
        documents=len(list(common.CORPUS_DIR.glob("*.md"))),
        questions=len(questions),
        by_type=", ".join(f"{n} {t}" for t, n in types.items()),
        threshold=threshold,
        today=TODAY.isoformat(),
    )
    overlap = score_overlap(questions, baseline)
    report = to_markdown(meta, table, sweep, misses(questions, baseline, threshold), latency, overlap)
    print("\n" + report)

    common.results_path("retrieval", offline, "md").write_text(report, encoding="utf-8")
    common.results_path("retrieval", offline, "json").write_text(
        json.dumps(
            {
                "meta": meta,
                "configs": table,
                "sweep": sweep,
                "latency": latency,
                "score_overlap": overlap,
                "date_ranges": {k: v.model_dump() for k, v in date_ranges.items()},
                "rankings": {
                    name: {qid: [[d, round(s, 4)] for d, s in docs[:10]] for qid, docs in r.items()}
                    for name, r in rankings_by_config.items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="No API keys: in-memory Qdrant + hashing embeddings")
    asyncio.run(main(parser.parse_args().offline))
