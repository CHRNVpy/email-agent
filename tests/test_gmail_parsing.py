import base64

from app.google.gmail import build_mime, extract_attachments, extract_text, reply_subject


def b64(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return base64.urlsafe_b64encode(data).decode()


def test_extract_text_prefers_plain_part():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("Plain version")}},
        ],
    }
    assert extract_text(payload) == "Plain version"


def test_extract_text_falls_back_to_html_in_nested_parts():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/html", "body": {"data": b64("<div>Hello<br>world &amp; co</div>")}}],
            }
        ],
    }
    assert extract_text(payload) == "Hello\nworld & co"


def test_extract_attachments_reads_inline_data():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("see attached")}},
            {"mimeType": "text/csv", "filename": "data.csv", "body": {"data": b64("a,b\n1,2")}},
        ],
    }
    [attachment] = extract_attachments(payload, "msg-1", service=None)
    assert attachment.filename == "data.csv"
    assert attachment.data == b"a,b\n1,2"


def test_reply_subject_normalises_prefixes():
    assert reply_subject("Re: RE: Fwd: Budget") == "Re: Budget"
    assert reply_subject("") == "Re: your request"


def test_build_mime_renders_markdown_alternative():
    message = build_mime("bob@example.com", "Report", "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    html = message.get_body(preferencelist=("html",)).get_content()
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "<h1>Title</h1>" in html and "<table style=" in html
    assert "# Title" in plain


def test_email_html_has_inline_table_and_code_styles():
    body = "| Customer | Revenue |\n|:--|--:|\n| Acme | $412,300 |\n\n```sql\nSELECT 1\n```"
    html = build_mime("bob@example.com", "Report", body).get_body(preferencelist=("html",)).get_content()
    assert '<table style="border-collapse:collapse' in html
    assert 'style="border:1px solid #d0d7de;padding:6px 12px;text-align: right;"' in html  # merged with alignment
    assert '<pre style="background:#f6f8fa' in html and "SELECT 1" in html


def test_sql_identifiers_are_not_autolinked():
    from app.google.gmail import render_markdown

    html = render_markdown("See `c.name`\n\n```sql\nSELECT c.name, c.id FROM customers AS c\n```")
    assert "c<span>.</span>name" in html and "c<span>.</span>id" in html
    assert ">c.name</code>" in html  # inline code outside <pre> is untouched
