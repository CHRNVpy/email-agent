"""Gmail API: reading new inbox messages and sending replies.

The Google client library is synchronous; callers in async code wrap these
functions with `asyncio.to_thread`.
"""

import base64
import html
import logging
import re
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

import markdown
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import dedupe
from app.config import settings
from app.google.auth import get_credentials
from app.messages import Attachment, IncomingEmail
from app.state import JsonState

logger = logging.getLogger(__name__)

watch_state = JsonState("gmail_watch.json")

_HTML_TAG = re.compile(r"<[^>]+>")


def gmail_service():
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


# --- Parsing ------------------------------------------------------------------


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode())


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", value)
    return html.unescape(_HTML_TAG.sub("", value)).strip()


def extract_text(payload: dict) -> str:
    """Return the text body of a message, preferring text/plain over text/html."""
    plain, rich = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and not part.get("filename"):
            if mime == "text/plain":
                plain.append(_decode(data).decode("utf-8", errors="replace"))
            elif mime == "text/html":
                rich.append(html_to_text(_decode(data).decode("utf-8", errors="replace")))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    return "\n".join(plain or rich).strip()


def extract_attachments(payload: dict, message_id: str, service) -> list[Attachment]:
    attachments: list[Attachment] = []

    def walk(part: dict) -> None:
        filename = part.get("filename")
        body = part.get("body", {})
        if filename:
            data = body.get("data")
            if not data and body.get("attachmentId"):
                try:
                    data = (
                        service.users()
                        .messages()
                        .attachments()
                        .get(userId="me", messageId=message_id, id=body["attachmentId"])
                        .execute()
                        .get("data")
                    )
                except HttpError as exc:
                    logger.error("Failed to download attachment %s: %s", filename, exc)
            if data:
                attachments.append(
                    Attachment(
                        filename=filename,
                        mime_type=part.get("mimeType") or "application/octet-stream",
                        data=_decode(data),
                    )
                )
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    return attachments


def _is_from_agent(from_header: str) -> bool:
    return parseaddr(from_header)[1].lower() == settings.agent_email.lower()


# --- Inbox --------------------------------------------------------------------


def list_new_message_ids(history_id: int) -> list[str]:
    """Message ids added to INBOX since the last processed history id."""
    last = watch_state.load().get("history_id")
    if last is None:
        # First notification after setup: start tracking from here.
        watch_state.update(history_id=history_id)
        return []
    if history_id <= last:
        return []

    service = gmail_service()
    ids: list[str] = []
    page_token = None
    try:
        while True:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=last,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=page_token,
                )
                .execute()
            )
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    if msg_id not in ids:
                        ids.append(msg_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        logger.warning("History id %s expired; resyncing from %s", last, history_id)

    watch_state.update(history_id=history_id)
    return ids


def search_message_ids(query: str, limit: int = 50) -> list[str]:
    """Ids of messages matching a Gmail search query, oldest first (used by polling mode)."""
    response = gmail_service().users().messages().list(userId="me", q=query, maxResults=limit).execute()
    return [m["id"] for m in reversed(response.get("messages", []))]


def get_sender(message_id: str) -> str:
    msg = (
        gmail_service()
        .users()
        .messages()
        .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From"])
        .execute()
    )
    return _headers(msg["payload"]).get("from", "")


def load_email(message_id: str) -> IncomingEmail | None:
    """Load a message with its thread. Returns None if the thread was already answered."""
    service = gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    thread = service.users().threads().get(userId="me", id=msg["threadId"], format="full").execute()
    thread_messages = thread.get("messages", [])

    if thread_messages and _is_from_agent(_headers(thread_messages[-1]["payload"]).get("from", "")):
        logger.info("Skipping %s: the agent already replied last in thread", message_id)
        return None

    transcript = []
    for item in thread_messages:
        if item["id"] == message_id:
            continue
        headers = _headers(item["payload"])
        transcript.append(f"From: {headers.get('from', '')}\n{extract_text(item['payload'])}")

    headers = _headers(msg["payload"])
    cc = [addr for _, addr in getaddresses([headers.get("cc", "")]) if addr]
    return IncomingEmail(
        id=message_id,
        thread_id=msg["threadId"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        body=extract_text(msg["payload"]),
        thread="\n\n---\n\n".join(transcript),
        message_id_header=headers.get("message-id"),
        cc=cc,
        attachments=extract_attachments(msg["payload"], message_id, service),
    )


# --- Sending ------------------------------------------------------------------


# Gmail drops most <style> rules, so the rendered Markdown gets inline styles.
_INLINE_STYLES = {
    "table": "border-collapse:collapse;margin:12px 0;font-size:14px",
    "th": "border:1px solid #d0d7de;padding:6px 12px;background:#f6f8fa;text-align:left",
    "td": "border:1px solid #d0d7de;padding:6px 12px",
    "pre": "background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 12px;"
    "font-size:12px;line-height:1.45;white-space:pre-wrap",
    "code": "font-family:SFMono-Regular,Consolas,Menlo,monospace",
    "hr": "border:none;border-top:1px solid #d0d7de;margin:18px 0",
}
_TAG = re.compile(r"<(table|th|td|pre|code|hr)(\s[^>]*)?>")


def _inline_styles(markup: str) -> str:
    def add_style(match: re.Match) -> str:
        tag, attrs = match.group(1), match.group(2) or ""
        style = _INLINE_STYLES[tag]
        if 'style="' in attrs:  # e.g. text-align from Markdown table alignment
            return f"<{tag}{attrs.replace('style="', f'style="{style};', 1)}>"
        return f'<{tag} style="{style}"{attrs}>'

    return _TAG.sub(add_style, markup)


_PRE_BLOCK = re.compile(r"(<pre[^>]*>)(.*?)(</pre>)", re.DOTALL)
_DOTTED = re.compile(r"(?<=\w)\.(?=\w)")


def _no_autolink(markup: str) -> str:
    """Gmail turns `c.name` / `c.id` in SQL into links (.name and .id are TLDs); a span breaks the match."""
    return _PRE_BLOCK.sub(lambda m: m.group(1) + _DOTTED.sub("<span>.</span>", m.group(2)) + m.group(3), markup)


def render_markdown(body: str) -> str:
    rendered = _no_autolink(markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists"]))
    return (
        '<html><body><div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#1f2328">'
        f"{_inline_styles(rendered)}</div></body></html>"
    )


def build_mime(to: str, subject: str, body: str, cc: str | None = None) -> EmailMessage:
    """Build a multipart message. Markdown bodies are rendered to HTML with a plain-text part."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc

    if body.lstrip().lower().startswith(("<!doctype", "<html")):
        message.set_content(html_to_text(body))
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
        message.add_alternative(render_markdown(body), subtype="html")
    return message


def reply_subject(subject: str) -> str:
    clean = re.sub(r"^(\s*(re|fwd?)\s*:\s*)+", "", subject or "", flags=re.IGNORECASE).strip()
    return f"Re: {clean}" if clean else "Re: your request"


def _raw(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def send_reply(email: IncomingEmail, body: str) -> bool:
    """Reply in-thread to the sender (and CC). Idempotent per incoming message."""
    key = dedupe.make_key("reply", email.id)
    if not dedupe.claim(key):
        logger.info("Reply to %s already sent, skipping", email.id)
        return False

    agent = settings.agent_email.lower()
    cc = ", ".join(addr for addr in email.cc if addr.lower() != agent) or None
    message = build_mime(email.sender, reply_subject(email.subject), body, cc)
    if email.message_id_header:
        message["In-Reply-To"] = email.message_id_header
        message["References"] = email.message_id_header
    try:
        gmail_service().users().messages().send(
            userId="me", body={"raw": _raw(message), "threadId": email.thread_id}
        ).execute()
    except Exception:
        dedupe.release(key)
        raise
    dedupe.mark_done(key)
    logger.info("Replied to %s in thread %s", email.sender_email, email.thread_id)
    return True


def send_email(to: str, subject: str, body: str, cc: str | None = None) -> str:
    result = (
        gmail_service()
        .users()
        .messages()
        .send(userId="me", body={"raw": _raw(build_mime(to, subject, body, cc))})
        .execute()
    )
    return result["id"]


def create_draft(to: str, subject: str, body: str, cc: str | None = None) -> str:
    result = (
        gmail_service()
        .users()
        .drafts()
        .create(userId="me", body={"message": {"raw": _raw(build_mime(to, subject, body, cc))}})
        .execute()
    )
    return result["id"]


# --- Push notifications -----------------------------------------------------------


def renew_watch() -> None:
    """(Re)register the Pub/Sub watch on INBOX. Gmail watches expire after 7 days."""
    response = (
        gmail_service()
        .users()
        .watch(userId="me", body={"topicName": settings.gmail_pubsub_topic, "labelIds": ["INBOX"]})
        .execute()
    )
    state = watch_state.load()
    values = {"expiration_ms": int(response["expiration"])}
    if state.get("history_id") is None:
        values["history_id"] = int(response["historyId"])
    watch_state.update(**values)
    logger.info("Gmail watch registered on %s", settings.gmail_pubsub_topic)
