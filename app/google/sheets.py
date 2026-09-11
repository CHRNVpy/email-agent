"""Google Sheets helpers (gspread)."""

import re

import gspread

from app.google.auth import get_credentials

_SHEET_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def client() -> gspread.Client:
    return gspread.authorize(get_credentials())


def spreadsheet_id(url_or_id: str) -> str:
    match = _SHEET_URL.search(url_or_id)
    return match.group(1) if match else url_or_id.strip()


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def read_records(sheet_id: str, tab: str) -> dict:
    """Rows as dicts plus a header -> column-letter map, so an LLM can address cells."""
    worksheet = client().open_by_key(spreadsheet_id(sheet_id)).worksheet(tab)
    headers = worksheet.row_values(1)
    return {
        "headers": {h.strip(): column_letter(i) for i, h in enumerate(headers) if h.strip()},
        "rows": worksheet.get_all_records(),
    }


def export_text(sheet_id: str) -> str:
    """All worksheets rendered as pipe-separated text (used as LLM context)."""
    book = client().open_by_key(spreadsheet_id(sheet_id))
    parts = []
    for worksheet in book.worksheets():
        parts.append(f"=== Sheet: {worksheet.title} ===")
        rows = worksheet.get_all_values()
        parts.extend(" | ".join(map(str, row)) for row in rows)
        if not rows:
            parts.append("(empty)")
        parts.append("")
    return "\n".join(parts)
