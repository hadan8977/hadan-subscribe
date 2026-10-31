import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from hadan.config import Settings
from hadan.service import Problem, Service
from hadan.ses_feedback import SesFeedback, _canonical
from hadan.transport import Transport, provider_message_id

TOPIC = "arn:aws:sns:us-east-1:123456789012:hadan-quant-feedback"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"


def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def envelope(kind, message, key, *, subject=None):
    value = {
        "Type": kind,
        "MessageId": "sns-" + uuid.uuid4().hex,
        "TopicArn": TOPIC,
        "Message": json.dumps(message, ensure_ascii=False),
        "Timestamp": "2026-10-30T07:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": CERT_URL,
    }
    if subject is not None:
        value["Subject"] = subject
    if kind == "SubscriptionConfirmation":
        value["Token"] = "token-123"
        value["SubscribeURL"] = (
            "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=token-123"
        )
    signature = key.sign(
        _canonical(value).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
    )
    value["Signature"] = base64.b64encode(signature).decode()
    return value


def test_canonical_string_uses_named_fields():
    confirmation = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m-1",
        "Message": "hello",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
        "Timestamp": "2026-10-30T07:00:00.000Z",
        "Token": "t-1",
        "TopicArn": TOPIC,
    }
    assert _canonical(confirmation) == (
        "Message\nhello\n"
        "MessageId\nm-1\n"
        "SubscribeURL\nhttps://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription\n"
        "Timestamp\n2026-10-30T07:00:00.000Z\n"
        "Token\nt-1\n"
        f"TopicArn\n{TOPIC}\n"
        "Type\nSubscriptionConfirmation\n"
    )
    notification = {
        "Type": "Notification",
        "MessageId": "m-2",
        "Message": "bye",
        "Subject": "subject",
        "Timestamp": "2026-10-30T07:00:00.000Z",
        "TopicArn": TOPIC,
    }
    assert _canonical(notification) == (
        "Message\nbye\n"
        "MessageId\nm-2\n"
        "Subject\nsubject\n"
        "Timestamp\n2026-10-30T07:00:00.000Z\n"
        f"TopicArn\n{TOPIC}\n"
        "Type\nNotification\n"
    )


@pytest.fixture
def feedback(tmp_path):
    key, cert = keypair()
    settings = Settings(
        data_dir=tmp_path,
        secret="s" * 64,
        admin_emails=("owner@example.com",),
        mail_backend="file",
        allowed_emails=("u@example.com",),
        ses_feedback_enabled=True,
        ses_sns_topic_arn=TOPIC,
    )
    service = Service(settings)
    confirmed = []

    def confirm(url):
        confirmed.append(url)
        return httpx.Response(200)

    return (
        SesFeedback(
            service,
            cert_fetcher=lambda _url: cert,
            confirm_get=confirm,
        ),
        service,
        key,
        confirmed,
    )


def test_permanent_bounce_suppresses_once(feedback):
    ses, service, key, _ = feedback
    value = envelope(
        "Notification",
        {
            "eventType": "Bounce",
            "mail": {"messageId": "ses-1", "destination": ["u@example.com"]},
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "u@example.com"}],
            },
        },
        key,
    )
    raw = json.dumps(value).encode()
    first = ses.handle(raw)
    assert first["suppressed"] == 1
    assert service.db.one(
        "SELECT reason FROM suppressions WHERE email='u@example.com'"
    )["reason"] == "bounce"
    incidents = service.db.all("SELECT * FROM incidents WHERE kind='ses_feedback'")
    assert len(incidents) == 1
    second = ses.handle(raw)
    assert second["duplicate"] is True
    assert len(service.db.all("SELECT * FROM incidents WHERE kind='ses_feedback'")) == 1


def test_transient_bounce_opens_incident_without_suppression(feedback):
    ses, service, key, _ = feedback
    value = envelope(
        "Notification",
        {
            "eventType": "Bounce",
            "mail": {"messageId": "ses-2", "destination": ["u@example.com"]},
            "bounce": {
                "bounceType": "Transient",
                "bouncedRecipients": [{"emailAddress": "u@example.com"}],
            },
        },
        key,
    )
    result = ses.handle(json.dumps(value).encode())
    assert result["suppressed"] == 0
    assert service.db.one("SELECT email FROM suppressions WHERE email='u@example.com'") is None
    assert service.db.one("SELECT id FROM incidents WHERE kind='ses_feedback'") is not None


def test_complaint_suppresses(feedback):
    ses, service, key, _ = feedback
    value = envelope(
        "Notification",
        {
            "eventType": "Complaint",
            "mail": {"messageId": "ses-3", "destination": ["u@example.com"]},
            "complaint": {
                "complainedRecipients": [{"emailAddress": "u@example.com"}]
            },
        },
        key,
    )
    result = ses.handle(json.dumps(value).encode())
    assert result["suppressed"] == 1
    assert service.db.one(
        "SELECT reason FROM suppressions WHERE email='u@example.com'"
    )["reason"] == "complaint"


def test_subscription_confirmation_verifies_and_calls_url(feedback):
    ses, _service, key, confirmed = feedback
    value = envelope("SubscriptionConfirmation", {}, key)
    result = ses.handle(json.dumps(value).encode())
    assert result["type"] == "SubscriptionConfirmation"
    assert confirmed == [value["SubscribeURL"]]


def test_invalid_signature_rejected(feedback):
    ses, _service, key, _ = feedback
    value = envelope("Notification", {"eventType": "Delivery"}, key)
    value["Signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(Problem) as caught:
        ses.handle(json.dumps(value).encode())
    assert caught.value.status == 403


def test_otp_request_is_blocked_for_suppressed_mailbox(feedback):
    _ses, service, _key, _ = feedback
    with service.db.transaction() as db:
        db.execute(
            "INSERT INTO suppressions VALUES('u@example.com','bounce',?)",
            (time.time(),),
        )
    with pytest.raises(Problem) as caught:
        service.request_code("u@example.com", "203.0.113.10")
    assert caught.value.status == 409
    assert service.db.one("SELECT email FROM otp WHERE email='u@example.com'") is None
    assert service.db.one("SELECT id FROM jobs WHERE kind='login_mail'") is None


def test_smtp_message_uses_crlf(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        secret="s" * 64,
        admin_emails=("owner@example.com",),
        mail_backend="file",
        mail_from="Quant <notify@example.com>",
    )
    Service(settings)
    message_id = Transport(settings).mail(
        "u@example.com",
        "Subject",
        "Line one\nLine two",
        identity="test-message",
    )
    assert message_id.endswith("@example.com>")
    raw = (tmp_path / "mail" / "test-message.eml").read_bytes()
    lines = raw.split(b"\n")
    assert all(line.endswith(b"\r") for line in lines[:-1])


def test_ses_message_id_parser():
    assert provider_message_id(b"Ok 0100018abc", "fallback") == "0100018abc"
    assert provider_message_id(b"Ok", "fallback") == "fallback"
    assert provider_message_id(b"250 queued", "fallback") == "fallback"


def test_per_process_smtp_preflight():
    base = dict(
        base_url="https://quant.example.com",
        secret="x" * 64,
        admin_emails=("owner@example.com",),
        mail_backend="smtp",
        ses_feedback_enabled=True,
        ses_sns_topic_arn=TOPIC,
        support_contact="support@quant.example",
        production=True,
    )
    web = Settings(**base, smtp_required=False)
    assert web.preflight(require_smtp=False) == []
    assert any("SMTP" in error for error in web.preflight(require_smtp=True))
    worker = Settings(**base, smtp_required=True)
    assert any("SMTP" in error for error in worker.preflight(require_smtp=True))


def test_production_requires_a_real_support_contact():
    base = dict(
        base_url="https://quant.example.com",
        secret="x" * 64,
        admin_emails=("owner@example.com",),
        mail_backend="smtp",
        ses_feedback_enabled=True,
        ses_sns_topic_arn=TOPIC,
        production=True,
        smtp_required=False,
    )
    missing = Settings(**base, support_contact="")
    assert any("SUPPORT_CONTACT" in error for error in missing.preflight(require_smtp=False))
    placeholder = Settings(**base, support_contact="owner@example.test")
    assert any("SUPPORT_CONTACT" in error for error in placeholder.preflight(require_smtp=False))
