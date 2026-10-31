from contextlib import contextmanager
import json
import time

import pytest

from hadan.service import Problem
from hadan.transport import DeliveryError
from hadan.worker import Worker


class NoticeTransport:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.mail_calls = []
        self.webhook_calls = []

    def mail(self, recipient, subject, body, **kwargs):
        self.mail_calls.append((recipient, subject, body, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return "fixture-account-accepted"

    def webhook(self, *args):
        self.webhook_calls.append(args)
        return "204"


def subscriber(harness, email="alice@example.com"):
    _, service, login = harness
    client, user = login(email)
    sub = client.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    service.db.execute("UPDATE jobs SET state='complete' WHERE kind='login_mail'")
    return service, client, user, sub


def notice(service, user_id, *, identity="fixture-account-notice"):
    with service.db.transaction() as db:
        event_id = service.account_notice(
            db,
            user_id,
            "quantgt",
            "expired",
            "Subscription expired",
            "Private account detail for this subscriber",
            identity,
        )
    return event_id, service.db.one(
        "SELECT * FROM deliveries WHERE event_id=? AND channel='email'", (event_id,)
    )


def test_expired_account_notice_is_private_without_restoring_content_access(
    harness, bind_agent
):
    service, alice, user_a, sub_a = subscriber(harness)
    _, bob, user_b, _ = subscriber(harness, "bob@example.com")
    _, auth_a = bind_agent(alice)
    _, auth_b = bind_agent(bob)
    published = service.publish(
        "quantgt",
        "before-expiry",
        "Private report",
        "Paid content",
        files=[{"name": "report.txt", "data": b"paid-file"}],
    )
    aid = service.content(user_a["id"], published["id"])["attachments"][0]["id"]
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?",
        (time.time() - 1, sub_a["id"]),
    )
    event_id, _ = notice(service, user_a["id"])
    events_a = alice.get("/api/events", headers=auth_a).json()["events"]
    assert [e["id"] for e in events_a] == [event_id]
    assert events_a[0]["kind"] == "account:expired"
    assert events_a[0]["content_id"] is None and events_a[0]["version_id"] is None
    assert event_id not in [
        e["id"] for e in bob.get("/api/events", headers=auth_b).json()["events"]
    ]
    for headers in ({}, auth_a):
        assert (
            alice.get("/api/content/" + published["id"], headers=headers).status_code
            == 404
        )
        assert alice.get("/api/files/" + aid, headers=headers).status_code == 404
    assert (
        alice.post(
            "/api/events/" + published["event_id"] + "/ack", headers=auth_a
        ).status_code
        == 404
    )
    assert (
        bob.post("/api/events/" + event_id + "/ack", headers=auth_b).status_code == 404
    )
    rpc = alice.post(
        "/mcp",
        headers=auth_a,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_events", "arguments": {}},
        },
    )
    assert [
        e["id"]
        for e in json.loads(rpc.json()["result"]["content"][0]["text"])["events"]
    ] == [event_id]
    assert (
        alice.post("/api/events/" + event_id + "/ack", headers=auth_a).status_code
        == 200
    )


def test_account_notice_replay_never_fans_out_to_other_subscribers(harness):
    service, _, alice, _ = subscriber(harness)
    _, _, bob, _ = subscriber(harness, "bob@example.com")
    event_id, _ = notice(service, alice["id"])
    service.replay(event_id, apply=True)
    assert {
        r["user_id"]
        for r in service.db.all(
            "SELECT user_id FROM receipts WHERE event_id=?", (event_id,)
        )
    } == {alice["id"]}
    assert {
        r["user_id"]
        for r in service.db.all(
            "SELECT user_id FROM deliveries WHERE event_id=?", (event_id,)
        )
    } == {alice["id"]}
    try:
        service.replay(event_id, bob["id"], apply=True)
    except Problem as error:
        assert error.status in (403, 404)
    assert service.events(bob["id"]) == []
    assert (
        service.db.one(
            "SELECT id FROM deliveries WHERE event_id=? AND user_id=?",
            (event_id, bob["id"]),
        )
        is None
    )


def test_expired_notice_retains_pending_state_when_agent_changes(harness, bind_agent):
    service, client, user, sub = subscriber(harness)
    _, old_auth = bind_agent(client, name="old-agent")
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    event_id, _ = notice(service, user["id"])
    _, new_auth = bind_agent(client, name="replacement-agent")
    assert client.get("/api/events", headers=old_auth).status_code == 401
    assert (
        client.post("/api/events/" + event_id + "/ack", headers=old_auth).status_code
        == 401
    )
    assert [
        e["id"]
        for e in client.get("/api/events?pending=true", headers=new_auth).json()[
            "events"
        ]
    ] == [event_id]
    assert (
        client.post("/api/events/" + event_id + "/ack", headers=new_auth).status_code
        == 200
    )


def test_agent_optout_hides_expired_account_notices_and_blocks_ack(harness, bind_agent):
    service, client, user, sub = subscriber(harness)
    _, auth = bind_agent(client)
    event_id, _ = notice(service, user["id"])
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    service.preferences(user["id"], sub["id"], {"agent_enabled": False})
    assert client.get("/api/events", headers=auth).json()["events"] == []
    assert (
        client.post("/api/events/" + event_id + "/ack", headers=auth).status_code == 404
    )
    assert [e["id"] for e in client.get("/api/events").json()["events"]] == [event_id]


@pytest.mark.parametrize("block", ["email_optout", "suppression", "full_unsubscribe"])
def test_account_email_respects_current_optout_and_suppression(harness, block):
    service, _, user, sub = subscriber(harness)
    event_id, delivery = notice(service, user["id"])
    if block == "email_optout":
        service.preferences(user["id"], sub["id"], {"email_enabled": False})
    elif block == "suppression":
        service.db.execute(
            "INSERT INTO suppressions VALUES(?,?,?)",
            (user["email"], "complaint", time.time()),
        )
    else:
        service.cancel_subscription(user["id"], sub["id"])
    transport = NoticeTransport()
    Worker(service, transport).deliver(delivery["id"])
    assert transport.mail_calls == []
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "skipped"
    )


def test_account_email_rechecks_consent_at_dispatch_transaction(harness, monkeypatch):
    service, _, user, sub = subscriber(harness)
    _, delivery = notice(service, user["id"])
    original = service.db.transaction
    injected = False

    @contextmanager
    def transaction():
        nonlocal injected
        if not injected:
            injected = True
            service.preferences(user["id"], sub["id"], {"email_enabled": False})
        with original() as db:
            yield db

    monkeypatch.setattr(service.db, "transaction", transaction)
    transport = NoticeTransport()
    Worker(service, transport).deliver(delivery["id"])
    assert injected and transport.mail_calls == []


def test_account_mail_is_independent_of_agent_processing(harness, bind_agent):
    service, client, user, sub = subscriber(harness)
    _, auth = bind_agent(client)
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    event_id, delivery = notice(service, user["id"])
    assert (
        client.post("/api/events/" + event_id + "/ack", headers=auth).status_code == 200
    )
    transport = NoticeTransport()
    Worker(service, transport).deliver(delivery["id"])
    assert len(transport.mail_calls) == 1


def test_old_webhook_does_not_receive_account_notice_after_binding_revocation(
    harness, bind_agent
):
    service, client, user, _ = subscriber(harness)
    binding, _ = bind_agent(client)
    # Fixture represents an already-tested active webhook, without network calls.
    service.db.execute(
        "UPDATE bindings SET mode='webhook',endpoint='https://callback.example/events',test_hook=1 WHERE id=?",
        (binding["id"],),
    )
    event_id, _ = notice(service, user["id"])
    hook = service.db.one(
        "SELECT * FROM deliveries WHERE event_id=? AND channel='webhook'", (event_id,)
    )
    assert hook is not None
    service.revoke(user["id"], binding["id"])
    transport = NoticeTransport()
    Worker(service, transport).deliver(hook["id"])
    assert transport.webhook_calls == []
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (hook["id"],))[
            "state"
        ]
        == "skipped"
    )


def test_account_notice_is_idempotent_per_provider_identity(harness):
    service, _, user, _ = subscriber(harness)
    first, _ = notice(service, user["id"])
    second, _ = notice(service, user["id"])
    assert first == second
    assert len(service.db.all("SELECT * FROM events WHERE kind LIKE 'account:%'")) == 1
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='account_mail'")) == 1


def test_account_smtp_temporary_error_retries_then_accepts(harness):
    service, _, user, sub = subscriber(harness)
    service.settings.mail_backend = "smtp"
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    _, delivery = notice(service, user["id"])
    transport = NoticeTransport(
        [DeliveryError("temporary connection failure", retry=True)]
    )
    worker = Worker(service, transport)
    assert worker.run_once() is True
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "retrying"
    )
    service.db.execute("UPDATE jobs SET available_at=0 WHERE kind='account_mail'")
    assert worker.run_once() is True
    assert len(transport.mail_calls) == 2
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "accepted"
    )


def test_account_smtp_uncertain_result_and_replay_never_resend(harness):
    service, _, user, _ = subscriber(harness)
    service.settings.mail_backend = "smtp"
    event_id, delivery = notice(service, user["id"])
    transport = NoticeTransport(
        [DeliveryError("connection closed after DATA", retry=False, uncertain=True)]
    )
    worker = Worker(service, transport)
    assert worker.run_once() is True
    service.replay(event_id, apply=True)
    assert worker.run_once() is False
    assert len(transport.mail_calls) == 1
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "uncertain"
    )


def test_account_worker_crash_after_dispatch_preserves_uncertainty(harness):
    service, _, user, _ = subscriber(harness)
    service.settings.mail_backend = "smtp"
    _, delivery = notice(service, user["id"])
    worker = Worker(service, NoticeTransport())
    job = worker.claim()
    assert job["kind"] == "account_mail"
    service.db.execute(
        "UPDATE deliveries SET state='sending' WHERE id=?", (delivery["id"],)
    )
    service.db.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"])
    )
    assert worker.claim() is None
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "uncertain"
    )
    assert (
        service.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"]
        == "uncertain"
    )


def test_failed_account_email_can_be_replayed_only_to_its_owner(harness):
    service, _, user, _ = subscriber(harness)
    event_id, delivery = notice(service, user["id"])
    transport = NoticeTransport(
        [DeliveryError("recipient rejected fixture", retry=False)]
    )
    worker = Worker(service, transport)
    assert worker.run_once() is True
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "failed"
    )
    service.replay(event_id, user["id"], apply=True)
    assert worker.run_once() is True
    assert len(transport.mail_calls) == 2
    assert (
        service.db.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[
            "state"
        ]
        == "accepted"
    )


def test_account_notice_without_preference_defaults_to_immediate_email(harness):
    service, _, user, sub = subscriber(harness)
    service.db.execute(
        "DELETE FROM delivery_preferences WHERE subscription_id=?", (sub["id"],)
    )
    event_id, delivery = notice(service, user["id"], identity="missing-pref")
    job = service.db.one(
        "SELECT * FROM jobs WHERE kind='account_mail' AND json_extract(payload,'$.delivery_id')=?",
        (delivery["id"],),
    )
    assert job is not None
    assert job["available_at"] <= time.time() + 5
