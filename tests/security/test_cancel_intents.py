import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading

import pytest

from hadan.billing import Billing
from hadan.db import Database
from hadan.service import Problem
from hadan.worker import Worker


def paid_fixture(harness):
    _, service, login = harness
    client, user = login("alice@example.com")
    sub = client.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    service.db.execute(
        "UPDATE products SET paid=1,price_id='price_fixture' WHERE id='quantgt'"
    )
    service.db.execute(
        "UPDATE subscriptions SET billing_id='sub_fixture',expires_at=?,auto_renew=1 WHERE id=?",
        (time.time() + 86400, sub["id"]),
    )
    return service, client, user, sub


def remote_subscription(
    user_id, *, paid=True, billing_id="sub_fixture", cancelled=False
):
    return {
        "id": billing_id,
        "metadata": {"user_id": user_id, "product_id": "quantgt"},
        "items": {"data": [{"price": {"id": "price_fixture"}}]},
        "current_period_end": int(time.time()) + 86400,
        "status": "active" if paid else "incomplete",
        "cancel_at_period_end": cancelled,
        "latest_invoice": {
            "id": "invoice_fixture",
            "status": "paid" if paid else "open",
            "amount_paid": 1000 if paid else 0,
            "currency": "usd",
        },
    }


def test_completed_cancel_then_provider_resume_ignores_stale_job(harness, monkeypatch):
    service, _, user, sub = paid_fixture(harness)
    service.cancel_subscription(user["id"], sub["id"])
    first_id = service.db.one(
        "SELECT cancel_request_id FROM subscriptions WHERE id=?", (sub["id"],)
    )["cancel_request_id"]
    billing = Billing(service)
    calls = []
    remote = remote_subscription(user["id"])

    def provider(method, path, data=None, identity=None):
        calls.append((method, path, data, identity))
        return remote

    monkeypatch.setattr(billing, "api", provider)
    billing.cancel(sub["id"], first_id)
    assert len([c for c in calls if c[0] == "POST"]) == 1
    # Provider truth later shows an explicit user resume of auto-renewal.
    billing.reconcile("sub_fixture", "event_user_resumed")
    assert (
        service.db.one("SELECT auto_renew FROM subscriptions WHERE id=?", (sub["id"],))[
            "auto_renew"
        ]
        == 1
    )
    calls.clear()
    billing.cancel(sub["id"], first_id)
    assert calls == []
    assert (
        service.db.one("SELECT auto_renew FROM subscriptions WHERE id=?", (sub["id"],))[
            "auto_renew"
        ]
        == 1
    )


def test_old_generation_cannot_cancel_or_clear_a_new_intent(harness, monkeypatch):
    service, _, user, sub = paid_fixture(harness)
    billing = Billing(service)
    calls = []
    monkeypatch.setattr(
        billing, "api", lambda *args, **kwargs: calls.append((args, kwargs)) or {}
    )
    service.cancel_subscription(user["id"], sub["id"])
    old_id = service.db.one(
        "SELECT cancel_request_id FROM subscriptions WHERE id=?", (sub["id"],)
    )["cancel_request_id"]
    billing.cancel(sub["id"], old_id)
    service.cancel_subscription(user["id"], sub["id"])
    new_id = service.db.one(
        "SELECT cancel_request_id FROM subscriptions WHERE id=?", (sub["id"],)
    )["cancel_request_id"]
    assert new_id and new_id != old_id
    calls.clear()
    billing.cancel(sub["id"], old_id)
    assert calls == []
    actual = service.db.one(
        "SELECT cancel_requested,cancel_request_id FROM subscriptions WHERE id=?",
        (sub["id"],),
    )
    assert actual["cancel_requested"] == 1 and actual["cancel_request_id"] == new_id
    billing.cancel(sub["id"], new_id)
    assert len(calls) == 1
    assert calls[0][1]["identity"].endswith(":" + new_id)


def test_pending_checkout_cancellation_survives_unpaid_then_paid_reconciliation(
    harness, monkeypatch
):
    _, service, login = harness
    client, user = login("alice@example.com")
    service.db.execute(
        "UPDATE products SET paid=1,price_id='price_fixture' WHERE id='quantgt'"
    )
    service.db.execute("UPDATE jobs SET state='complete' WHERE kind='login_mail'")
    billing = Billing(service)
    remote = remote_subscription(user["id"], paid=False)
    calls = []

    def provider(self, method, path, data=None, identity=None):
        calls.append((method, path, data, identity))
        if path == "checkout/sessions":
            return {"id": "cs_test_fixture", "url": "https://checkout.example/fixture"}
        if method == "POST" and path == "subscriptions/sub_fixture":
            remote["cancel_at_period_end"] = True
        return remote

    monkeypatch.setattr(Billing, "api", provider)
    assert (
        client.post("/api/checkout", json={"product_id": "quantgt", "accept_terms": True, "terms_version": "2026-10-28"}).status_code == 200
    )
    sub = service.db.one("SELECT * FROM subscriptions WHERE user_id=?", (user["id"],))
    assert (
        sub["status"] == "pending" and service.entitled(user["id"], "quantgt") is None
    )
    assert client.post("/api/subscriptions/" + sub["id"] + "/cancel").status_code == 200
    billing.reconcile("sub_fixture", "event_unpaid")
    assert service.entitled(user["id"], "quantgt") is None
    assert Worker(service).run_once() is True
    assert any(
        method == "POST" and path == "subscriptions/sub_fixture"
        for method, path, _, _ in calls
    )
    remote["latest_invoice"]["status"] = "paid"
    remote["latest_invoice"]["amount_paid"] = 1000
    remote["status"] = "active"
    billing.reconcile("sub_fixture", "event_paid_late")
    actual = service.db.one("SELECT * FROM subscriptions WHERE id=?", (sub["id"],))
    assert actual["auto_renew"] == 0
    assert actual["email_enabled"] == 0 and actual["agent_enabled"] == 0
    assert remote["cancel_at_period_end"] is True


def test_pending_checkout_does_not_grant_private_report_or_attachment(
    harness, monkeypatch
):
    _, service, login = harness
    client, user = login("alice@example.com")
    service.db.execute(
        "UPDATE products SET paid=1,price_id='price_fixture' WHERE id='quantgt'"
    )
    published = service.publish(
        "quantgt",
        "private-before-checkout",
        "Private",
        "Paid content",
        files=[{"name": "file.txt", "data": b"private"}],
    )
    aid = service.db.one("SELECT id FROM attachments")["id"]
    monkeypatch.setattr(
        Billing,
        "api",
        lambda *_args, **_kwargs: {"id": "cs_test_fixture", "url": "https://checkout.example/fixture"},
    )
    assert (
        client.post("/api/checkout", json={"product_id": "quantgt", "accept_terms": True, "terms_version": "2026-10-28"}).status_code == 200
    )
    assert service.entitled(user["id"], "quantgt") is None
    assert client.get("/api/content/" + published["id"]).status_code == 404
    assert client.get("/api/files/" + aid).status_code == 404
    assert client.get("/api/events").json()["events"] == []


def test_conflicting_provider_mapping_cannot_overwrite_existing_subscription(
    harness, monkeypatch
):
    service, _, user, sub = paid_fixture(harness)
    billing = Billing(service)
    remote = remote_subscription(user["id"], billing_id="sub_other")
    monkeypatch.setattr(billing, "api", lambda *_args, **_kwargs: remote)
    with pytest.raises(Problem):
        billing.reconcile("sub_other", "event_conflicting")
    assert (
        service.db.one("SELECT billing_id FROM subscriptions WHERE id=?", (sub["id"],))[
            "billing_id"
        ]
        == "sub_fixture"
    )


@pytest.mark.parametrize(
    "endpoint", ["/api/bindings", "/api/admin/publish", "/api/checkout"]
)
def test_blocking_integration_route_does_not_stall_health_requests(
    harness, monkeypatch, endpoint
):
    _, service, login = harness
    client, _ = login("owner@example.com")
    entered = threading.Event()
    release = threading.Event()
    if endpoint == "/api/bindings":
        original = service.new_binding
        body = {"name": "fixture-agent", "mode": "poll"}
    elif endpoint == "/api/admin/publish":
        original = service.publish
        body = {"product_id": "quantgt", "title": "Fixture", "body": "Body"}
    else:
        original = lambda *_args, **_kwargs: {"id": "cs_test_fixture", "url": "https://checkout.example/fixture"}
        body = {"product_id": "quantgt", "accept_terms": True, "terms_version": "2026-10-28"}

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release simulated integration"
        return original(*args, **kwargs)

    if endpoint == "/api/bindings":
        monkeypatch.setattr(service, "new_binding", slow)
    elif endpoint == "/api/admin/publish":
        monkeypatch.setattr(service, "publish", slow)
    else:
        monkeypatch.setattr(Billing, "checkout", slow)
    # Enter TestClient so both requests run on one persistent ASGI event loop.
    with client, ThreadPoolExecutor(max_workers=2) as pool:
        operation = pool.submit(client.post, endpoint, json=body)
        assert entered.wait(2)
        health = pool.submit(client.get, "/health/live")
        try:
            assert health.result(timeout=1).status_code == 200
        finally:
            release.set()
        assert operation.result(timeout=5).status_code == 200


def test_additive_cancellation_migration_preserves_existing_consent(tmp_path):
    from hadan import db as db_module

    schema = Path(db_module.__file__).with_name("schema.sql").read_text()
    old_schema = schema.replace(
        "cancel_requested INTEGER NOT NULL DEFAULT 0,", ""
    ).replace("cancel_request_id TEXT,", "")
    path = tmp_path / "old-schema.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO users VALUES('user-fixture','alice@example.com',?)",
            (time.time(),),
        )
        db.execute("INSERT INTO products(id,name) VALUES('quantgt','Fixture')")
        db.execute(
            "INSERT INTO subscriptions(id,user_id,product_id,email_enabled,agent_enabled,created_at) VALUES('sub-local','user-fixture','quantgt',0,0,?)",
            (time.time(),),
        )
        db.execute("PRAGMA user_version=1")
    database = Database(path)
    database.initialize()
    database.initialize()
    actual = database.one("SELECT * FROM subscriptions WHERE id='sub-local'")
    assert actual["email_enabled"] == 0 and actual["agent_enabled"] == 0
    assert actual["cancel_requested"] == 0 and actual["cancel_request_id"] is None
