"""Parsing workflow definitions from a Google Sheet.

One row = one workflow:

| workflow | model | actions | source_<name>... | stage_1 | stage_2 | ... |

* `workflow` — name; an email whose subject contains it starts the workflow;
* `model` — optional LLM override; `actions` — comma-separated tool groups;
* `source_*` — inputs, referenced in prompts as `{source_<name>}`;
  a Drive folder / Google Doc / Google Sheet URL, `query: <text>` for a
  knowledge-base search, or plain text. Prefix with `iteration=TRUE;` to run
  the stages that reference the source once per item;
* `stage_N` — prompt templates executed in order; `{await_reply}` pauses the
  workflow and emails the stage prompt to the user, resuming on their reply.
"""

import logging
import re
import time

from app.config import settings
from app.google import sheets
from app.workflows.models import SourceConfig, SourceKind, StageConfig, WorkflowConfig

logger = logging.getLogger(__name__)

_ITERATE = re.compile(r"^\s*iteration\s*=\s*true\s*;?\s*", re.IGNORECASE)
_QUERY = re.compile(r'^\s*query\s*[:=]\s*"?(.*?)"?\s*$', re.IGNORECASE | re.DOTALL)
_STAGE = re.compile(r"^stage_(\d+)$")
_NAME_COLUMNS = ("workflow", "task")

_cache: tuple[float, list[WorkflowConfig]] | None = None


def detect_kind(column: str, value: str) -> tuple[SourceKind, str]:
    if "/folders/" in value:
        return SourceKind.DRIVE_FOLDER, value
    if "/document/d/" in value:
        return SourceKind.GOOGLE_DOC, value
    if "/spreadsheets/d/" in value:
        return SourceKind.GOOGLE_SHEET, value
    query = _QUERY.match(value)
    if query:
        return SourceKind.KNOWLEDGE_QUERY, query.group(1).strip()
    if column.startswith(("source_knowledge", "source_kb", "source_rag")):
        return SourceKind.KNOWLEDGE_QUERY, value
    return SourceKind.TEXT, value


def parse_source(column: str, raw: str) -> SourceConfig | None:
    value = raw.strip()
    if not value:
        return None
    iterate = bool(_ITERATE.match(value))
    value = _ITERATE.sub("", value).strip()
    kind, value = detect_kind(column, value)
    return SourceConfig(name=column, kind=kind, value=value, iterate=iterate)


def parse_row(row: dict) -> WorkflowConfig | None:
    row = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k}
    name = next((row[c] for c in _NAME_COLUMNS if row.get(c)), "")
    if not name:
        return None

    sources = {}
    for column, value in row.items():
        if column.startswith("source_") and (source := parse_source(column, value)):
            sources[column] = source

    stages = sorted(
        (
            StageConfig(number=int(match.group(1)), template=value)
            for column, value in row.items()
            if (match := _STAGE.match(column)) and value
        ),
        key=lambda s: s.number,
    )
    if not stages:
        logger.warning("Workflow '%s' has no stages; skipped", name)
        return None

    actions = [a.strip().lower() for a in row.get("actions", "").split(",") if a.strip()]
    return WorkflowConfig(name=name, model=row.get("model") or None, actions=actions, sources=sources, stages=stages)


def parse_rows(rows: list[dict]) -> list[WorkflowConfig]:
    return [config for row in rows if (config := parse_row(row))]


def load_workflows(force: bool = False) -> list[WorkflowConfig]:
    """Workflow definitions from the sheet, cached for WORKFLOW_CACHE_TTL seconds."""
    global _cache
    if not settings.workflows_enabled:
        return []
    if not force and _cache and time.monotonic() - _cache[0] < settings.workflow_cache_ttl:
        return _cache[1]
    worksheet = sheets.client().open_by_key(settings.workflow_sheet_id).worksheet(settings.workflow_sheet_tab)
    workflows = parse_rows(worksheet.get_all_records())
    _cache = (time.monotonic(), workflows)
    logger.info("Loaded %d workflow(s) from the sheet", len(workflows))
    return workflows


def match_subject(subject: str, workflows: list[WorkflowConfig]) -> WorkflowConfig | None:
    """The workflow whose name appears in the subject (the longest name wins)."""
    subject = subject.lower()
    candidates = [w for w in workflows if w.name.lower() in subject]
    return max(candidates, key=lambda w: len(w.name), default=None)
