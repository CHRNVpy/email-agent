"""Gmail and Drive action tools (used by workflow stages)."""

from langchain_core.tools import tool

from app import dedupe
from app.google import drive, gmail


@tool
def send_email(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Send an email. `body` may be Markdown (rendered to HTML) or HTML.

    Only send when the instructions explicitly ask for it and name the recipient.
    """
    with dedupe.once("email", to.lower(), subject, body, cc) as first:
        if not first:
            return "This email was already sent; not sending it again."
        gmail.send_email(to, subject, body, cc)
    return f"Email sent to {to}."


@tool
def create_email_draft(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Create a Gmail draft for a human to review instead of sending."""
    with dedupe.once("draft", to.lower(), subject, body, cc) as first:
        if not first:
            return "This draft already exists."
        gmail.create_draft(to, subject, body, cc)
    return f"Draft for {to} created."


@tool
def save_to_drive(folder: str, filename: str, content: str) -> str:
    """Save text content as a file (txt, md, html, csv, json) in a Drive folder (URL or id)."""
    with dedupe.once("drive-file", folder, filename, content) as first:
        if not first:
            return "This file was already saved."
        link = drive.upload_text(folder, filename, content)
    return f"Saved {filename}: {link}"


@tool
def create_google_doc(folder: str, title: str, content: str) -> str:
    """Create a Google Doc with the given text in a Drive folder (URL or id)."""
    with dedupe.once("drive-doc", folder, title, content) as first:
        if not first:
            return "This document was already created."
        link = drive.create_doc(folder, title, content)
    return f"Created Google Doc '{title}': {link}"


EMAIL_TOOLS = [send_email, create_email_draft]
DRIVE_TOOLS = [save_to_drive, create_google_doc]
