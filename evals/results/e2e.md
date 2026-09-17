# End-to-end latency & cost

Run 2026-09-17T08:58:12+00:00 · model `gemini-2.5-flash` · 27 requests (3 run(s) of 9) · errors 0

**Overall:** latency p50 6.6 s, p95 10.8 s · cost per request mean $0.0026, max $0.0054 · 2614 tokens on average

| Request | Plan | Latency (median) | Router | Specialists | Tokens | Cost |
|---|---|---|---|---|---|---|
| sql-top-customers | sql | 6.7 s | 1.6 s | 5.1 s | 2676 | $0.0027 |
| sql-overdue | sql | 8.2 s | 1.5 s | 6.7 s | 3810 | $0.0041 |
| kb-refund | knowledge | 4.3 s | 1.5 s | 2.9 s | 2247 | $0.0015 |
| kb-june | knowledge | 8.1 s | 1.5 s | 6.6 s | 3096 | $0.0034 |
| kb-unanswerable | knowledge | 4.2 s | 1.5 s | 2.7 s | 2105 | $0.0013 |
| finance-news | finance | 6.6 s | 1.9 s | 4.7 s | 3080 | $0.0026 |
| web-ai-act | web | 6.5 s | 1.5 s | 4.9 s | 1491 | $0.0023 |
| assistant-hello | assistant | 3.4 s | 1.4 s | 2.0 s | 1140 | $0.0011 |
| chain-reminder | sql → assistant | 10.8 s | 1.8 s | 9.0 s | 3929 | $0.0047 |

**Number grounding** — 21 replies had their numbers checked against the data of the same run. 0 contained numbers not found in it and were retried; after the retry 0 still did (0 SQL replies replaced by the returned rows).

**Abstention check** — reply to a question the knowledge base cannot answer: “I'm sorry, but the knowledge-base excerpts do not contain information about a parental leave policy.…”

Cost uses the Gemini paid-tier price table in `app/telemetry.py`; Google Search grounding fees and Alpha Vantage are not included, embedding tokens are estimated.
