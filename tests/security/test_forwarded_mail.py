"""Offline forwarded-original/curator authorization and mailbox cutover cases."""
import base64
import json
from email import policy
from email.message import EmailMessage

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hadan.sources import inbound, jobs

dkim = pytest.importorskip("dkim", reason="Install sources extra for real DKIM tests")
OWNER = "owner@example.test"
READER = "reader@example.test"
ALIAS = "alias@example.test"


@pytest.fixture
def signing(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(public)
    verify = dkim.DKIM.verify
    monkeypatch.setattr(dkim.DKIM, "verify", lambda self, idx=0, dnsfunc=None: verify(self, idx=idx, dnsfunc=lambda *_args, **_kwargs: record))
    monkeypatch.setenv("OFFICIAL_SENDER_DOMAINS", "quantgt.io")
    monkeypatch.setenv("INBOUND_CURATORS", OWNER)
    monkeypatch.setenv("INBOUND_ADDRESS", ALIAS)

    def sign(*, sender="updates@quantgt.io", recipient=ALIAS, subject="Original official update", body="Verified original body", files=True, headers=None, length=False, domain=None, raw=None):
        if raw is None:
            message = EmailMessage(policy=policy.SMTP)
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = subject
            message["Message-ID"] = "<fixture-original@" + sender.split("@")[1] + ">"
            message.set_content(body)
            if files:
                message.add_attachment(b"authenticated report", maintype="application", subtype="octet-stream", filename="report.xlsx")
            raw = message.as_bytes()
        included = headers or [b"from", b"to", b"subject", b"message-id", b"content-type", b"mime-version", b"content-transfer-encoding"]
        signature = dkim.sign(raw, b"fixture", (domain or sender.split("@")[1]).encode(), pem, include_headers=included, canonicalize=(b"simple", b"simple"), length=length)
        return signature + raw

    return sign


def wrapper(original, *, identifier="one", eml=False, envelope_sender=READER):
    attachment = (b"Content-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=original.eml\r\nContent-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(original)) if eml else b"Content-Type: message/rfc822\r\nContent-Disposition: attachment; filename=original.eml\r\n\r\n" + original
    return (f"From: {envelope_sender}\r\nTo: {ALIAS}\r\nSubject: Outer forwarding {identifier}\r\nMessage-ID: <wrapper-{identifier}@example.test>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=fixture-boundary\r\n\r\n".encode()
        + b"--fixture-boundary\r\nContent-Type: text/plain\r\n\r\nUNVERIFIED OUTER COMMENT\r\n"
        + b"--fixture-boundary\r\n" + attachment + b"\r\n"
        + b"--fixture-boundary\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=unverified.exe\r\n\r\nUNVERIFIED OUTER ATTACHMENT\r\n"
        + b"--fixture-boundary--\r\n")


@pytest.mark.parametrize("eml", [False, True])
def test_forwarded_original_preserves_raw_signature_and_excludes_outer_content(harness, signing, eml):
    _, service, _ = harness
    original = signing()
    envelope = wrapper(original, eml=eml)
    selected = inbound.select_messages(envelope, {"quantgt.io"}, {OWNER}, ALIAS)
    assert len(selected) == 1 and selected[0].raw == original
    assert dkim.verify(selected[0].raw)
    result = jobs.ingest_message(service, envelope, "fixture-mailbox")
    version = service.db.one("SELECT * FROM versions WHERE content_id=?", (result["id"],))
    assert "Verified original body" in version["body"]
    assert "UNVERIFIED OUTER" not in version["body"]
    assert "管理员转发" not in version["title"]
    assert [x["name"] for x in service.db.all("SELECT name FROM attachments")] == ["report.xlsx"]
    assert service.db.one("SELECT action FROM audit WHERE actor='inbound'")["action"] == "official_verified"


def test_repeated_wrappers_and_mailbox_id_changes_do_not_duplicate_original(harness, signing):
    _, service, _ = harness
    original = signing()
    first = jobs.ingest_message(service, wrapper(original, identifier="one"), "mailbox-old-validity")
    second = jobs.ingest_message(service, wrapper(original, identifier="two", eml=True), "mailbox-new-validity")
    assert second["id"] == first["id"] and second["unchanged"] is True
    assert len(service.db.all("SELECT * FROM events")) == 1
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")) == 1


def test_later_valid_aligned_signature_is_checked_on_unchanged_original(signing):
    original = signing()
    with_extra_signature = signing(raw=original, domain="relay.example")
    proof = jobs.authenticated_sender(with_extra_signature, "updates@quantgt.io")
    assert proof and proof.index == 1 and proof.domain == "quantgt.io"


def test_invalid_aligned_signature_does_not_hide_later_valid_signature(signing):
    original = signing()
    twice_signed = signing(raw=original)
    invalid_first = twice_signed[:-len(original)].replace(b"b=", b"b=A", 1)
    proof = jobs.authenticated_sender(invalid_first + original, "updates@quantgt.io")
    assert proof and proof.index == 1


def test_invalid_inner_does_not_become_official_under_valid_owner_outer(harness, signing):
    _, service, _ = harness
    changed = signing().replace(b"Verified original body", b"Tampered original body")
    envelope = signing(sender=OWNER, raw=wrapper(changed, envelope_sender=OWNER))
    assert dkim.verify(envelope)
    result = jobs.ingest_message(service, envelope, "fixture-mailbox")
    assert result["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


def test_owner_curated_forward_is_distinct_and_audited(harness, signing):
    _, service, _ = harness
    raw = signing(sender=OWNER, body="Owner comment\nForwarded message\nFrom: updates@quantgt.io", files=False)
    result = jobs.ingest_message(service, raw, "fixture-mailbox")
    version = service.db.one("SELECT * FROM versions WHERE content_id=?", (result["id"],))
    assert version["title"].startswith("管理员转发 · ")
    assert version["body"].startswith("管理员转发（未独立核验官方原文）")
    assert service.db.one("SELECT action FROM audit WHERE actor='inbound'")["action"] == "owner_curated"
    metadata = json.loads(service.db.one("SELECT value FROM source_state WHERE key LIKE 'inbound:%'")["value"])
    assert metadata["mode"] == "owner_curated" and metadata["sender"] == OWNER


def test_reader_does_not_acquire_curator_authority(harness, signing):
    _, service, _ = harness
    raw = signing(sender=READER, files=False, body="From: updates@quantgt.io\nQuoted content")
    assert dkim.verify(raw)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["ignored"] is True
    assert service.db.all("SELECT * FROM contents") == []


@pytest.mark.parametrize("missing", [b"to", b"subject"])
def test_curator_signature_must_cover_alias_and_subject(harness, signing, missing):
    _, service, _ = harness
    raw = signing(sender=OWNER, recipient="another@example.com" if missing == b"to" else ALIAS, files=False, headers=[h for h in [b"from", b"to", b"subject", b"message-id"] if h != missing])
    if missing == b"to":
        raw = raw.replace(b"To: another@example.com", ("To: " + ALIAS).encode())
    assert dkim.verify(raw)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


def test_duplicate_alias_header_cannot_redirect_a_private_signed_message(harness, signing):
    _, service, _ = harness
    raw = signing(sender=OWNER, recipient="private@example.com", files=False)
    changed = ("To: " + ALIAS + "\r\n").encode() + raw
    assert dkim.verify(changed)
    assert jobs.ingest_message(service, changed, "fixture-mailbox")["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


@pytest.mark.parametrize("recipient", ["other@example.com", "alias@example.test.attacker.test", "owner@example.test"])
def test_curator_requires_exact_intentional_alias(harness, signing, recipient):
    _, service, _ = harness
    raw = signing(sender=OWNER, recipient=recipient, files=False)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["quarantined"] is True


def test_quoted_from_alone_never_authenticates_a_forward(harness, signing):
    _, service, _ = harness
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = OWNER
    message["To"] = ALIAS
    message["Subject"] = "Forged quoted official"
    message.set_content("From: updates@quantgt.io\nOfficial-looking unverified content")
    assert jobs.ingest_message(service, message.as_bytes(), "fixture-mailbox")["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


def test_curated_mode_is_disabled_without_explicit_curators(harness, signing, monkeypatch):
    _, service, _ = harness
    monkeypatch.delenv("INBOUND_CURATORS", raising=False)
    raw = signing(sender=OWNER, files=False)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["ignored"] is True
    assert service.db.all("SELECT * FROM contents") == []


def test_signed_absence_of_subject_is_not_a_signed_subject(harness, signing):
    _, service, _ = harness
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = OWNER
    message["To"] = ALIAS
    message.set_content("Forwarded content")
    raw = signing(sender=OWNER, raw=message.as_bytes(), headers=[b"from", b"to", b"subject"])
    assert dkim.verify(raw)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["quarantined"] is True


@pytest.mark.parametrize("header", [b"Subject: Altered subject\r\n", b"Message-ID: <injected@example.com>\r\n", b"From: attacker@example.com\r\n", b"Content-Type: text/plain\r\n"])
def test_duplicate_interpretation_headers_are_rejected(harness, signing, header):
    _, service, _ = harness
    raw = header + signing(sender=OWNER)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


def test_curated_unsigned_message_id_cannot_replay_the_same_signed_body(harness, signing):
    _, service, _ = harness
    raw = signing(sender=OWNER, files=False, headers=[b"from", b"to", b"subject", b"content-type", b"mime-version", b"content-transfer-encoding"])
    first = jobs.ingest_message(service, raw, "fixture-mailbox")
    changed = raw.replace(b"<fixture-original@example.test>", b"<attacker-replay@example.test>")
    second = jobs.ingest_message(service, changed, "fixture-mailbox")
    assert second["id"] == first["id"] and second["unchanged"] is True
    assert len(service.db.all("SELECT * FROM events")) == 1


def test_partial_body_signature_cannot_authorize_curated_forward(harness, signing):
    _, service, _ = harness
    raw = signing(sender=OWNER, files=False, length=True)
    assert dkim.verify(raw)
    assert jobs.ingest_message(service, raw, "fixture-mailbox")["quarantined"] is True


def test_nested_and_oversized_forwarding_is_bounded(harness, signing):
    _, service, _ = harness
    nested = signing()
    for index in range(inbound.MAX_DEPTH + 2):
        nested = wrapper(nested, identifier=str(index), eml=True)
    assert jobs.ingest_message(service, nested, "fixture-mailbox")["quarantined"] is True
    assert jobs.ingest_message(service, b"X" * (inbound.MAX_BYTES + 1), "fixture-mailbox")["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []


@pytest.fixture
def mailbox(monkeypatch):
    calls = []
    control = {"next": 11, "validity": 53, "uids": b"11", "size_error": False}
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "unrelated@example.com"
    message["Subject"] = "Ignored unrelated mailbox message"
    message.set_content("private unrelated text")
    raw = message.as_bytes()
    for key, value in {"IMAP_HOST": "imap.example.com", "IMAP_USERNAME": "fixture@example.com", "IMAP_PASSWORD": "fixture-only", "IMAP_MAILBOX": "INBOX", "IMAP_SEARCH": 'TO "' + ALIAS + '"'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IMAP_START_MODE", raising=False)

    class Mailbox:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *args):
            return "OK", []
        def select(self, folder, readonly=False):
            assert readonly is True
            calls.append(("select", folder, readonly))
            return "OK", []
        def response(self, name):
            return name, [str(control["next" if name == "UIDNEXT" else "validity"]).encode()] if control.get("next") is not None else [None]
        def uid(self, command, *args):
            calls.append((command, *args))
            if command == "search":
                return "OK", [control["uids"]]
            uid, operation = args
            if operation == "(RFC822.SIZE)":
                return ("NO", []) if control["size_error"] else ("OK", [f"{uid} (RFC822.SIZE {len(raw)})".encode()])
            assert operation == "(BODY.PEEK[])"
            return "OK", [(b"fixture", raw)]
        def logout(self):
            calls.append(("logout",))
            return "OK", []
    monkeypatch.setattr(jobs.imaplib, "IMAP4_SSL", Mailbox)
    return calls, control


def test_first_mailbox_activation_checkpoints_without_fetching_history(harness, mailbox):
    _, service, _ = harness
    calls, control = mailbox
    result = jobs.official_mail(service)
    assert result["processed"] == 0 and result["baseline_uid"] == 10
    assert not any(call[0] in ("search", "fetch") for call in calls)
    calls.clear()
    result = jobs.official_mail(service)
    assert result["processed"] == 1
    assert ("search", None, "UID", "11:*", 'TO "' + ALIAS + '"') in calls
    assert any(call[-1] == "(BODY.PEEK[])" for call in calls)
    assert service.db.all("SELECT * FROM contents") == []


def test_explicit_cutover_helper_preserves_existing_checkpoint(harness, mailbox):
    _, service, _ = harness
    calls, control = mailbox
    assert jobs.cutover_mailbox(service)["baseline_uid"] == 10
    control["next"] = 99
    result = jobs.cutover_mailbox(service)
    assert result["baseline_uid"] == 10 and result["initialized"] is False
    assert not any(call[0] in ("search", "fetch") for call in calls)
    assert not any(name in result for name in ("username", "password", "host"))


def test_mailbox_generation_change_rebaselines_without_historical_import(harness, mailbox):
    _, service, _ = harness
    calls, control = mailbox
    assert jobs.cutover_mailbox(service)["baseline_uid"] == 10
    calls.clear()
    control["validity"] = 77
    control["next"] = 29
    result = jobs.official_mail(service)
    assert result["initialized"] is True and result["baseline_uid"] == 28
    assert not any(call[0] in ("search", "fetch") for call in calls)


def test_missing_mailbox_checkpoint_metadata_does_not_import_history(harness, mailbox):
    _, service, _ = harness
    calls, control = mailbox
    control["next"] = None
    with pytest.raises(RuntimeError, match="unavailable"):
        jobs.official_mail(service)
    assert not any(call[0] in ("search", "fetch") for call in calls)
    assert service.db.all("SELECT * FROM source_state WHERE key LIKE 'imap:%'") == []


def test_cutover_race_preserves_cursor_committed_while_reading_uidnext(harness, mailbox, monkeypatch):
    _, service, _ = harness
    calls, _ = mailbox
    original = jobs._imap_number

    def racing_number(client, name):
        if name == "UIDNEXT":
            with jobs.inbound_mailbox() as (_, _, key, _):
                jobs.save(service, key, 100)
        return original(client, name)

    monkeypatch.setattr(jobs, "_imap_number", racing_number)
    result = jobs.cutover_mailbox(service)
    assert result["baseline_uid"] == 100 and result["initialized"] is False
    assert not any(call[0] in ("search", "fetch") for call in calls)


def test_historical_import_requires_explicit_all_mode(harness, mailbox, monkeypatch):
    _, service, _ = harness
    calls, _ = mailbox
    monkeypatch.setenv("IMAP_START_MODE", "all")
    assert jobs.official_mail(service)["processed"] == 1
    assert ("search", None, "UID", "1:*", 'TO "' + ALIAS + '"') in calls


def test_transient_imap_size_error_does_not_discard_message(harness, mailbox, monkeypatch):
    _, service, _ = harness
    _, control = mailbox
    control["size_error"] = True
    monkeypatch.setenv("IMAP_START_MODE", "all")
    with pytest.raises(RuntimeError, match="size fetch"):
        jobs.official_mail(service)
    assert service.db.one("SELECT value FROM source_state WHERE key LIKE 'imap:%'")["value"] == "0"


def test_search_command_injection_is_rejected(harness, mailbox, monkeypatch):
    _, service, _ = harness
    calls, _ = mailbox
    monkeypatch.setenv("IMAP_START_MODE", "all")
    monkeypatch.setenv("IMAP_SEARCH", "ALL\r\nSTORE 1 +FLAGS \\Deleted")
    with pytest.raises(ValueError, match="IMAP_SEARCH"):
        jobs.official_mail(service)
    assert not any(call[0] in ("search", "fetch") for call in calls)


def test_unsigned_subject_uses_generic_title_not_attacker_subject(harness, signing):
    _, service, _ = harness
    raw = signing(
        subject="Untrusted replacement subject",
        body="Verified official body",
        files=False,
        headers=[
            b"from",
            b"to",
            b"message-id",
            b"content-type",
            b"mime-version",
            b"content-transfer-encoding",
        ],
    )
    result = jobs.ingest_message(service, raw, "fixture-mailbox")
    version = service.db.one("SELECT * FROM versions WHERE content_id=?", (result["id"],))
    assert version["title"] == "Quant GT · 官方邮件更新"
    assert "Untrusted replacement subject" not in version["title"]
    assert "Verified official body" in version["body"]


def test_unsigned_content_type_or_transfer_encoding_is_quarantined(harness, signing):
    _, service, _ = harness
    missing_content_type = signing(
        body="Verified official body",
        files=False,
        headers=[b"from", b"to", b"subject", b"mime-version", b"content-transfer-encoding"],
    )
    result = jobs.ingest_message(service, missing_content_type, "fixture-mailbox")
    assert result["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []

    missing_cte = signing(
        body="Verified official body",
        files=False,
        headers=[b"from", b"to", b"subject", b"content-type", b"mime-version"],
    )
    result = jobs.ingest_message(service, missing_cte, "fixture-mailbox")
    assert result["quarantined"] is True
    assert service.db.all("SELECT * FROM contents") == []
