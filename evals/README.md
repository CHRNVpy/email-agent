# Evaluations

Three small, reproducible evaluations. The data is fictional and lives in this folder.

| Script | What it measures | Needs |
|---|---|---|
| `python -m evals.retrieval` | Retrieval quality on 29 labelled questions, with ablations and a score-threshold sweep | Gemini key, Qdrant |
| `python -m evals.routing` | Whether the router picks the right specialist chain for 30 labelled emails | Gemini key |
| `python -m evals.e2e` | End-to-end latency per stage, tokens and cost on 8 typical requests | Gemini key, Qdrant |

Add `--offline` to `retrieval` / `routing` to check the harness without keys: in-memory Qdrant with
hashing embeddings, or a stub router. **Those numbers are not results.**

Results are written to `evals/results/*.md` and `*.json` (including rankings, plans and replies,
so that every number can be traced back to what the system actually returned).

## Data

- `corpus/` has 20 documents of the fictional company Brightwave Labs: policies, SLA, a product spec,
  release notes and meeting notes. Each has a `date:` in its front matter.
- `retrieval_questions.jsonl` has 29 questions, each labelled with its relevant document ids:
  - 16 **single** — one document answers the question, often paraphrased ("money back" vs. "refund");
  - 5 **multi** — the answer is spread over 2–4 documents;
  - 4 **date** — only answerable with the right time window ("this month", "in April 2026");
    "today" is fixed at 2026-06-30 for reproducibility;
  - 4 **unanswerable** — nothing in the corpus answers it, including one near miss about a competitor's price.
- `routing_cases.jsonl` has 30 emails: 5–6 per specialist, two-step chains, attachments and two vague
  requests that should get a clarifying question. A case may accept several plans.

## Metric choices

- **Hit@k / MRR@5** for questions with one relevant document. Precision@k is misleading there,
  because its maximum is 1/k (0.2 at k=5).
- **P@5 / R@5** only for multi-document questions.
- Retrieval is scored at the production `RAG_SCORE_THRESHOLD`, so results can come back empty.
  **Correct abstentions** (unanswerable → nothing) and **false abstentions** (answerable →
  nothing) show the trade-off the threshold controls. The sweep shows where it should be set.
- Routing counts an exact ordered plan as correct. First-step accuracy and per-specialist
  precision and recall show *how* it fails; `--repeat` measures run-to-run consistency.
- Latency is reported as p50/p95 (nearest rank). With 8–30 samples p95 is indicative, not precise.

## Limitations

The evaluation sets are small (29 / 30 / 8), so one question moves a metric by 3–4 points.
The corpus is synthetic and written by the author, so vocabulary overlap with the questions may be
higher than with real user queries. The documents are short: at the default chunk size of 1,000
characters each fits in a single chunk (20 chunks, versus 28 at 500). The chunk-size ablation
therefore mostly compares whole documents with half documents. Treat the numbers as a regression baseline and a way to
compare configurations, not as a benchmark.
