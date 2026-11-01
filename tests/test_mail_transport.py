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
    assert "single-message size limit" in message.get_content()
    assert "/read/" + publication["id"] in message.get_content()
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert not list(message.iter_attachments())
    content = s.content(sub["user_id"], publication["id"])
    assert len(content["attachments"]) == 2
    assert s.db.one("SELECT email_format FROM subscriptions")["email_format"] == "full"


def test_report_email_always_uses_full_body_attachments_and_one_site_link(tmp_path):
    s = Service(
        Settings(
            data_dir=tmp_path,
            secret="fixture-private-key-" * 4,
            mail_backend="file",
            base_url="https://quant.example.test",
        )
    )
    s.seed()
    grant(s, "reader@example.invalid", "notes", email_consent=True)
    sub = s.db.one("SELECT * FROM subscriptions")
    # Legacy rows may still say summary; delivery must ignore that value.
    s.db.execute(
        "UPDATE subscriptions SET email_format='summary' WHERE id=?", (sub["id"],)
    )
    s.publish(
        "notes",
        "report",
        "Report title",
        "Plain body",
        html="<p>Ignored headline</p><table><tr><td>Rich table</td></tr></table>",
        files=[{"name": "report.txt", "data": b"attachment"}],
    )
    assert Worker(s).run_once()
    delivery = s.db.one("SELECT * FROM deliveries")
    assert delivery["state"] == "accepted"
    message = email.message_from_bytes(
        (tmp_path / "mail" / (delivery["id"] + ".eml")).read_bytes(),
        policy=policy.default,
    )
    text_part = message.get_body(("plain",)).get_content()
    html_part = message.get_body(("html",)).get_content()
    assert "Plain body" in text_part
    assert "Rich table" in html_part
    assert "Ignored headline" not in html_part
    assert text_part.count("https://quant.example.test") == 1
    assert html_part.count("https://quant.example.test") == 1
    assert "/read/" not in text_part + html_part
    assert "/unsubscribe/" not in text_part + html_part
    assert [item.get_filename() for item in message.iter_attachments()] == [
        "report.txt"
    ]


def test_content_mail_carries_stable_quant_event_headers(tmp_path):
    cfg = Settings(
        data_dir=tmp_path,
        mail_backend="file",
        mail_from="Quant <notify@example.test>",
    )
    (tmp_path / "mail").mkdir()
    Transport(cfg).mail(
        "reader@example.test",
        "Report",
        "Body",
        identity="event-123",
        headers={
            "X-Quant-Event-ID": "event-123",
            "X-Quant-Content-ID": "content-456",
            "X-Quant-Version": "2",
        },
    )
    message = email.message_from_bytes((tmp_path / "mail" / "event-123.eml").read_bytes())
    assert message["X-Quant-Event-ID"] == "event-123"
    assert message["X-Quant-Content-ID"] == "content-456"
    assert message["X-Quant-Version"] == "2"


def test_picks_email_has_three_sections_and_neutral_subject(tmp_path):
    s = Service(
        Settings(
            data_dir=tmp_path,
            secret="fixture-private-key-" * 4,
            mail_backend="file",
            base_url="https://quant.example.test",
        )
    )
    s.seed()
    grant(s, "reader@example.invalid", "notes", email_consent=True)
    body = """Portfolio rebalanced & Weekly Watchlist updated

Changes:
Portfolio — Oct 30, 2026 (previously Oct 23, 2026)
  + Added MPC — Marathon Petroleum Corporation
Weekly Watchlist — Oct 30, 2026 (previously Oct 23, 2026)
  + Added TWST — Twist Bioscience Corporation

Portfolio — Oct 30, 2026 · 1 stocks
  1. MRNA — Moderna, Inc. | GT 4.91 · $221.72 · Buy +0.51

Weekly Watchlist — Oct 30, 2026 · 1 stocks
  1. MRNA — Moderna, Inc. | GT 4.87 · $221.72 · Buy +0.51

Full details: attached Excel report and page screenshots.
Data checked Nov 1, 2026."""
    s.publish(
        "notes",
        "picks:test",
        "Quant GT · Portfolio rebalance: +MRNA",
        body,
        files=[{"name": "report.txt", "data": b"attachment"}],
    )
    assert Worker(s).run_once()
    delivery = s.db.one("SELECT * FROM deliveries")
    message = email.message_from_bytes(
        (tmp_path / "mail" / (delivery["id"] + ".eml")).read_bytes(),
        policy=policy.default,
    )
    assert message["Subject"] == "Quant GT · Portfolio update"
    text_part = message.get_body(("plain",)).get_content()
    html_part = message.get_body(("html",)).get_content()
    for heading in ("What changed", "Monthly Picks", "Weekly Picks"):
        assert heading in text_part
        assert heading in html_part
    assert text_part.count("https://quant.example.test") == 1
    assert html_part.count("https://quant.example.test") == 1
    assert [item.get_filename() for item in message.iter_attachments()] == [
        "report.txt"
    ]


def test_picks_email_keeps_legacy_style_and_splits_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("HADAN_EMAIL_LAYOUT", "legacy")
    s = Service(
        Settings(
            data_dir=tmp_path,
            secret="fixture-private-key-" * 4,
            mail_backend="file",
            base_url="https://quant.example.test",
        )
    )
    s.seed()
    grant(s, "reader@example.invalid", "notes", email_consent=True)
    body = """Portfolio rebalanced & Weekly Watchlist updated

Changes:
Portfolio — Oct 30, 2026 (previously Oct 23, 2026)
  + Added MPC — Marathon Petroleum Corporation
Weekly Watchlist — Oct 30, 2026 (previously Oct 23, 2026)
  + Added TWST — Twist Bioscience Corporation

Portfolio — Oct 30, 2026 · 1 stocks
  1. MRNA — Moderna, Inc. | GT 4.91 · $221.72 · Buy +0.51

Weekly Watchlist — Oct 30, 2026 · 1 stocks
  1. MRNA — Moderna, Inc. | GT 4.87 · $221.72 · Buy +0.51

Full details: attached Excel report and page screenshots.
Data checked Nov 1, 2026."""
    legacy_html = """<html><body>
<div style="display:none;max-height:0;">preheader</div>
<div style="font-size:11px;font-weight:800;">Quant GT Picks Alert</div>
<h1 style="font-size:23px;">Portfolio rebalanced &amp; Weekly Watchlist updated</h1>
<div style="font-size:14px;line-height:1.5;color:#334155;">summary line</div>
<div style="margin:20px 0 4px 0;padding:16px 14px 0 14px;border:1px solid #e2e8f0;">
  <div style="font-size:16px;font-weight:800;">What changed</div>
</div>
<div style="margin:22px 0 0 0;"><div>Portfolio · 1 stocks · Oct 30</div>
  <table><tr><td style="padding:6px 0;">MRNA</td></tr></table></div>
<div style="margin:22px 0 0 0;"><div>Weekly Watchlist · 1 stocks · Oct 30</div>
  <table><tr><td style="padding:6px 0;">MRNA</td></tr></table></div>
<div style="margin:22px 0 0 0;padding:12px 0 0 0;border-top:1px solid #e2e8f0;">Full details</div>
</body></html>"""
    s.publish(
        "notes",
        "picks:legacy",
        "Quant GT · Portfolio rebalance: +MRNA",
        body,
        html=legacy_html,
        files=[{"name": "report.txt", "data": b"attachment"}],
    )
    assert Worker(s).run_once()
    delivery = s.db.one("SELECT * FROM deliveries")
    message = email.message_from_bytes(
        (tmp_path / "mail" / (delivery["id"] + ".eml")).read_bytes(),
        policy=policy.default,
    )
    html_part = message.get_body(("html",)).get_content()
    assert "Quant GT Picks Alert" in html_part
    assert "What changed" in html_part
    assert "Monthly Picks · 1 stocks" in html_part
    assert "Weekly Picks · 1 stocks" in html_part
    assert "Portfolio rebalanced" not in html_part
    assert "summary line" not in html_part
    assert "Manage subscription" in html_part
    assert "管理订阅" not in html_part
    assert html_part.count("https://quant.example.test") == 1
    assert [item.get_filename() for item in message.iter_attachments()] == [
        "report.txt"
    ]


def test_compact_picks_email_is_short_and_english(tmp_path, monkeypatch):
    monkeypatch.setenv("HADAN_EMAIL_LAYOUT", "compact")
    s = Service(
        Settings(
            data_dir=tmp_path,
            secret="fixture-private-key-" * 4,
            mail_backend="file",
            base_url="https://quant.example.test",
        )
    )
    s.seed()
    grant(s, "reader@example.invalid", "notes", email_consent=True)
    body = """Portfolio rebalanced & Weekly Watchlist updated

Changes:
Portfolio — Oct 30, 2026 (previously Oct 23, 2026)
  + Added MPC — Marathon Petroleum Corporation
  - Removed DELL — Dell Technologies Inc.
  * GT Score re-rated: MRNA 4.61->4.91
Weekly Watchlist — Oct 30, 2026 (previously Oct 23, 2026)
  + Added TWST — Twist Bioscience Corporation
  - Removed CHYM — Chime Financial, Inc.
  * GT Score re-rated: ESTC 4.17->4.14

Portfolio — Oct 30, 2026 · 2 stocks
  1. MRNA — Moderna, Inc. | +18.81% · GT 4.91 · $221.72 · Strong Buy +0.51
  2. MPC — Marathon Petroleum Corporation | GT 4.08 · $444.73 · Buy +0.18

Weekly Watchlist — Oct 30, 2026 · 2 stocks
  1. MRNA — Moderna, Inc. | GT 4.87 · $221.72 · Strong Buy +0.51 · Up 10.8% last week
  2. TWST — Twist Bioscience Corporation | GT 4.47 · $152.92 · Sell -0.11

Full details: attached Excel report and page screenshots.
Data checked Nov 1, 2026."""
    s.publish(
        "notes",
        "picks:compact",
        "Quant GT · Portfolio rebalance: +MRNA",
        body,
        files=[{"name": "report.txt", "data": b"attachment"}],
    )
    assert Worker(s).run_once()
    delivery = s.db.one("SELECT * FROM deliveries")
    message = email.message_from_bytes(
        (tmp_path / "mail" / (delivery["id"] + ".eml")).read_bytes(),
        policy=policy.default,
    )
    text_part = message.get_body(("plain",)).get_content()
    html_part = message.get_body(("html",)).get_content()
    assert message["Subject"] == "Quant GT · Portfolio update"
    for heading in ("What changed", "Monthly Picks", "Weekly Picks"):
        assert heading in text_part
        assert heading in html_part
    assert "Manage subscription" in text_part
    assert "Manage subscription" in html_part
    assert "管理订阅" not in text_part + html_part
    assert "MPC (NEW)" in text_part and "TWST (NEW)" in text_part
    assert html_part.count(">NEW</span>") == 2
    assert "background:#dcfce7" in html_part
    assert len(html_part) < 15000
    assert [item.get_filename() for item in message.iter_attachments()] == [
        "report.txt"
    ]
