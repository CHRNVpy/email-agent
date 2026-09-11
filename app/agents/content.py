"""Building LLM inputs from an email: text, prior results and inline attachments."""

from google.genai import types
from langchain_core.messages import BaseMessage

from app.agents import prompts
from app.agents.state import AgentState
from app.llm import is_xai_model, resolve_model
from app.messages import Attachment

# Gemini accepts up to ~20 MB of inline data per request; keep headroom for the text.
MAX_INLINE_BYTES = 18 * 1024 * 1024
INLINE_MIME_PREFIXES = ("application/pdf", "image/", "text/", "audio/", "video/")


def inline_attachments(attachments: list[Attachment]) -> tuple[list[Attachment], list[str]]:
    """Split attachments into those sent inline and the names of those skipped."""
    included, skipped, total = [], [], 0
    for attachment in attachments:
        if not attachment.mime_type.startswith(INLINE_MIME_PREFIXES):
            skipped.append(f"{attachment.filename} (unsupported type {attachment.mime_type})")
        elif total + attachment.size > MAX_INLINE_BYTES:
            skipped.append(f"{attachment.filename} (over the size limit)")
        else:
            included.append(attachment)
            total += attachment.size
    return included, skipped


def request_text(state: AgentState) -> str:
    text = state.email.as_prompt()
    if state.results:
        previous = "\n\n".join(f"## {r.agent}\n{r.output}" for r in state.results)
        text += f"\n\n{prompts.PREVIOUS_RESULTS}\n{previous}"
    return text


def human_content(state: AgentState) -> str | list[dict]:
    """LangChain message content. Gemini models also receive attachments as media blocks."""
    text = request_text(state)
    if not state.email.attachments or is_xai_model(resolve_model(state.model)):
        return text
    included, skipped = inline_attachments(state.email.attachments)
    if skipped:
        text += "\n\nAttachments not available to you: " + "; ".join(skipped)
    return [{"type": "text", "text": text}] + [
        {"type": "media", "mime_type": a.mime_type, "data": a.data} for a in included
    ]


def genai_parts(state: AgentState) -> list[types.Part]:
    """Native google-genai parts (used by the web and files specialists)."""
    included, skipped = inline_attachments(state.email.attachments)
    text = request_text(state)
    if skipped:
        text += "\n\nAttachments that could not be read: " + "; ".join(skipped)
    return [types.Part.from_bytes(data=a.data, mime_type=a.mime_type) for a in included] + [
        types.Part.from_text(text=text)
    ]


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part if isinstance(part, str) else part.get("text", "")
        for part in content
        if isinstance(part, str) or part.get("type", "text") == "text"
    )
