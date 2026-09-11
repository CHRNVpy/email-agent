# End-to-end latency & cost

Run 2026-09-11T16:32:18+00:00 · model `gemini-2.5-flash` · 27 requests (3 run(s) of 9) · errors 0

**Overall:** latency p50 7.5 s, p95 20.3 s · cost per request mean $0.0029, max $0.0096 · 2703 tokens on average

| Request | Plan | Latency (median) | Router | Specialists | Tokens | Cost |
|---|---|---|---|---|---|---|
| sql-top-customers | sql | 7.7 s | 1.8 s | 5.8 s | 2641 | $0.0027 |
| sql-overdue | sql | 8.3 s | 1.5 s | 6.9 s | 3670 | $0.0037 |
| kb-refund | knowledge | 4.4 s | 1.5 s | 2.8 s | 2191 | $0.0013 |
| kb-june | knowledge | 8.6 s | 1.8 s | 6.8 s | 2806 | $0.0027 |
| kb-unanswerable | knowledge | 4.5 s | 1.6 s | 2.9 s | 2108 | $0.0014 |
| finance-news | finance | 7.1 s | 1.9 s | 5.2 s | 2775 | $0.0019 |
| web-ai-act | web | 7.5 s | 2.0 s | 5.5 s | 1662 | $0.0028 |
| assistant-hello | assistant | 4.1 s | 1.9 s | 2.2 s | 1128 | $0.0011 |
| chain-reminder | sql → assistant | 20.3 s | 1.9 s | 18.4 s | 5472 | $0.0090 |

**Abstention check** — reply to a question the knowledge base cannot answer: “I'm sorry, but the provided knowledge-base excerpts do not contain information about a parental leave policy.…”

Cost uses the Gemini paid-tier price table in `app/telemetry.py`; Google Search grounding fees and Alpha Vantage are not included, embedding tokens are estimated.
