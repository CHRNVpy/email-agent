# Routing evaluation

Run 2026-09-11T16:43:20+00:00 · 30 cases × 3 run(s)

| Model | Exact plan | First step | Clarifies vague | Consistency | Latency p50 / p95 | Tokens | Cost / routing | Errors |
|---|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash` | 0.91 | 0.92 | 1.00 | 0.83 | 1.76 s / 2.29 s | 630 | $0.00049 | 0 |
| `gemini-3.1-flash-lite` | 1.00 | 1.00 | 1.00 | 1.00 | 1.30 s / 1.76 s | 567 | $0.00020 | 0 |

**Per specialist — `gemini-2.5-flash`:**

| Specialist | Precision | Recall |
|---|---|---|
| assistant | 0.90 | 0.75 |
| files | 0.90 | 1.00 |
| finance | 1.00 | 1.00 |
| knowledge | 1.00 | 0.83 |
| sheets | 1.00 | 1.00 |
| sql | 1.00 | 1.00 |
| web | 1.00 | 0.89 |

Misrouted:

- `r23` "Great, thanks a lot — that's all I needed." → [] (expected ['assistant'])
- `r27` 'Check what our SLA says about service credits and draft a short reply ' → ['files', 'assistant'] (expected ['knowledge'] or ['knowledge', 'assistant'])
- `r28` "Look up Datadog's latest revenue growth and compare it with our own re" → ['finance', 'sql', 'assistant'] (expected ['finance', 'sql'] or ['sql', 'finance'] or ['web', 'sql'] or ['sql', 'web'])

**Per specialist — `gemini-3.1-flash-lite`:**

| Specialist | Precision | Recall |
|---|---|---|
| assistant | 1.00 | 1.00 |
| files | 1.00 | 1.00 |
| finance | 1.00 | 1.00 |
| knowledge | 1.00 | 1.00 |
| sheets | 1.00 | 1.00 |
| sql | 1.00 | 1.00 |
| web | 1.00 | 1.00 |

Misrouted:

- none
