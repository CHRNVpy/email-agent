# Retrieval evaluation

Run 2026-09-11T10:44:56+00:00 · corpus 20 docs · 29 questions (16 single, 5 multi, 4 date, 4 unanswerable) · embeddings `gemini-embedding-001` (768d) · score threshold 0.3

| Config | Hit@1 | Hit@3 | Hit@5 | MRR@5 | Date Hit@3 | P@5 multi | R@5 multi | Correct abstain | False abstain |
|---|---|---|---|---|---|---|---|---|---|
| baseline (soft date filter) | 0.90 | 1.00 | 1.00 | 0.94 | 1.00 | 0.52 | 1.00 | 0.00 | 0.00 |
| hard date filter | 0.90 | 1.00 | 1.00 | 0.94 | 1.00 | 0.52 | 1.00 | 0.00 | 0.00 |
| no date filter | 0.85 | 1.00 | 1.00 | 0.92 | 1.00 | 0.52 | 1.00 | 0.00 | 0.00 |
| chunk 500 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.52 | 1.00 | 0.00 | 0.00 |
| no title in chunks | 0.90 | 1.00 | 1.00 | 0.95 | 1.00 | 0.52 | 1.00 | 0.00 | 0.00 |

**Score-threshold sweep (baseline):**

| Threshold | Answerable Hit@5 | Correct abstain | False abstain |
|---|---|---|---|
| 0.0 | 1.00 | 0.00 | 0.00 |
| 0.3 | 1.00 | 0.00 | 0.00 |
| 0.4 | 1.00 | 0.00 | 0.00 |
| 0.5 | 1.00 | 0.00 | 0.00 |
| 0.55 | 1.00 | 0.00 | 0.00 |
| 0.6 | 1.00 | 0.00 | 0.00 |
| 0.65 | 1.00 | 0.00 | 0.00 |
| 0.7 | 1.00 | 0.00 | 0.00 |
| 0.75 | 0.96 | 0.25 | 0.04 |

**Score overlap:** relevant documents score 0.74–… (median 0.81); the top hit of unanswerable questions scores 0.75–0.82. The ranges overlap, so no score threshold can separate them: abstention has to happen in the answer step (measured by `kb-unanswerable` in the end-to-end eval).

**Latency:** query embedding + vector search p50 715 ms, p95 882 ms; LLM date parsing (only the 12 questions with a time cue) p50 1.89 s, p95 2.75 s.

**Baseline misses** (no relevant document in the top 3, or no abstention):

- `u01` should abstain, top hit `travel-policy` (0.82)
- `u02` should abstain, top hit `travel-policy` (0.75)
- `u03` should abstain, top hit `competitor-analysis` (0.77)
- `u04` should abstain, top hit `travel-policy` (0.76)
