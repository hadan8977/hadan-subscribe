from contextlib import contextmanager
import json
import time

from hadan.service import Problem
from hadan.transport import DeliveryError
from hadan.worker import Worker


class FakeTransport:
    def __init__(self, *, uncertain=False):
        self.calls = []
        self.uncertain = uncertain

    def mail(self, recipient, subject, body, **kwargs):
        self.calls.append((recipient, subject, body, kwargs))
        if self.uncertain:
            raise DeliveryError("SMTP closed after DATA", retry=False, uncertain=True)
        return "accepted-test-message"

    def webhook(self, *args):
        self.calls.append(args)
        return "204"


def prepare(harness):
    _, service, login = harness
    client, user = login("alice@example.com")
    subscription = client.post(
        "/api/subscriptions", json={"product_id": "quantgt"}
    ).json()
    # Auth jobs are not the target of these outbox experiments.
    service.db.execute("UPDATE jobs SET state='complete' WHERE kind='login_mail'")
    event = service.publish("quantgt", "report", "Title", "Body")
    delivery = service.db.one(
        "SELECT * FROM deliveries WHERE event_id=?", (event["event_id"],)
    )
    return service, user, subscription, delivery


def test_disabled_email_wins_over_already_queued_delivery(harness):
    service, user, sub, delivery = prepare(harness)
    service.preferences(user["id"], sub["id"], {"email_enabled": False})
    fake = FakeTransport()
    assert Worker(service, fake).run_once() is True
    assert fake.calls == []
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "skipped"
    )


def test_uncertain_smtp_is_not_blindly_retried(harness):
    service, user, sub, delivery = prepare(harness)
    service.settings.mail_backend = "smtp"
    fake = FakeTransport(uncertain=True)
    worker = Worker(service, fake)
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert len(fake.calls) == 1
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "uncertain"
    )
    assert (
        service.db.one("SELECT state FROM jobs WHERE kind='deliver'")["state"]
        == "uncertain"
    )
    assert service.db.one("SELECT count FROM incidents")["count"] == 1


def test_crash_after_smtp_dispatch_does_not_requeue(harness):
    service, user, sub, delivery = prepare(harness)
    service.settings.mail_backend = "smtp"
    fake = FakeTransport()
    worker = Worker(service, fake)
    job = worker.claim()
    service.db.execute(
        "UPDATE deliveries SET state='sending' WHERE id=?", (delivery["id"],)
    )
    service.db.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"])
    )
    # A replacement process knows acceptance may have happened before the crash.
    assert Worker(service, fake).claim() is None
    assert fake.calls == []
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "uncertain"
    )


def test_crash_before_dispatch_is_safely_reclaimable(harness):
    service, user, sub, delivery = prepare(harness)
    service.settings.mail_backend = "smtp"
    fake = FakeTransport()
    old = Worker(service, fake).claim()
    service.db.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, old["id"])
    )
    worker = Worker(service, fake)
    new = worker.claim()
    assert new["id"] == old["id"]
    assert new["lease_token"] != old["lease_token"]
    worker.dispatch(new)
    assert len(fake.calls) == 1
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "accepted"
    )


def test_idempotent_publish_commits_single_event_and_delivery(harness):
    service, user, sub, delivery = prepare(harness)
    again = service.publish("quantgt", "report", "Title", "Body")
    assert again["unchanged"] is True
    assert len(service.db.all("SELECT * FROM events")) == 1
    assert len(service.db.all("SELECT * FROM deliveries")) == 1
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='deliver'")) == 1


def test_unsubscribe_committed_before_sending_transition_is_respected(
    harness, monkeypatch
):
    service, user, sub, delivery = prepare(harness)
    fake = FakeTransport()
    original_transaction = service.db.transaction
    injected = False

    @contextmanager
    def transaction():
        nonlocal injected
        # Concurrent preference change commits immediately before the dispatch
        # transaction takes its lock, after any initial nontransactional reads.
        if not injected:
            injected = True
            service.preferences(user["id"], sub["id"], {"email_enabled": False})
        with original_transaction() as db:
            yield db

    monkeypatch.setattr(service.db, "transaction", transaction)
    Worker(service, fake).deliver(delivery["id"])
    assert injected
    assert fake.calls == [], (
        "a committed preference change was ignored before dispatch authorization"
    )
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "skipped"
    )
