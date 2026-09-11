"""Transport-independent representation of an incoming email."""

import re
from email.utils import parseaddr

from pydantic import BaseModel, Field

_QUOTE_HEADER = re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE | re.MULTILINE)
_MODEL_FLAG = re.compile(r"\s*-model:(\S+)", re.IGNORECASE)


class Attachment(BaseModel):
    filename: str
    mime_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


class IncomingEmail(BaseModel):
    id: str
    thread_id: str
    sender: str
    subject: str = ""
    body: str = ""
    thread: str = ""
    message_id_header: str | None = None
    cc: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)

    @property
    def sender_email(self) -> str:
        return parseaddr(self.sender)[1].lower()

    @property
    def latest_text(self) -> str:
        """The newest message without the quoted conversation below it."""
        return strip_quoted_reply(self.body)

    def as_prompt(self) -> str:
        parts = [f"Subject: {self.subject}", "", self.latest_text]
        if self.thread and self.thread.strip() != self.body.strip():
            parts += ["", "Earlier messages in this thread:", self.thread]
        if self.attachments:
            parts += ["", "Attachments: " + ", ".join(a.filename for a in self.attachments)]
        return "\n".join(parts)


def strip_quoted_reply(text: str) -> str:
    match = _QUOTE_HEADER.search(text)
    if match:
        text = text[: match.start()]
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def parse_model_flag(subject: str) -> tuple[str, str | None]:
    """Extract an optional `-model:<name>` override from the subject line."""
    match = _MODEL_FLAG.search(subject)
    if not match:
        return subject, None
    return _MODEL_FLAG.sub("", subject).strip(), match.group(1)
