"""DirectMail EventBridge feedback: token, dedupe, suppression and incidents."""
import json

import pytest

from hadan.config import Settings
from hadan.directmail_feedback import DirectMailFeedback
from hadan.service import Problem, Service

TOKEN = "t" * 48
HEADERS = {"x-eventbridge-signature-token": TOKEN}


@pytest.fixture
def feedback(tmp_path):
    service = Service(
        Settings(
            data_dir=tmp_path,
            secret="s" * 64,
            admin_emails=("owner@example.com",),
            mail_backend="file",
            directmail_feedback_enabled=True,
            directmail_webhook_token=TOKEN,
        )
    )
    return DirectMailFeedback(service), service


def envelope(kind, data, *, event_id="evt-1", source="acs:dm"):
    return json.dumps(
        {
            "id": event_id,
            "source": source,
            "specversion": "1.0",
            "type": kind,
            "data": data,
        }
    ).encode()


def test_permanent_bounce_suppresses_and_dedupes(feedback):
    handler, service = feedback
    raw = envelope(
        "dm:Deliver:Fail",
        {
            "rcpt": "bounce@example.com",
            "msg_id": "msg-1@example.com",
            "status": "2",
            "failed_type": "SmtpNxBox",
            "err_code": "554",
            "err_msg": "mailbox does not exist",
        },
    )
    result = handler.handle(raw, HEADERS)
    assert result["event"] == "Bounce" and result["suppressed"] == 1
    assert service.db.one(
        "SELECT reason FROM suppressions WHERE email='bounce@example.com'"
    )["reason"] == "bounce"
    assert service.db.one("SELECT COUNT(*) n FROM directmail_feedback")["n"] == 1

    again = handler.handle(raw, HEADERS)
    assert again["duplicate"] is True
    assert service.db.one("SELECT COUNT(*) n FROM suppressions")["n"] == 1


def test_spam_complaint_suppresses_and_opens_incident(feedback):
    handler, service = feedback
    handler.handle(
        envelope(
            "dm:Deliver:Fail",
            {
                "rcpt": "spam@example.com",
                "msg_id": "msg-2@example.com",
                "status": "3",
                "err_msg": "recipient marked as spam",
            },
            event_id="evt-2",
        ),
        HEADERS,
    )
    assert service.db.one(
        "SELECT reason FROM suppressions WHERE email='spam@example.com'"
    )["reason"] == "complaint"
    incident = service.db.one(
        "SELECT summary,state FROM incidents WHERE kind='directmail_feedback'"
    )
    assert incident["summary"] == "DirectMail spam complaint"
    assert incident["state"] == "open"


def test_fbl_report_suppresses_and_opens_incident(feedback):
    handler, service = feedback
    handler.handle(
        envelope(
            "dm:Feedback:FblReport",
            {
                "block_email": "fbl@example.com",
                "message_id": "<msg-3@example.com>",
                "fbl_isp": "outlook",
                "block_time": "1726821667",
            },
            event_id="evt-3",
            source="acs.dm",
        ),
        HEADERS,
    )
    assert service.db.one(
        "SELECT reason FROM suppressions WHERE email='fbl@example.com'"
    )["reason"] == "complaint"
    incident = service.db.one(
        "SELECT summary FROM incidents WHERE kind='directmail_feedback'"
    )
    assert incident["summary"] == "DirectMail FBL complaint"


def test_other_failure_is_incident_only(feedback):
    handler, service = feedback
    handler.handle(
        envelope(
            "dm:Deliver:Fail",
            {
                "rcpt": "soft@example.com",
                "msg_id": "msg-4@example.com",
                "status": "4",
                "err_msg": "temporary failure",
            },
            event_id="evt-4",
        ),
        HEADERS,
    )
    assert service.db.one(
        "SELECT email FROM suppressions WHERE email='soft@example.com'"
    ) is None
    assert service.db.one(
        "SELECT summary FROM incidents WHERE kind='directmail_feedback'"
    )["summary"] == "DirectMail delivery failure"


def test_delivery_is_recorded_without_suppression(feedback):
    handler, service = feedback
    result = handler.handle(
        envelope(
            "dm:Deliver:Succeed",
            {
                "rcpt": "ok@example.com",
                "msg_id": "msg-5@example.com",
                "status": "0",
                "failed_type": "SendOk",
            },
            event_id="evt-5",
        ),
        HEADERS,
    )
    assert result["event"] == "Delivery" and result["suppressed"] == 0
    assert service.db.one("SELECT event_type FROM directmail_feedback")["event_type"] == "Delivery"
    assert service.db.one("SELECT COUNT(*) n FROM suppressions")["n"] == 0


def test_invalid_token_and_source_are_rejected(feedback):
    handler, service = feedback
    raw = envelope("dm:Deliver:Fail", {"rcpt": "x@example.com", "status": "2"})
    with pytest.raises(Problem) as token_error:
        handler.handle(raw, {"x-eventbridge-signature-token": "wrong"})
    assert token_error.value.status == 403
    assert service.db.one("SELECT COUNT(*) n FROM directmail_feedback")["n"] == 0

    with pytest.raises(Problem) as source_error:
        handler.handle(
            envelope(
                "dm:Deliver:Fail",
                {"rcpt": "x@example.com", "status": "2"},
                source="acs:other",
            ),
            HEADERS,
        )
    assert source_error.value.status == 403


def test_production_preflight_accepts_directmail_feedback_without_ses():
    settings = Settings(
        data_dir="/tmp/unused",
        base_url="https://quant.example.test",
        secret="s" * 64,
        admin_emails=("owner@example.com",),
        mail_backend="smtp",
        smtp_host="smtpdm.example.test",
        smtp_user="notify@example.test",
        smtp_password="not-a-real-password",
        smtp_tls="implicit",
        mail_from="Quant <notify@example.test>",
        directmail_feedback_enabled=True,
        directmail_webhook_token=TOKEN,
    )
    settings.production = True
    assert settings.preflight() == []


def test_directmail_feedback_requires_a_long_token():
    settings = Settings(
        data_dir="/tmp/unused",
        base_url="https://quant.example.test",
        secret="s" * 64,
        admin_emails=("owner@example.com",),
        directmail_feedback_enabled=True,
        directmail_webhook_token="short",
    )
    settings.production = True
    errors = settings.preflight(require_smtp=False)
    assert any("DIRECTMAIL_WEBHOOK_TOKEN" in error for error in errors)
