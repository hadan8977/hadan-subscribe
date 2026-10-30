import json
import time

from hadan.billing import Billing
from hadan.security import mac


def signed(service, event, *, stamp=None):
    stamp = int(time.time()) if stamp is None else stamp
    body = json.dumps(event, separators=(",", ":")).encode()
    signature = mac(
        service.settings.stripe_webhook_secret, str(stamp) + "." + body.decode()
    )
    return body, "t=" + str(stamp) + ",v1=" + signature


def test_payment_callback_rejects_tamper_expiry_and_deduplicates(harness):
    app, service, login = harness
    service.settings.stripe_webhook_secret = "fixture-webhook-secret"
    client, _ = login("alice@example.com")
    event = {
        "id": "evt_fixture",
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_fixture"}},
    }
    body, signature = signed(service, event)
    assert (
        client.post(
            "/webhooks/stripe",
            content=body + b" ",
            headers={"stripe-signature": signature},
        ).status_code
        == 400
    )
    old_body, old_signature = signed(service, event, stamp=int(time.time()) - 600)
    assert (
        client.post(
            "/webhooks/stripe",
            content=old_body,
            headers={"stripe-signature": old_signature},
        ).status_code
        == 400
    )
    assert service.db.all("SELECT * FROM billing_events") == []
    for _ in range(2):
        assert (
            client.post(
                "/webhooks/stripe",
                content=body,
                headers={"stripe-signature": signature},
            ).status_code
            == 200
        )
    assert len(service.db.all("SELECT * FROM billing_events")) == 1
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='billing_reconcile'")) == 1
    assert service.db.all("SELECT * FROM subscriptions") == []


def test_unpaid_invoice_cannot_grant_entitlement_and_paid_sync_preserves_optout(
    harness, monkeypatch
):
    _, service, login = harness
    client, user = login("alice@example.com")
    sub = client.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    service.db.execute(
        "UPDATE products SET paid=1,price_id='price_fixture' WHERE id='quantgt'"
    )
    service.db.execute(
        "UPDATE subscriptions SET email_enabled=0,agent_enabled=0,expires_at=? WHERE id=?",
        (time.time() - 1, sub["id"]),
    )
    remote = {
        "id": "sub_fixture",
        "metadata": {"user_id": user["id"], "product_id": "quantgt"},
        "items": {"data": [{"price": {"id": "price_fixture"}}]},
        "current_period_end": int(time.time()) + 86400,
        "status": "active",
        "latest_invoice": {
            "id": "in_fixture",
            "status": "open",
            "amount_paid": 0,
            "currency": "usd",
        },
    }
    billing = Billing(service)
    monkeypatch.setattr(billing, "api", lambda *_args, **_kwargs: remote)
    billing.reconcile("sub_fixture", "evt_unpaid")
    assert service.entitled(user["id"], "quantgt") is None
    remote["latest_invoice"]["status"] = "paid"
    remote["latest_invoice"]["amount_paid"] = 1000
    billing.reconcile("sub_fixture", "evt_paid")
    actual = service.entitled(user["id"], "quantgt")
    assert actual is not None
    assert actual["email_enabled"] == 0
    assert actual["agent_enabled"] == 0
    billing.reconcile("sub_fixture", "evt_duplicate_paid")
    assert len(service.db.all("SELECT * FROM payments")) == 1


def test_agent_credential_cannot_checkout_or_control_billing(harness, bind_agent):
    app, service, login = harness
    client, user = login("alice@example.com")
    binding, auth = bind_agent(client)
    # Remove human browser identity; only the Agent credential remains.
    client.cookies.clear()
    assert (
        client.post(
            "/api/checkout", headers=auth, json={"product_id": "quantgt"}
        ).status_code
        == 401
    )
    assert client.post("/api/billing/portal", headers=auth).status_code == 401


def test_cancelling_paid_delivery_preserves_paid_access_and_queues_renewal_cancel(
    harness,
):
    _, service, login = harness
    client, user = login("alice@example.com")
    sub = client.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    service.db.execute(
        "UPDATE subscriptions SET expires_at=?,billing_id='sub_fixture',auto_renew=1 WHERE id=?",
        (time.time() + 86400, sub["id"]),
    )
    result = client.post("/api/subscriptions/" + sub["id"] + "/cancel")
    assert result.status_code == 200
    actual = service.entitled(user["id"], "quantgt")
    assert actual is not None
    assert actual["email_enabled"] == 0 and actual["agent_enabled"] == 0
    assert actual["auto_renew"] == 0
    assert (
        service.db.one("SELECT state FROM jobs WHERE kind='billing_cancel'")["state"]
        == "queued"
    )
