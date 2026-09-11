from app.workflows.models import SourceKind
from app.workflows.sheet import match_subject, parse_row, parse_rows

ROW = {
    "workflow": "Weekly Market Brief",
    "model": "",
    "actions": "email, sheets",
    "source_research": "https://drive.google.com/drive/folders/FOLDER123",
    "source_style": "https://docs.google.com/document/d/DOC456/edit",
    "source_knowledge": "semiconductor supply chain news",
    "source_tickers": "iteration=TRUE; NVDA\nAMD\nTSM",
    "stage_1": "Summarise the themes in {source_research}",
    "stage_2": "Write the brief in this style: {source_style}. {await_reply}",
    "stage_3": "",
    "stage_10": "Email the final brief to ops@example.com",
}


def test_parse_row_detects_sources_and_stages():
    config = parse_row(ROW)
    assert config.name == "Weekly Market Brief"
    assert config.model is None
    assert config.actions == ["email", "sheets"]
    assert config.sources["source_research"].kind == SourceKind.DRIVE_FOLDER
    assert config.sources["source_style"].kind == SourceKind.GOOGLE_DOC
    assert config.sources["source_knowledge"].kind == SourceKind.KNOWLEDGE_QUERY

    tickers = config.sources["source_tickers"]
    assert tickers.kind == SourceKind.TEXT and tickers.iterate
    assert tickers.value == "NVDA\nAMD\nTSM"
    assert config.iteration_source == "source_tickers"

    assert [s.number for s in config.stages] == [1, 2, 10]  # empty stage skipped, numeric order
    assert config.stages[1].awaits_reply


def test_query_syntax_in_any_source_column():
    config = parse_row({"workflow": "X", "source_notes": 'query: "Q4 revenue guidance"', "stage_1": "{source_notes}"})
    source = config.sources["source_notes"]
    assert source.kind == SourceKind.KNOWLEDGE_QUERY
    assert source.value == "Q4 revenue guidance"


def test_legacy_task_column_and_rows_without_stages():
    workflows = parse_rows([{"task": "Legacy", "stage_1": "Do it"}, {"workflow": "No stages"}, {"workflow": ""}])
    assert [w.name for w in workflows] == ["Legacy"]


def test_match_subject_prefers_longest_name():
    workflows = parse_rows([{"workflow": "Brief", "stage_1": "a"}, {"workflow": "Weekly Market Brief", "stage_1": "b"}])
    assert match_subject("Re: weekly market brief for Monday", workflows).name == "Weekly Market Brief"
    assert match_subject("Brief update", workflows).name == "Brief"
    assert match_subject("Lunch?", workflows) is None


def test_example_sheet_parses():
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(Path(__file__).parents[1].joinpath("examples/workflows.csv").open()))
    workflows = {w.name: w for w in parse_rows(rows)}
    assert set(workflows) == {"Weekly Market Brief", "Lead Enrichment", "FAQ Draft"}

    brief = workflows["Weekly Market Brief"]
    assert brief.iteration_source == "source_tickers"
    assert [s.awaits_reply for s in brief.stages] == [False, False, True, False]
    assert workflows["Lead Enrichment"].iteration_source == "source_companies"
    assert workflows["FAQ Draft"].sources["source_kb"].kind == SourceKind.KNOWLEDGE_QUERY
    assert workflows["FAQ Draft"].model == "gemini-2.5-pro"
