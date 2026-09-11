from app.messages import Attachment, IncomingEmail, parse_model_flag, strip_quoted_reply


def test_parse_model_flag_extracts_override():
    assert parse_model_flag("Quarterly report -model:grok-4") == ("Quarterly report", "grok-4")
    assert parse_model_flag("Plain subject") == ("Plain subject", None)


def test_strip_quoted_reply_removes_quote_block():
    body = "Yes, go ahead.\n\nOn Mon, 1 Sep 2025 at 10:00, Agent <agent@example.com> wrote:\n> Shall I send it?"
    assert strip_quoted_reply(body) == "Yes, go ahead."


def test_strip_quoted_reply_drops_prefixed_lines():
    assert strip_quoted_reply("Approved\n> old line\n>> older") == "Approved"


def test_as_prompt_contains_subject_thread_and_attachment_names():
    email = IncomingEmail(
        id="1",
        thread_id="t",
        sender="Alice <alice@example.com>",
        subject="Invoice",
        body="Please check the attached invoice.",
        thread="From: bob@example.com\nEarlier note",
        attachments=[Attachment(filename="inv.pdf", mime_type="application/pdf", data=b"%PDF")],
    )
    prompt = email.as_prompt()
    assert "Subject: Invoice" in prompt
    assert "Earlier note" in prompt
    assert "inv.pdf" in prompt
    assert email.sender_email == "alice@example.com"
