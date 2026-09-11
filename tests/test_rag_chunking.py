from app.config import RagSqlTable
from app.rag.documents import Document, chunk_document, chunk_text
from app.rag.indexers import row_to_document


def test_short_text_is_single_chunk():
    assert chunk_text("hello", 100, 10) == ["hello"]
    assert chunk_text("   ", 100, 10) == []


def test_chunks_respect_size_and_prefer_sentence_breaks():
    text = " ".join(f"Sentence number {i} ends here." for i in range(60))
    chunks = chunk_text(text, 200, 40)
    assert len(chunks) > 5
    assert all(len(c) <= 200 for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])
    # Consecutive chunks overlap so that facts on a boundary are not lost.
    assert chunks[0][-20:] in chunks[1] or chunks[1][:20] in chunks[0]


def test_chunk_ids_are_stable_and_unique():
    doc = Document(source="drive", source_id="f1", title="Plan", text="word " * 600)
    first, second = chunk_document(doc, 500, 50), chunk_document(doc, 500, 50)
    assert [c.id for c in first] == [c.id for c in second]
    assert len({c.id for c in first}) == len(first)
    assert first[0].text.startswith("Plan\n\n")
    assert first[0].payload["source_id"] == "f1" and first[0].payload["chunk_total"] == len(first)


def test_sql_row_becomes_document():
    spec = RagSqlTable(
        database="crm", table="articles", text_columns=["title", "body"], title_column="title", date_column="published"
    )
    from datetime import datetime

    doc = row_to_document(spec, {"id": 7, "title": "Launch", "body": "Details", "published": datetime(2025, 5, 1)})
    assert doc.source == "sql:crm.articles" and doc.source_id == "7"
    assert doc.text == "Launch\n\nDetails"
    assert doc.date == "2025-05-01T00:00:00Z"
