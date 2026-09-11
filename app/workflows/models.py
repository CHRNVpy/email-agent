"""Workflow definitions (parsed from the sheet) and run state."""

import re
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.db import utcnow

AWAIT_REPLY = "{await_reply}"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class SourceKind(StrEnum):
    DRIVE_FOLDER = "drive_folder"
    GOOGLE_DOC = "google_doc"
    GOOGLE_SHEET = "google_sheet"
    KNOWLEDGE_QUERY = "knowledge_query"
    TEXT = "text"


class SourceConfig(BaseModel):
    name: str  # column name, used as {placeholder} in stage prompts
    kind: SourceKind
    value: str
    iterate: bool = False


class StageConfig(BaseModel):
    number: int
    template: str

    @property
    def awaits_reply(self) -> bool:
        return AWAIT_REPLY in self.template

    @property
    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.template))


class WorkflowConfig(BaseModel):
    name: str
    model: str | None = None
    actions: list[str] = Field(default_factory=list)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    stages: list[StageConfig] = Field(default_factory=list)

    @property
    def iteration_source(self) -> str | None:
        return next((name for name, s in self.sources.items() if s.iterate), None)


class ResolvedSource(BaseModel):
    content: str = ""
    items: list[str] = Field(default_factory=list)  # one entry per item for iterated sources


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REPLY = "awaiting_reply"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow: str
    user_email: str
    thread_id: str
    model: str | None = None
    status: RunStatus = RunStatus.RUNNING
    config: WorkflowConfig
    sources: dict[str, ResolvedSource] = Field(default_factory=dict)
    history: list[dict[str, str]] = Field(default_factory=list)
    completed_stages: int = 0
    pending_message: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def add_turn(self, prompt: str, answer: str) -> None:
        self.history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]

    @property
    def last_answer(self) -> str:
        return next((m["content"] for m in reversed(self.history) if m["role"] == "assistant"), "")
