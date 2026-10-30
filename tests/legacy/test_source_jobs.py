"""Source publishing and recovery acceptance using realistic data and fake pages."""

from contextlib import contextmanager
import copy
from datetime import date, datetime, timezone
from email.message import EmailMessage
import json
import time

import pytest

from hadan.cli import grant, import_members
from hadan.sources import jobs
from .test_validation import valid_capture


def picks_data():
    result = valid_capture()
    result["fetched_at"] = "2026-10-26T12:30:00+00:00"
    return result


def row_count(service, table):
    assert table in {"contents", "versions", "events", "deliveries"}
    return service.db.one("SELECT COUNT(*) count FROM " + table)["count"]


def reader(service, email="reader@example.test"):
    grant(service, email, "quantgt", email_consent=True)
    return service.db.one("SELECT id FROM users WHERE email=?", (email,))["id"]


def test_picks_first_capture_is_private_baseline_then_real_change_fans_out(
    source_service,
):
    s = source_service
    reader(s)
    data = picks_data()
    first = jobs.accept_picks(s, data)
    assert first["version"] == 1
    assert row_count(s, "deliveries") == 0
    updated = copy.deepcopy(data)
    updated["weekly"]["rows"][0]["symbol"] = "NEW"
    published = jobs.accept_picks(s, updated)
    assert published["event_id"] != first["event_id"]
    assert row_count(s, "deliveries") == 1
    assert s.db.one(
        "SELECT name FROM attachments WHERE version_id=(SELECT version_id FROM events WHERE id=?)",
        (published["event_id"],),
    )["name"].endswith(".xlsx")


def test_no_change_refreshes_health_without_publishing_market_noise(source_service):
    s = source_service
    data = picks_data()
    jobs.accept_picks(s, data)
    before = row_count(s, "events")
    observed = copy.deepcopy(data)
    observed["fetched_at"] = "2026-10-26T13:00:00+00:00"
    observed["weekly"]["rows"][0]["current_price"] = "$999.00"
    assert jobs.accept_picks(s, observed)["unchanged"]
    assert row_count(s, "events") == before
    assert jobs.state(s, "picks:last_success") > 0


def test_partial_capture_cannot_promote_source_baseline(source_service):
    s = source_service
    data = picks_data()
    jobs.accept_picks(s, data)
    accepted_before = jobs.state(s, "picks:accepted")
    broken = copy.deepcopy(data)
    broken["weekly"]["rows"] = broken["weekly"]["rows"][:2]
    with pytest.raises(RuntimeError, match="near-complete"):
        jobs.accept_picks(s, broken)
    assert jobs.state(s, "picks:accepted") == accepted_before
    assert row_count(s, "events") == 1


def test_picks_retry_after_publish_before_source_save_is_idempotent(
    source_service, monkeypatch
):
    s = source_service
    reader(s)
    initial = picks_data()
    jobs.accept_picks(s, initial)
    changed = copy.deepcopy(initial)
    changed["weekly"]["rows"][0]["symbol"] = "NEW"
    changed["fetched_at"] = "2026-10-26T13:00:00+00:00"
    save = jobs.save

    def interrupted(service, key, value):
        if key == "picks:accepted":
            raise RuntimeError("injected source-state crash")
        return save(service, key, value)

    with monkeypatch.context() as m:
        m.setattr(jobs, "save", interrupted)
        with pytest.raises(RuntimeError, match="injected"):
            jobs.accept_picks(s, changed)
    retry = copy.deepcopy(changed)
    retry["fetched_at"] = "2026-10-26T13:02:00+00:00"
    retry["weekly"]["rows"][0]["current_price"] = "$10.03"
    jobs.accept_picks(s, retry)
    assert row_count(s, "events") == 2, (
        "One baseline plus one real update; crash recovery must not publish a fake revision"
    )
    assert row_count(s, "deliveries") == 1


def test_recurring_real_change_after_intervening_update_is_not_suppressed(
    source_service,
):
    s = source_service
    reader(s)
    original = picks_data()
    changed = copy.deepcopy(original)
    changed["weekly"]["rows"][0]["symbol"] = "NEW"
    jobs.accept_picks(s, original)
    jobs.accept_picks(s, changed)
    jobs.accept_picks(s, original)
    jobs.accept_picks(s, changed)
    assert row_count(s, "events") == 4
    assert row_count(s, "deliveries") == 3


def digest_payload(headline="Weekly summary"):
    return {
        "week_start": datetime.now(timezone.utc).date().isoformat(),
        "headline": headline,
        "sections": {
            "market_news": [
                {"title": "Source news", "note": "Context", "source": "Source"}
            ],
            "watchlist": [
                {"ticker": "AAA", "name": "Alpha", "score": 4.5, "reason": "Reason"}
            ],
        },
    }


def fake_digest(monkeypatch, payload):
    class Page:
        def evaluate(self, script):
            return copy.deepcopy(payload)

    @contextmanager
    def session():
        yield Page()

    monkeypatch.setattr(jobs, "browser_session", session)


def test_digest_correction_publishes_once_and_next_identical_poll_is_noop(
    source_service, monkeypatch
):
    s = source_service
    reader(s)
    fake_digest(monkeypatch, digest_payload())
    first = jobs.weekly_digest(s)
    corrected = digest_payload("Corrected headline")
    fake_digest(monkeypatch, corrected)
    second = jobs.weekly_digest(s)
    third = jobs.weekly_digest(s)
    assert first["id"] == second["id"]
    assert second["version"] == 2
    assert third["unchanged"], (
        "Changing only the generated revision label must not create another event"
    )
    assert row_count(s, "events") == 2
    assert row_count(s, "deliveries") == 1


def test_stale_digest_does_not_overwrite_latest_content(source_service, monkeypatch):
    payload = digest_payload()
    fake_digest(monkeypatch, payload)
    jobs.weekly_digest(source_service)
    stale = copy.deepcopy(payload)
    stale["week_start"] = "2000-01-01"
    fake_digest(monkeypatch, stale)
    with pytest.raises(ValueError, match="freshness"):
        jobs.weekly_digest(source_service)
    assert jobs.state(source_service, "digest:accepted") == payload
    assert row_count(source_service, "events") == 1


class SitePage:
    def __init__(self, heading="Page", fail_path=None):
        self.heading, self.fail_path, self.url = heading, fail_path, ""

    def goto(self, url, **_):
        self.url = url
        if self.fail_path and url.endswith(self.fail_path):
            raise TimeoutError("fake timeout")

    def wait_for_function(self, *_, **__):
        pass

    def evaluate(self, script):
        return {
            "title": "Source",
            "headings": [self.heading],
            "nav": ["Portfolio"],
            "buttons": [],
            "links": [],
        }


def fake_site(monkeypatch, page):
    @contextmanager
    def session():
        yield page

    monkeypatch.setattr(jobs, "browser_session", session)


def test_site_snapshot_repeat_and_structural_change_are_valid(
    source_service, monkeypatch
):
    fake_site(monkeypatch, SitePage())
    assert not jobs.health_site(source_service)["changed"]
    assert not jobs.health_site(source_service)["changed"]
    fake_site(monkeypatch, SitePage("New source navigation"))
    assert jobs.health_site(source_service)["changed"]
    assert source_service.db.one("SELECT id FROM incidents WHERE kind='site_structure'")


def test_partial_site_capture_preserves_known_page_and_reports_failure(
    source_service, monkeypatch
):
    fake_site(monkeypatch, SitePage())
    jobs.health_site(source_service)
    before = jobs.state(source_service, "site:accepted")
    fake_site(monkeypatch, SitePage(fail_path="market-tools"))
    with pytest.raises(RuntimeError, match="Partial site scan"):
        jobs.health_site(source_service)
    after = jobs.state(source_service, "site:accepted")

    def pages(value):
        return (
            value["pages"]
            if isinstance(value["pages"], dict)
            else {page["name"]: page for page in value["pages"]}
        )

    assert pages(after)["market_tools"] == pages(before)["market_tools"]


def test_verified_official_mail_keeps_attachments_and_queues_one_pick_check(
    source_service, monkeypatch
):
    monkeypatch.setattr(jobs, "authenticated_sender", lambda *_: True)
    monkeypatch.setenv("OFFICIAL_SENDER_DOMAINS", "quantgt.io")
    message = EmailMessage()
    message["From"] = "editor@quantgt.io"
    message["Subject"] = "Updated picks"
    message["Message-ID"] = "<fixture-123@quantgt.io>"
    message.set_content("Full official message")
    message.add_attachment(
        b"private workbook",
        maintype="application",
        subtype="octet-stream",
        filename="report.xlsx",
    )
    one = jobs.ingest_message(source_service, message.as_bytes(), "fixture-inbox")
    two = jobs.ingest_message(source_service, message.as_bytes(), "fixture-inbox")
    assert two["unchanged"]
    assert row_count(source_service, "events") == 1
    assert (
        source_service.db.one("SELECT name FROM attachments")["name"] == "report.xlsx"
    )
    assert (
        len(source_service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")) == 1
    )


def test_official_source_recovery_preserves_post_publish_pick_trigger(
    source_service, monkeypatch
):
    monkeypatch.setattr(jobs, "authenticated_sender", lambda *_: True)
    monkeypatch.setenv("OFFICIAL_SENDER_DOMAINS", "quantgt.io")
    message = EmailMessage()
    message["From"] = "editor@quantgt.io"
    message["Subject"] = "Source-trigger recovery"
    message["Message-ID"] = "<recovery@quantgt.io>"
    message.set_content(
        "Content whose publication committed before trigger scheduling."
    )
    with monkeypatch.context() as m:
        m.setattr(
            jobs,
            "enqueue",
            lambda *_, **__: (_ for _ in ()).throw(
                RuntimeError("trigger queue interrupted")
            ),
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            jobs.ingest_message(source_service, message.as_bytes(), "fixture-inbox")
    jobs.ingest_message(source_service, message.as_bytes(), "fixture-inbox")
    assert row_count(source_service, "events") == 1
    assert (
        len(source_service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")) == 1
    )


def test_cancelled_legacy_record_never_regains_content_access(source_service, tmp_path):
    path = tmp_path / "members.json"
    path.write_text(
        json.dumps(
            {
                "members": [
                    {
                        "email": "cancelled@example.test",
                        "status": "cancelled",
                        "expires_at": None,
                    }
                ]
            }
        )
    )
    import_members(source_service, path, apply=True, consent=True)
    user = source_service.db.one(
        "SELECT id FROM users WHERE email='cancelled@example.test'"
    )
    assert source_service.entitled(user["id"], "quantgt") is None


def test_reimport_never_overwrites_a_later_renewal(source_service, tmp_path):
    path = tmp_path / "members.json"
    path.write_text(
        json.dumps(
            {
                "members": [
                    {
                        "email": "renewed@example.test",
                        "status": "active",
                        "expires_at": "2030-01-09T00:00:00-05:00",
                    }
                ]
            }
        )
    )
    import_members(source_service, path, apply=True, consent=True)
    grant(
        source_service, "renewed@example.test", "quantgt", "2030-04-09T00:00:00-04:00"
    )
    before = source_service.db.one("SELECT expires_at FROM subscriptions")["expires_at"]
    import_members(source_service, path, apply=True, consent=True)
    assert (
        source_service.db.one("SELECT expires_at FROM subscriptions")["expires_at"]
        == before
    )


def test_replay_does_not_requeue_accepted_or_uncertain_delivery(source_service):
    s = source_service
    for i in range(3):
        reader(s, f"reader{i}@example.test")
    event = s.publish("quantgt", "replay-fixture", "Update", "Body")
    deliveries = s.db.all("SELECT * FROM deliveries ORDER BY user_id")
    for row, status in zip(deliveries, ("accepted", "uncertain", "failed")):
        s.db.execute("UPDATE deliveries SET state=? WHERE id=?", (status, row["id"]))
        s.db.execute(
            "UPDATE jobs SET state=? WHERE dedupe=?",
            ("complete" if status == "accepted" else status, "delivery:" + row["id"]),
        )
    assert not s.replay(event["event_id"])["applied"]
    s.replay(event["event_id"], apply=True)
    assert [
        row["state"]
        for row in s.db.all("SELECT state FROM deliveries ORDER BY user_id")
    ] == ["accepted", "uncertain", "queued"]
