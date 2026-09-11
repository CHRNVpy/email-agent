# Workflows

Workflows are repeatable, multi-stage AI processes that non-developers define in a Google
Sheet. Each row is a workflow; an email whose **subject contains the workflow name** starts it.

## Sheet format

| Column | Required | Meaning |
|---|---|---|
| `workflow` | yes | Name. Matched case-insensitively against the email subject; the longest match wins. |
| `model` | no | LLM override for this workflow, e.g. `gemini-2.5-pro` or `grok-4`. |
| `actions` | no | Tool groups available to every stage: `email`, `drive`, `sheets`, `finance`, `sql`. Default: `email, drive`. |
| `source_<name>` | no | Inputs, referenced in prompts as `{source_<name>}` (see below). Any number of columns. |
| `stage_1`, `stage_2`, … | yes | Prompt templates, executed in numeric order. Empty cells are skipped. |

### Sources

The value of a `source_*` cell decides how it is loaded:

| Value | Loaded as |
|---|---|
| Google Drive **folder** URL | Text of every Doc, Sheet and PDF in the folder tree (PDFs via Gemini) |
| Google **Doc** URL | Document text |
| Google **Sheet** URL | Spreadsheet id + all worksheets as text (so stages can also update it) |
| `query: <text>` (or a `source_knowledge*` column) | Top knowledge-base (RAG) matches for the query |
| anything else | Used verbatim |

Prefix a value with `iteration=TRUE;` to **iterate**: every stage that references this source
runs once per item — once per document for a folder, once per line for text.

### Stages

* The whole conversation (earlier prompts and answers) is passed to each stage, so later
  stages can say "now shorten that" or "turn the analysis above into an email".
* Stages can call the tools enabled in `actions` — e.g. *"Email the final report to
  ops@company.com"* or *"Save it as a Google Doc in https://drive.google.com/drive/folders/…"*.
  Side effects are deduplicated per stage, so retries never send two emails.
* `{await_reply}` anywhere in a stage turns it into an **approval / question step**: the stage
  text is emailed to the user and the run pauses. When the user replies in the same thread, that
  stage runs with their answer appended and the workflow continues.

## Example

See [`examples/workflows.csv`](../examples/workflows.csv) — import it into a Google Sheet and
set `WORKFLOW_SHEET_ID`.

**Weekly Market Brief**

1. `stage_1` — *"Summarise the main themes in the research notes: {source_research}"*
2. `stage_2` — runs once per ticker in `source_tickers` (`iteration=TRUE; NVDA⏎AMD⏎TSM`)
3. `stage_3` — *"Here is the draft outline. Reply with changes or 'OK'. {await_reply}"* → pauses
4. `stage_4` — *"Write the final brief in the style of {source_style} and email it to …"*

## Operations

```bash
python -m app.cli workflows      # validate the sheet: lists parsed workflows, sources, stages
```

Runs are stored in the app database (`workflow_runs` table) with their full state, which is
what makes pause/resume and auditing possible.
