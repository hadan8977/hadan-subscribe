"""Fault injection uses a local mailbox or an in-memory transport only."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from hadan.transport import DeliveryError, Transport
from hadan.worker import Worker


class FaultyLocalTransport:
    def __init__(self, settings, errors=()):
        self.local = Transport(settings)
        self.errors = list(errors)
        self.identities = []

    def mail(self, recipient, subject, body, **kwargs):
        self.identities.append(kwargs["identity"])
        if self.errors:
            raise self.errors.pop(0)
        return self.local.mail(recipient, subject, body, **kwargs)


def make_delivery(journey, *, key="report"):
    reader = journey.login()
    subscription = journey.subscribe(reader)
    item = journey.service.publish(
        "quantgt", key, "Report", "Body", files=[{"name": "a.txt", "data": b"one"}]
    )
    return reader, subscription, item


def ready_retries(journey):
    journey.service.db.execute("UPDATE jobs SET available_at=0 WHERE state='queued'")


def test_content_email_carries_stable_event_and_content_ids(journey):
    reader = journey.login()
    journey.subscribe(reader)
    item = journey.service.publish("quantgt", "email-ids", "Report", "Body")
    journey.drain()
    message = journey.messages(login=False)[-1]
    assert message["X-Quant-Event-ID"] == item["event_id"]
    assert message["X-Quant-Content-ID"] == item["id"]
    assert message["X-Quant-Version"] == str(item["version"])


def test_identical_publication_replay_and_worker_restart_do_not_duplicate_mail(journey):
    _, _, item = make_delivery(journey)
    same = journey.service.publish(
        "quantgt", "report", "Report", "Body", files=[{"name": "a.txt", "data": b"one"}]
    )
    assert same["unchanged"] is True and same["id"] == item["id"]
    journey.drain()
    assert len(journey.messages(login=False)) == 1
    journey.service.replay(item["event_id"], apply=True)
    journey.drain(worker=Worker(journey.service))
    assert len(journey.messages(login=False)) == 1
    assert journey.service.db.one("SELECT count(*) n FROM events")["n"] == 1
    assert journey.service.db.one("SELECT count(*) n FROM receipts")["n"] == 1
    assert journey.service.db.one("SELECT count(*) n FROM deliveries")["n"] == 1


def test_transient_failure_retries_same_identity_then_accepts_once(journey):
    _, _, item = make_delivery(journey)
    transport = FaultyLocalTransport(
        journey.settings, [DeliveryError("temporary relay outage")]
    )
    worker = Worker(journey.service, transport)
    assert worker.run_once()
    delivery = journey.service.db.one(
        "SELECT * FROM deliveries WHERE event_id=?", (item["event_id"],)
    )
    assert delivery["state"] == "retrying"
    assert journey.messages(login=False) == []
    assert not worker.run_once(), "Retry should respect its persisted backoff"
    ready_retries(journey)
    journey.drain(worker=worker)
    assert len(transport.identities) == 2 and len(set(transport.identities)) == 1
    assert len(journey.messages(login=False)) == 1
    assert (
        journey.service.db.one(
            "SELECT state FROM deliveries WHERE id=?", (delivery["id"],)
        )["state"]
        == "accepted"
    )
    assert (
        journey.service.db.one(
            "SELECT state FROM receipts WHERE event_id=?", (item["event_id"],)
        )["state"]
        == "pending"
    )


def test_unsubscribe_wins_over_a_queued_retry(journey):
    reader, sub, _ = make_delivery(journey)
    transport = FaultyLocalTransport(
        journey.settings, [DeliveryError("temporary relay outage")]
    )
    worker = Worker(journey.service, transport)
    assert worker.run_once()
    assert (
        journey.write(
            reader,
            "PATCH",
            f"/api/subscriptions/{sub['id']}",
            json={"email_enabled": False},
        ).status_code
        == 200
    )
    ready_retries(journey)
    journey.drain(worker=worker)
    assert len(transport.identities) == 1
    assert journey.messages(login=False) == []
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "skipped"


def test_suppressed_recipient_is_not_sent_queued_content(journey):
    _, _, _ = make_delivery(journey)
    journey.service.db.execute(
        "INSERT INTO suppressions VALUES(?,?,?)",
        ("reader@example.test", "hard_bounce", time.time()),
    )
    journey.drain()
    assert journey.messages(login=False) == []
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "skipped"


def test_smtp_uncertain_outcome_stays_uncertain_on_replay(journey):
    _, _, item = make_delivery(journey)
    transport = FaultyLocalTransport(
        journey.settings,
        [DeliveryError("SMTP result unknown", retry=False, uncertain=True)],
    )
    worker = Worker(journey.service, transport)
    assert worker.run_once()
    assert (
        journey.service.db.one("SELECT state FROM deliveries")["state"] == "uncertain"
    )
    assert (
        journey.service.db.one("SELECT state FROM jobs WHERE kind='deliver'")["state"]
        == "uncertain"
    )
    assert journey.service.db.one("SELECT count(*) n FROM incidents")["n"] == 1
    journey.service.replay(item["event_id"], apply=True)
    ready_retries(journey)
    journey.drain(worker=worker)
    assert len(transport.identities) == 1
    assert journey.messages(login=False) == []


def test_expired_smtp_sending_lease_does_not_repeat_remote_effect(journey):
    _, _, _ = make_delivery(journey)
    # No SMTP client is invoked: changing the mode only exercises crash recovery policy.
    journey.settings.mail_backend = "smtp"
    transport = FaultyLocalTransport(
        journey.settings, [AssertionError("must never send")]
    )
    worker = Worker(journey.service, transport)
    job = worker.claim()
    assert job["kind"] == "deliver"
    delivery_id = json.loads(job["payload"])["delivery_id"]
    journey.service.db.execute(
        "UPDATE deliveries SET state='sending' WHERE id=?", (delivery_id,)
    )
    journey.service.db.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"])
    )
    assert worker.claim() is None
    assert (
        journey.service.db.one(
            "SELECT state FROM deliveries WHERE id=?", (delivery_id,)
        )["state"]
        == "uncertain"
    )
    assert transport.identities == []


def test_file_mail_crash_after_side_effect_recovers_without_second_message(journey):
    _, _, _ = make_delivery(journey)
    worker = Worker(journey.service)
    job = worker.claim()
    worker.dispatch(job)
    assert len(journey.messages(login=False)) == 1
    # Simulate losing completion acknowledgement before the worker commits the job.
    delivery_id = json.loads(job["payload"])["delivery_id"]
    journey.service.db.execute(
        "UPDATE deliveries SET state='sending' WHERE id=?", (delivery_id,)
    )
    journey.service.db.execute(
        "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"])
    )
    journey.drain(worker=Worker(journey.service))
    assert len(journey.messages(login=False)) == 1
    assert (
        journey.service.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))[
            "state"
        ]
        == "complete"
    )


def test_two_workers_cannot_claim_the_same_job(journey):
    _, _, _ = make_delivery(journey)
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return Worker(journey.service).claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    jobs = [result for result in results if result]
    assert len(jobs) == 1
    assert (
        journey.service.db.one(
            "SELECT attempts FROM jobs WHERE id=?", (jobs[0]["id"],)
        )["attempts"]
        == 1
    )


def test_email_is_sent_even_after_agent_processed_event(journey):
    reader = journey.login()
    journey.subscribe(reader)
    _, agent = journey.connect(reader)
    item = journey.service.publish("quantgt", "agent-first", "Agent first", "Body")
    assert agent.post(f"/api/events/{item['event_id']}/ack").status_code == 200
    journey.drain()
    assert len(journey.messages(login=False)) == 1
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "accepted"


def test_new_version_supersedes_queued_old_version_mail(journey):
    _, _, item = make_delivery(journey)
    revision = journey.service.publish(
        "quantgt",
        "report",
        "Corrected report",
        "Corrected body",
        changes="Corrected data",
    )
    assert revision["id"] == item["id"]
    journey.drain()
    messages = journey.messages(login=False)
    assert len(messages) == 1 and messages[0]["Subject"] == "Corrected report"
    states = {
        r["event_id"]: r["state"]
        for r in journey.service.db.all("SELECT event_id,state FROM deliveries")
    }
    assert states[item["event_id"]] == "skipped"
    assert states[revision["event_id"]] == "accepted"


def test_webhook_retries_use_same_event_and_do_not_mark_processing_complete(
    journey, monkeypatch
):
    reader = journey.login()
    sub = journey.subscribe(reader)
    journey.write(
        reader,
        "PATCH",
        f"/api/subscriptions/{sub['id']}",
        json={"email_enabled": False},
    )
    # Endpoint validation is tested separately. There is no DNS request or socket here.
    monkeypatch.setattr("hadan.service.webhook_target", lambda endpoint: None)
    created = journey.write(
        reader,
        "POST",
        "/api/bindings",
        json={
            "name": "Webhook agent",
            "mode": "webhook",
            "endpoint": "https://agent.example.test/inbox",
        },
    )
    assert created.status_code == 200

    class Receiver:
        def __init__(self):
            self.received = []
            self.tests = []

        def webhook(self, url, payload, secret, event_id):
            if payload.get("type") == "connection.test":
                self.tests.append(event_id)
                return "202"
            self.received.append((event_id, payload))
            if len(self.received) == 1:
                raise DeliveryError("Webhook HTTP 503")
            return "202"

    receiver = Receiver()
    worker = Worker(journey.service, receiver)
    agent = journey.client()
    agent.headers["Authorization"] = "Bearer " + created.json()["token"]
    agent.get("/api/agent/test")
    agent.get("/api/agent/test-file")
    assert agent.post("/api/agent/test/complete").status_code == 409
    assert worker.run_once()
    assert len(receiver.tests) == 1
    assert agent.post("/api/agent/test/complete").status_code == 200
    item = journey.service.publish(
        "quantgt",
        "hook",
        "Hook report",
        "Body",
        files=[{"name": "a.txt", "data": b"file"}],
    )
    journey.drain(worker=worker)
    ready_retries(journey)
    journey.drain(worker=worker)
    assert [event for event, _ in receiver.received] == [
        item["event_id"],
        item["event_id"],
    ]
    assert receiver.received[1][1]["attachments"][0]["name"] == "a.txt"
    assert "Body" not in json.dumps(receiver.received[1][1])
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "accepted"
    assert journey.service.db.one("SELECT state FROM receipts")["state"] == "pending"


def test_opt_out_committed_before_sending_state_prevents_the_send(journey, monkeypatch):
    reader, sub, _ = make_delivery(journey)
    original_entitled = journey.service.entitled
    closed = False

    def opt_out_after_entitlement_read(user_id, product_id):
        nonlocal closed
        snapshot = original_entitled(user_id, product_id)
        if not closed:
            closed = True
            journey.service.preferences(user_id, sub["id"], {"email_enabled": False})
        return snapshot

    # Deterministic interleaving: unsubscribe commits after candidate selection, before send.
    monkeypatch.setattr(journey.service, "entitled", opt_out_after_entitlement_read)
    journey.drain()
    assert journey.messages(login=False) == [], (
        "A committed opt-out must win before a send begins"
    )
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "skipped"


def test_baseline_replay_creates_missing_delivery_then_deduplicates(journey):
    reader = journey.login()
    journey.subscribe(reader)
    item = journey.service.publish(
        "quantgt",
        "repair-baseline",
        "Recovered publication",
        "Missed while source was unavailable",
        baseline=True,
    )
    journey.drain()
    assert journey.messages(login=False) == []
    preview = journey.service.replay(item["event_id"], apply=False)
    assert preview["eligible_users"] == 1
    assert journey.service.db.one("SELECT count(*) n FROM receipts")["n"] == 0
    journey.service.replay(item["event_id"], apply=True)
    journey.drain()
    assert len(journey.messages(login=False)) == 1, (
        "Repair replay must enqueue a delivery that was never created"
    )
    journey.service.replay(item["event_id"], apply=True)
    journey.drain()
    assert len(journey.messages(login=False)) == 1


def test_agent_ack_before_email_dispatch_does_not_cancel_email(journey):
    reader = journey.login()
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    item = journey.service.publish("quantgt", "ack-race", "Report", "Body")
    assert journey.service.ack(
        journey.service.agent(binding["token"]), item["event_id"]
    ) == {"state": "processed"}
    journey.drain()
    assert len(journey.messages(login=False)) == 1
    assert journey.service.db.one("SELECT state FROM deliveries")["state"] == "accepted"
