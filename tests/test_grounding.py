from app.agents.grounding import evidence_numbers, extract_numbers, feedback, unverified_numbers

ROWS = '{"row_count": 2, "rows": [{"customer": "Bluepeak Retail", "revenue": 147409.9, "growth": 0.2345}]}'


def texts(reply: str) -> list[str]:
    return [m.text for m in extract_numbers(reply)]


def test_currency_thousands_and_rounding_match_raw_values():
    evidence = evidence_numbers(ROWS)
    assert unverified_numbers("Bluepeak spent $147,409.90 this quarter.", evidence) == []
    assert unverified_numbers("Bluepeak spent about $147,410.", evidence) == []  # rounded to whole dollars
    assert unverified_numbers("Bluepeak spent $174,409.90.", evidence) == ["$174,409.90"]


def test_percentages_match_fractions_and_scaled_units():
    evidence = evidence_numbers(ROWS)
    assert unverified_numbers("Growth was 23.45% (0.2345).", evidence) == []
    assert unverified_numbers("Revenue reached $147.4k.", evidence) == []
    assert unverified_numbers("Growth was 32.45%.", evidence) == ["32.45%"]


def test_row_count_and_request_numbers_are_evidence():
    evidence = evidence_numbers(ROWS, "Who are our top 5 customers?")
    assert unverified_numbers("I found 2 customers out of the top 5 you asked for.", evidence) == []


def test_non_factual_digits_are_ignored():
    reply = """### 1. Overview
See [2] and https://example.com/reports/2026/9931.
Updated cell C7 in Pipeline!A42:D46 on 2026-09-11 at 14:30, due 08 August 2026 (Q3, H1).
Model gemini-2.5-flash, API v1, a 30-day trial.

```sql
SELECT 12345 FROM orders LIMIT 5
```
Inline `LIMIT 99` too."""
    assert texts(reply) == []


def test_negative_and_duplicate_mentions_are_reported_once():
    assert texts("Down -4% and up 18%") == ["-4%", "18%"]
    assert unverified_numbers("$999 then $999 again", evidence_numbers(ROWS)) == ["$999"]


def test_feedback_lists_the_numbers():
    assert "$999, 12%" in feedback(["$999", "12%"])
