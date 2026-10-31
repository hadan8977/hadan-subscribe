"""Real local DKIM signatures; only DNS is replaced, no external mailbox/network."""

import base64
from email.message import EmailMessage
from email import policy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hadan.config import Settings
from hadan.service import Service
from hadan.sources.jobs import authenticated_sender, ingest_message

dkim = pytest.importorskip(
    "dkim", reason="Install the sources extra for DKIM integration tests"
)


@pytest.fixture
def signer(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(public)
    real_verify = dkim.verify
    monkeypatch.setattr(
        dkim,
        "verify",
        lambda raw, timeout=8: real_verify(
            raw, dnsfunc=lambda *_a, **_k: record, timeout=timeout
        ),
    )

    def sign(*, domain=b"quantgt.io", length=False):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "Updates <updates@quantgt.io>"
        message["To"] = "archive@example.invalid"
        message["Subject"] = "Synthetic signed update"
        message["Message-ID"] = "<local-signature-fixture@example.invalid>"
        message.set_content("Synthetic official body")
        message.add_attachment(
            b"private attachment fixture",
            maintype="application",
            subtype="octet-stream",
            filename="report.txt",
        )
        raw = message.as_bytes()
        signature = dkim.sign(
            raw,
            b"fixture",
            domain,
            pem,
            include_headers=[b"from", b"to", b"subject", b"message-id", b"content-type", b"mime-version"],
            length=length,
        )
        return signature + raw

    return sign


def test_real_aligned_signature_ingests_body_and_attachment(tmp_path, signer):
    service = Service(Settings(data_dir=tmp_path, secret="signature-fixture-" * 4))
    service.seed()
    raw = signer()
    assert authenticated_sender(raw, "updates@quantgt.io")
    result = ingest_message(service, raw, "fixture-mailbox:1")
    assert result["version"] == 1
    assert (
        service.db.one("SELECT body FROM versions")["body"].strip()
        == "Synthetic official body"
    )
    assert service.db.one("SELECT name FROM attachments")["name"] == "report.txt"


def test_real_signature_rejects_body_tamper_unaligned_domain_and_partial_body(signer):
    raw = signer()
    assert not authenticated_sender(
        raw.replace(b"Synthetic official body", b"Tampered official body"),
        "updates@quantgt.io",
    )
    unaligned = signer(domain=b"attacker.example")
    assert dkim.verify(unaligned)
    assert not authenticated_sender(unaligned, "updates@quantgt.io")
    partial = signer(length=True)
    assert dkim.verify(partial)
    assert not authenticated_sender(partial, "updates@quantgt.io")
