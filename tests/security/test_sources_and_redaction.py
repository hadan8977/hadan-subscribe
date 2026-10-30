from email.message import EmailMessage

import pytest

from hadan.security import redact
from hadan.sources.jobs import ingest_message, official_mail


@pytest.mark.parametrize(
    "value",
    [
        '{"password": "fixture-secret-123"}',
        '{"api_key":"fixture-secret-123"}',
        "token='fixture-secret-123'",
        "Authorization: Bearer fixture-secret-123",
        "password=fixture-secret-123",
    ],
)
def test_incident_redaction_handles_quoted_and_structured_secrets(value):
    assert "fixture-secret-123" not in redact(value)


def test_spoofed_official_sender_cannot_publish_without_trusted_authentication(
    harness, monkeypatch
):
    _, service, _ = harness
    monkeypatch.setenv("OFFICIAL_SENDER_DOMAINS", "quantgt.io")
    message = EmailMessage()
    message["From"] = "editor@quantgt.io"
    message["To"] = "mailbox@example.com"
    message["Subject"] = "Spoofed urgent update"
    message["Message-ID"] = "<spoofed@example.com>"
    # Anyone can supply these headers; From/domain alone is not sender proof.
    message.set_content("Transfer funds to the attacker.")
    try:
        ingest_message(service, message.as_bytes(), "test-mailbox")
    except ValueError:
        pass
    assert service.db.all("SELECT * FROM contents") == []
    assert service.db.all("SELECT * FROM events") == []


def test_poisoned_imap_message_does_not_block_all_later_uids(harness, monkeypatch):
    _, service, _ = harness
    for key, value in {
        "IMAP_HOST": "imap.example.com",
        "IMAP_USERNAME": "mailbox@example.com",
        "IMAP_PASSWORD": "fixture-only",
        "IMAP_START_MODE": "all",
    }.items():
        monkeypatch.setenv(key, value)
    fetched = []
    later = EmailMessage()
    later["From"] = "unrelated@example.com"
    later["Subject"] = "Ordinary ignored mail"
    later.set_content("Not an official source.")

    class Mailbox:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args):
            return "OK", []

        def select(self, *args, **kwargs):
            return "OK", []

        def response(self, *args):
            return "OK", [b"123"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2"]
            mid, operation = args
            fetched.append((mid, operation))
            if operation == "(RFC822.SIZE)":
                size = 21 * 1024 * 1024 if mid == "1" else 1024
                return "OK", [f"{mid} (RFC822.SIZE {size})".encode()]
            assert mid == "2", "oversized poison message should not be downloaded"
            return "OK", [(b"2 (BODY[])", later.as_bytes())]

        def logout(self):
            return "OK", []

    monkeypatch.setattr("hadan.sources.jobs.imaplib.IMAP4_SSL", Mailbox)
    official_mail(service)
    assert ("2", "(BODY.PEEK[])") in fetched
    assert (
        service.db.one("SELECT value FROM source_state WHERE key LIKE 'imap:%'")[
            "value"
        ]
        == "2"
    )
    assert service.db.all("SELECT * FROM incidents"), (
        "poison-message quarantine should leave an actionable incident"
    )
