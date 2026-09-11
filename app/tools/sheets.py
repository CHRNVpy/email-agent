"""Google Sheets tools."""

import json

from langchain_core.tools import tool

from app import dedupe
from app.google import sheets
from app.permissions.service import require


@tool
def list_spreadsheets() -> str:
    """List spreadsheets the agent account can access (id and name)."""
    require("sheets:read")
    files = sheets.client().list_spreadsheet_files()
    return json.dumps([{"id": f["id"], "name": f.get("name", "")} for f in files], indent=2)


@tool
def list_worksheets(spreadsheet: str) -> str:
    """List the worksheet tabs of a spreadsheet (URL or id)."""
    require("sheets:read")
    book = sheets.client().open_by_key(sheets.spreadsheet_id(spreadsheet))
    return json.dumps([ws.title for ws in book.worksheets()])


@tool
def read_worksheet(spreadsheet: str, worksheet: str = "Sheet1") -> str:
    """Read all rows of a worksheet. The result maps each header to its column letter
    so you can compute A1 references for updates (e.g. header "Price" -> column "C")."""
    require("sheets:read")
    return json.dumps(sheets.read_records(spreadsheet, worksheet), indent=2, default=str)


@tool
def update_cell(spreadsheet: str, worksheet: str, cell: str, value: str) -> str:
    """Set one cell, addressed in A1 notation (e.g. "C7")."""
    require("sheets:write")
    ws = sheets.client().open_by_key(sheets.spreadsheet_id(spreadsheet)).worksheet(worksheet)
    if str(ws.acell(cell).value or "") == str(value):
        return f"{cell} already contains this value; nothing to do."
    ws.update_acell(cell, value)
    return f"Updated {worksheet}!{cell}."


@tool
def append_row(spreadsheet: str, worksheet: str, values: list[str]) -> str:
    """Append one row to the end of a worksheet. `values` are ordered like the header row."""
    require("sheets:write")
    with dedupe.once("sheet-append", spreadsheet, worksheet, values) as first:
        if not first:
            return "This exact row was already appended; not appending again."
        ws = sheets.client().open_by_key(sheets.spreadsheet_id(spreadsheet)).worksheet(worksheet)
        ws.append_row(values, value_input_option="USER_ENTERED")
    return f"Appended a row with {len(values)} values to {worksheet}."


SHEETS_TOOLS = [list_spreadsheets, list_worksheets, read_worksheet, update_cell, append_row]
