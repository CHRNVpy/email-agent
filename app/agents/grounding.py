"""Number grounding: every number in a reply must appear in the data the model was given.

A specialist can call its tool and still misquote the result ($147,409.90 -> $174,409.90).
This module checks replies deterministically: it extracts the numbers a reader would take
as facts, and looks for each of them in the evidence of the same run (tool results, the
request, today's date). Unmatched numbers get one retry with specific feedback; what happens
after that is up to the caller (SQL falls back to a code-rendered table, other agents keep the
answer and record the mismatch).

Derived values — a sum or a growth rate computed by the model — cannot be matched and are
reported too. That is intentional: the SQL planner is asked to compute them in the query.
"""

import calendar
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

# English month names and abbreviations ("sept" is common but not in `calendar`). The app never calls
# `locale.setlocale`, so `calendar` returns English names; replies are written in English too.
_MONTH_NAMES = {name.lower() for name in (*calendar.month_name[1:], *calendar.month_abbr[1:], "Sept")}
_MONTH = rf"(?:{'|'.join(sorted(_MONTH_NAMES, key=len, reverse=True))})\.?"
# Text that contains digits but no factual numbers to verify.
_IGNORED = [
    re.compile(r"```.*?```", re.DOTALL),  # code blocks
    re.compile(r"`[^`\n]*`"),  # inline code
    re.compile(r"https?://\S+|www\.\S+"),  # URLs
    re.compile(r"\]\([^)]*\)"),  # markdown link targets
    re.compile(r"\[\d+\]"),  # citation markers [1]
    re.compile(r"^\s*(?:#{1,6}\s*)?\d+[.)]\s", re.MULTILINE),  # numbered-list markers
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?\b"),  # ISO dates
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?", re.IGNORECASE),  # times
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+\d{{4}})?", re.IGNORECASE),  # 11 September 2026
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.IGNORECASE),  # Sep 11, 2026
    re.compile(rf"\b{_MONTH}\s+\d{{4}}\b", re.IGNORECASE),  # June 2026
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),  # 09/11/2026
]
_MENTION = re.compile(
    r"(?<![\w.\-/])"
    r"(?P<sign>[-−])?(?P<currency>[$€£])?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:(?P<suffix>[kKmMbB]|bn)(?![\w])|\s?(?P<word>thousand|million|billion)\b)?"
    r"(?P<percent>\s?%)?"
    r"(?![\w\-])"
)
_EVIDENCE = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}

# Numbers that carry no claim on their own ("1 invoice", "0 rows").
ALWAYS_ALLOWED = frozenset({0.0, 1.0})

FEEDBACK = (
    "Some numbers in your reply do not appear in the data you were given: {numbers}. "
    "Rewrite the reply using only values that are present in the data, copied exactly. "
    "If a number you want to state is not in the data, leave it out or say it is not available "
    "instead of computing or estimating it."
)


@dataclass(frozen=True)
class Mention:
    text: str
    value: float
    decimals: int
    scale: float = 1.0
    percent: bool = False

    def matches(self, evidence: float) -> bool:
        tolerance = 0.5 * 10 ** (-self.decimals) * self.scale + 1e-9
        candidates = [abs(evidence)]
        if self.percent:
            candidates.append(abs(evidence) * 100)
        return any(abs(c - abs(self.value)) <= tolerance for c in candidates)


def _strip_ignored(text: str) -> str:
    for pattern in _IGNORED:
        text = pattern.sub(" ", text)
    return text


def extract_numbers(text: str) -> list[Mention]:
    """Numbers in a reply that state a fact (amounts, counts, percentages)."""
    mentions = []
    for match in _MENTION.finditer(_strip_ignored(text)):
        number = match.group("number").replace(",", "")
        unit = (match.group("suffix") or match.group("word") or "").lower()
        scale = _SCALE.get(unit, 1.0)
        decimals = len(number.split(".")[1]) if "." in number else 0
        mentions.append(
            Mention(
                text=match.group(0).strip(),
                value=float(number) * scale,
                decimals=decimals,
                scale=scale,
                percent=bool(match.group("percent")),
            )
        )
    return mentions


def evidence_numbers(*texts: str) -> set[float]:
    """Every number in the given texts, read permissively (JSON, prose, dates, identifiers)."""
    values: set[float] = set()
    for text in texts:
        for match in _EVIDENCE.finditer(_THOUSANDS.sub("", text or "")):
            value = float(match.group(0))
            if math.isfinite(value):
                values.add(value)
    return values


def unverified_numbers(reply: str, evidence: Iterable[float]) -> list[str]:
    """Numbers stated in `reply` that match nothing in `evidence` (deduplicated, in order)."""
    pool = sorted(set(evidence) | ALWAYS_ALLOWED)
    missing: list[str] = []
    for mention in extract_numbers(reply):
        if mention.text in missing or any(mention.matches(value) for value in pool):
            continue
        missing.append(mention.text)
    return missing


def feedback(numbers: list[str]) -> str:
    return FEEDBACK.format(numbers=", ".join(numbers))
