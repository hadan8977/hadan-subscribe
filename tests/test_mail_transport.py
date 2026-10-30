import email
import smtplib
from email import policy

import pytest

from hadan.cli import grant
from hadan.config import Settings
from hadan.service import Service
from hadan.transport import DeliveryError, Transport
from hadan.worker import Worker


@pytest.mark.parametrize("stage,uncertain", [("rcpt", False), ("data", True)])
def test_smtp_deadline_stops_socket_and_preserves_acceptance_uncertainty(
    tmp_path, monkeypatch, stage, uncertain
):
    timers = []
    clients = []

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds < Settings().lease_seconds
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    class Socket:
        stopped = False

        def shutdown(self, _):
            self.stopped = True

    class SMTP:
        def __init__(self, **_):
            self.sock = Socket()
            self.closed = False
            clients.append(self)

        def connect(self, *_):
            pass

        def ehlo(self):
            pass

        def starttls(self, **_):
            pass

        def mail(self, *_):
            return 250, b"ok"

        def rcpt(self, *_):
            if stage == "rcpt":
                timers[0].callback()
                raise smtplib.SMTPServerDisconnected("deadline fixture")
            return 250, b"ok"

        def data(self, *_):
            timers[0].callback()
            raise smtplib.SMTPServerDisconnected("deadline fixture")

        def close(self):
            self.closed = True

    monkeypatch.setattr("hadan.transport.threading.Timer", Timer)
    monkeypatch.setattr("hadan.transport.smtplib.SMTP", SMTP)
    cfg = Settings(data_dir=tmp_path, mail_backend="smtp", smtp_host="fixture.invalid")
    with pytest.raises(DeliveryError) as raised:
        Transport(cfg).mail(
            "fixture@example.invalid", "Fixture", "body", identity="test"
        )
    assert raised.value.uncertain is uncertain
    assert raised.value.retry is (not uncertain)
    assert clients[0].sock.stopped and clients[0].closed and timers[0].cancelled


def test_implicit_tls_passes_hostname_to_wrapped_socket(tmp_path, monkeypatch):
    seen = {}

    class SMTPSSL:
        sock = None

        def __init__(self, host=None, port=0, timeout=None, context=None):
            seen.update(host=host, port=port, timeout=timeout, context=context)

        def ehlo(self):
            pass

        def login(self, *_):
            pass

        def mail(self, *_):
            return 250, b"ok"

        def rcpt(self, *_):
            return 250, b"ok"

        def data(self, *_):
            return 250, b"Ok directmail-fixture-id"

        def close(self):
            pass

    monkeypatch.setattr("hadan.transport.smtplib.SMTP_SSL", SMTPSSL)
    cfg = Settings(
        data_dir=tmp_path,
        mail_backend="smtp",
        smtp_host="smtpdm.example.invalid",
        smtp_port=465,
        smtp_tls="implicit",
        smtp_user="sender@example.invalid",
        smtp_password="fixture-password",
    )
    result = Transport(cfg).mail(
        "recipient@example.invalid", "Fixture", "body", identity="test"
    )
    assert seen["host"] == "smtpdm.example.invalid"
    assert seen["port"] == 465
    assert seen["context"] is not None
    assert result == "directmail-fixture-id"


def test_full_email_over_limit_uses_private_link_and_keeps_all_files(tmp_path):
    s = Service(
        Settings(
            data_dir=tmp_path,
            secret="fixture-private-key-" * 4,
            mail_reply_to="support@example.invalid",
        )
    )
    s.seed()
    grant(s, "reader@example.invalid", "notes", email_consent=True)
    sub = s.db.one("SELECT * FROM subscriptions")
    s.preferences(sub["user_id"], sub["id"], {"email_format": "full"})
    publication = s.publish(
        "notes",
        "large",
        "Large fixture",
        "Private body",
        files=[
            {"name": "a.txt", "data": b"a" * (9 * 1024**2)},
            {"name": "b.txt", "data": b"b" * (9 * 1024**2)},
        ],
    )
    assert Worker(s).run_once()
    delivery = s.db.one("SELECT * FROM deliveries")
    assert delivery["state"] == "accepted"
    message = email.message_from_bytes(
        (tmp_path / "mail" / (delivery["id"] + ".eml")).read_bytes(),
        policy=policy.default,
    )
    assert message["Reply-To"] == "support@example.invalid"
    assert "单封大小限制" in message.get_content()
    assert "/read/" + publication["id"] in message.get_content()
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert not list(message.iter_attachments())
    content = s.content(sub["user_id"], publication["id"])
    assert len(content["attachments"]) == 2
    assert s.db.one("SELECT email_format FROM subscriptions")["email_format"] == "full"
