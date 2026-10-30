"""Independent commerce review: synthetic users, local files, no provider traffic."""
from __future__ import annotations

import csv
import hashlib
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from hadan.commerce import Commerce, code_digest
from hadan.db import uid
from hadan.service import Problem, Service
from hadan.worker import Worker


PLAN = {"id": "month", "title": "Synthetic report access", "days": 30,
        "amount_minor": 1234, "currency": "CNY"}
ORIGINAL_PROVIDER_API = Commerce.api


@pytest.fixture
def commerce_env(harness, monkeypatch):
    app, service, login = harness
    cfg = service.settings
    cfg.base_url = "https://testserver"
    cfg.xorpay_enabled = True
    cfg.xorpay_aid = "123456789"
    cfg.xorpay_secret = "synthetic-review-secret-not-a-real-credential"
    cfg.payment_plans = [dict(PLAN)]
    cfg.xianyu_url = ""
    cfg.stripe_key = ""
    cfg.stripe_webhook_secret = ""
    cfg.deepseek_key = ""
    cfg.mail_backend = "file"
    service.db.execute("UPDATE products SET paid=1 WHERE id='quantgt'")

    def deny_network(*_args, **_kwargs):
        raise AssertionError("A commerce review test attempted provider traffic")

    monkeypatch.setattr(Commerce, "api", deny_network)
    return app, service, Commerce(service), login


def issue(commerce, directory: Path, *, count=1, days=30, expires_at=None):
    target = directory / ("review-codes-" + uid() + ".csv")
    result = commerce.issue_codes(days=days, count=count, output=target,
                                  expires_at=expires_at, actor="review")
    with target.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == count
    return result, [row["code"] for row in rows], target


def subscription(service, user, *, expires_at=None, status="active", email=False,
                 agent=False, auto_renew=False, billing_id=None):
    existing = service.db.one("SELECT id FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user["id"],))
    if existing:
        sid = existing["id"]
        service.db.execute(
            "UPDATE subscriptions SET status=?,expires_at=?,email_enabled=?,agent_enabled=?,"
            "email_format='full',auto_renew=?,billing_id=? WHERE id=?",
            (status, expires_at, int(email), int(agent), int(auto_renew), billing_id, sid),
        )
        service.db.execute("UPDATE delivery_preferences SET email_policy='always' WHERE subscription_id=?", (sid,))
        return sid
    sid = uid()
    service.db.execute(
        "INSERT INTO subscriptions(id,user_id,product_id,status,expires_at,email_enabled,"
        "agent_enabled,email_format,auto_renew,billing_id,created_at) VALUES(?,?,'quantgt',?,?,?,?,?,?,?,?)",
        (sid, user["id"], status, expires_at, int(email), int(agent), "full",
         int(auto_renew), billing_id, time.time()),
    )
    service.db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
    return sid


def fake_gateway(monkeypatch, *, remote_id=None, hook=None, qr=None):
    calls = []

    def api(self, method, path, data=None):
        calls.append((method, path, dict(data or {})))
        identity = remote_id or "remote-" + data["order_id"]
        if hook:
            hook(self, data, identity)
        link = qr or ("https://qr.alipay.com/review-" + data["order_id"]
                      if data["pay_type"] == "alipay"
                      else "weixin://wxpay/bizpayurl?pr=review-" + data["order_id"])
        return {"status": "ok", "aoid": identity, "info": {"qr": link}}

    monkeypatch.setattr(Commerce, "api", api)
    return calls


def signed_notice(commerce, order, *, amount="12.34", remote=None, **extra):
    record = commerce.s.db.one("SELECT * FROM purchase_orders WHERE id=?", (order,))
    fields = {
        "aoid": remote or record["remote_id"], "order_id": order,
        "pay_price": amount, "pay_time": "2026-10-27 12:34:56",
    }
    fields["sign"] = hashlib.md5(
        (fields["aoid"] + fields["order_id"] + fields["pay_price"]
         + fields["pay_time"] + commerce.cfg.xorpay_secret).encode()
    ).hexdigest()
    fields.update(extra)
    return fields


def create(client, **overrides):
    return client.post("/api/purchase", json={"plan_id": "month", "method": "alipay", "accept_terms": True, "terms_version": "2026-10-28", **overrides})


def test_code_only_export_is_private_and_plaintext_never_enters_database_or_logs(
    commerce_env, tmp_path, caplog
):
    _, service, commerce, login = commerce_env
    client, _ = login("code-reader@example.com")
    result, codes, target = issue(commerce, tmp_path, count=2)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result["plaintext_in_database"] is False
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 200
    connection = service.db.connect()
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    all_observed = dump + caplog.text + json.dumps(result)
    for code in codes:
        assert code not in all_observed
        assert code.replace("-", "") not in all_observed
        assert code.encode() not in service.settings.db_path.read_bytes()
        assert code.replace("-", "").encode() not in service.settings.db_path.read_bytes()
    assert len(service.db.all("SELECT * FROM redemption_codes")) == 2


def test_code_accepts_human_formatting_but_only_once(commerce_env, tmp_path):
    _, service, commerce, login = commerce_env
    alice, user = login("code-alice@example.com")
    bob, _ = login("code-bob@example.com")
    _, codes, _ = issue(commerce, tmp_path)
    response = alice.post("/api/redeem", json={"code": " \n" + codes[0].lower() + "\t"})
    assert response.status_code == 200, response.text
    end = service.entitled(user["id"], "quantgt")["expires_at"]
    assert alice.post("/api/redeem", json={"code": codes[0]}).status_code == 400
    assert bob.post("/api/redeem", json={"code": codes[0]}).status_code == 400
    assert service.entitled(user["id"], "quantgt")["expires_at"] == end
    row = service.db.one("SELECT * FROM redemption_codes")
    assert row["state"] == "redeemed" and row["redeemed_by"] == user["id"]
    assert len(service.db.all("SELECT * FROM events WHERE kind='account:access_granted'")) == 1


@pytest.mark.parametrize("unavailable", ["expired", "revoked", "inactive_product"])
def test_unusable_codes_never_consume_or_grant(commerce_env, tmp_path, unavailable):
    _, service, commerce, login = commerce_env
    client, user = login("unusable@example.com")
    batch, codes, _ = issue(commerce, tmp_path)
    if unavailable == "expired":
        service.db.execute("UPDATE code_batches SET expires_at=? WHERE id=?", (time.time()-1, batch["batch_id"]))
    elif unavailable == "revoked":
        assert commerce.revoke_batch(batch["batch_id"])["revoked_unused"] == 1
    else:
        service.db.execute("UPDATE products SET active=0 WHERE id='quantgt'")
    response = client.post("/api/redeem", json={"code": codes[0]})
    assert response.status_code in (400, 409), response.text
    assert service.entitled(user["id"], "quantgt") is None
    row = service.db.one("SELECT * FROM redemption_codes")
    assert row["redeemed_by"] is None and row["redeemed_at"] is None


def test_batch_revocation_preserves_previously_redeemed_entitlement(commerce_env, tmp_path):
    _, service, commerce, login = commerce_env
    client, user = login("batch@example.com")
    batch, codes, _ = issue(commerce, tmp_path, count=3)
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 200
    end = service.entitled(user["id"], "quantgt")["expires_at"]
    assert commerce.revoke_batch(batch["batch_id"])["revoked_unused"] == 2
    assert commerce.revoke_batch(batch["batch_id"])["revoked_unused"] == 0
    assert service.entitled(user["id"], "quantgt")["expires_at"] == end
    assert service.db.one("SELECT state FROM redemption_codes WHERE digest=?", (code_digest(codes[0]),))["state"] == "redeemed"


def test_concurrent_redemption_has_exactly_one_winner(commerce_env, tmp_path):
    _, service, commerce, _ = commerce_env
    users = [uid() for _ in range(8)]
    for number, user_id in enumerate(users):
        service.db.execute("INSERT INTO users VALUES(?,?,?)", (user_id, f"race{number}@example.com", time.time()))
    _, codes, _ = issue(commerce, tmp_path)
    start = threading.Barrier(len(users))

    def compete(user_id):
        start.wait(timeout=5)
        try:
            Commerce(service).redeem(user_id, codes[0])
            return user_id, 200
        except Problem as exc:
            return user_id, exc.status

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        outcomes = list(pool.map(compete, users))
    winners = [u for u, status in outcomes if status == 200]
    assert len(winners) == 1, outcomes
    assert sorted(status for _, status in outcomes) == [200] + [400]*7
    assert len(service.db.all("SELECT * FROM subscriptions")) == 1
    assert service.db.one("SELECT redeemed_by FROM redemption_codes")["redeemed_by"] == winners[0]


@pytest.mark.parametrize("status", ["active", "ended"])
def test_redemption_preserves_channel_optouts_and_paid_time(commerce_env, tmp_path, status):
    _, service, commerce, login = commerce_env
    client, user = login("optout@example.com")
    old_end = time.time()+10*86400
    sid = subscription(service, user, expires_at=old_end, status=status)
    _, codes, _ = issue(commerce, tmp_path, days=30)
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 200
    row = service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    assert row["expires_at"] == old_end+30*86400
    assert row["email_enabled"] == row["agent_enabled"] == 0
    assert row["email_format"] == "full"
    assert service.db.one("SELECT email_policy FROM delivery_preferences WHERE subscription_id=?", (sid,))["email_policy"] == "always"


@pytest.mark.parametrize("kind", ["lifetime", "autorenew"])
def test_unnecessary_redemption_does_not_burn_code(commerce_env, tmp_path, kind):
    _, service, commerce, login = commerce_env
    client, user = login("existing@example.com")
    sid = subscription(service, user, expires_at=None if kind == "lifetime" else time.time()+86400,
                       auto_renew=kind == "autorenew", billing_id="sub_review" if kind == "autorenew" else None)
    before = service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    _, codes, _ = issue(commerce, tmp_path)
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 409
    assert service.db.one("SELECT state FROM redemption_codes")["state"] == "active"
    assert service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)) == before


def test_code_guess_budget_survives_invalid_code_rollbacks(commerce_env, tmp_path):
    _, _, commerce, login = commerce_env
    client, _ = login("guess@example.com")
    _, codes, _ = issue(commerce, tmp_path)
    for _ in range(12):
        assert client.post("/api/redeem", json={"code": "not-a-code"}).status_code == 400
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 429


def test_existing_export_is_never_overwritten(commerce_env, tmp_path):
    _, service, commerce, _ = commerce_env
    _, _, target = issue(commerce, tmp_path)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        commerce.issue_codes(days=30, count=1, output=target)
    assert target.read_bytes() == before
    assert len(service.db.all("SELECT * FROM code_batches")) == 1


@pytest.mark.parametrize("missing", ["enabled", "aid", "secret", "https", "plans"])
def test_unconfigured_provider_has_no_prices_or_orders(commerce_env, missing):
    _, service, commerce, login = commerce_env
    client, _ = login("disabled@example.com")
    if missing == "enabled":
        commerce.cfg.xorpay_enabled = False
    elif missing == "aid":
        commerce.cfg.xorpay_aid = ""
    elif missing == "secret":
        commerce.cfg.xorpay_secret = ""
    elif missing == "https":
        commerce.cfg.base_url = "http://testserver"
    else:
        commerce.cfg.payment_plans = []
    options = client.get("/api/purchase-options")
    assert options.status_code == 200
    assert options.json()["plans"] == []
    assert options.json()["automatic_renewal"] is False
    methods = {m["id"]:m["enabled"] for m in options.json()["methods"]}
    assert not methods["wechat"] and not methods["alipay"] and not methods["usdt"]
    assert create(client).status_code in (404, 503)
    assert service.db.all("SELECT * FROM purchase_orders") == []


@pytest.mark.parametrize("changes", [{"days": True}, {"amount_minor": True}, {"days": 0},
                                     {"amount_minor": -100}, {"currency": "USDT"}])
def test_invalid_admin_plan_fails_closed(commerce_env, changes):
    _, service, commerce, login = commerce_env
    client, _ = login("plans@example.com")
    commerce.cfg.payment_plans = [{**PLAN, **changes}]
    assert client.get("/api/purchase-options").status_code == 503
    assert create(client).status_code == 503
    assert service.db.all("SELECT * FROM purchase_orders") == []


@pytest.mark.parametrize("method", ["alipay", "wechat"])
def test_order_owner_price_duration_and_remote_calls_come_from_server(commerce_env, monkeypatch, method):
    _, service, _, login = commerce_env
    alice, user = login("order-alice@example.com")
    bob, other = login("order-bob@example.com")
    calls = fake_gateway(monkeypatch)
    response = create(alice, method=method, user_id=other["id"], amount_minor=1, days=3650, product_id="notes")
    assert response.status_code == 200, response.text
    order = response.json()
    row = service.db.one("SELECT * FROM purchase_orders WHERE id=?", (order["id"],))
    assert (row["user_id"], row["amount_minor"], row["days"], row["product_id"]) == (user["id"], 1234, 30, "quantgt")
    assert calls[0][2]["price"] == "12.34" and calls[0][2]["pay_type"] == ("native" if method == "wechat" else "alipay")
    if method == "wechat":
        assert order["pay_url"] is None
    assert bob.get("/api/orders/"+order["id"]).status_code == 404
    assert bob.get("/api/orders/"+order["id"]+"/qr").status_code == 404
    qr = alice.get("/api/orders/"+order["id"]+"/qr")
    assert qr.status_code == 200 and "image/png" in qr.headers["content-type"]
    assert qr.content.startswith(b"\x89PNG")
    assert "no-store" in qr.headers["cache-control"]
    second = create(alice, method=method)
    assert second.json()["id"] == order["id"] and len(calls) == 1


def test_agent_cannot_purchase_redeem_or_read_human_payment(commerce_env, tmp_path, monkeypatch, bind_agent):
    app, service, commerce, login = commerce_env
    human, user = login("agent-owner@example.com")
    binding, auth = bind_agent(human)
    fake_gateway(monkeypatch)
    order = create(human).json()
    _, codes, _ = issue(commerce, tmp_path)
    with TestClient(app, raise_server_exceptions=False) as agent:
        agent.headers.update(auth)
        assert agent.post("/api/purchase", json={"plan_id":"month", "method":"alipay"}).status_code == 401
        assert agent.post("/api/redeem", json={"code": codes[0]}).status_code == 401
        assert agent.get("/api/orders/"+order["id"]).status_code == 401
        assert agent.get("/api/orders/"+order["id"]+"/qr").status_code == 401
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.one("SELECT state FROM redemption_codes")["state"] == "active"


def test_purchase_and_redemption_require_csrf(commerce_env, tmp_path):
    _, service, commerce, login = commerce_env
    client, _ = login("csrf@example.com")
    _, codes, _ = issue(commerce, tmp_path)
    del client.headers["X-CSRF-Token"]
    assert create(client).status_code == 403
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 403
    assert service.db.all("SELECT * FROM purchase_orders") == []


@pytest.mark.parametrize("qr", ["https://evil.example/checkout", "http://qr.alipay.com/pay",
                                "https://attacker@qr.alipay.com/pay", "javascript:alert(1)"])
def test_provider_returned_link_is_restricted(commerce_env, monkeypatch, qr):
    _, service, _, login = commerce_env
    client, _ = login("link@example.com")
    fake_gateway(monkeypatch, qr=qr)
    response = create(client)
    assert response.status_code == 502, response.text
    assert service.db.one("SELECT qr_data FROM purchase_orders")["qr_data"] is None


@pytest.mark.parametrize("amount", ["0.01", "12.3401", "NaN", "Infinity", "-12.34", "0"])
def test_even_validly_signed_wrong_amount_never_grants(commerce_env, monkeypatch, amount):
    _, service, commerce, login = commerce_env
    client, user = login("amount@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    callback = signed_notice(commerce, order["id"], amount=amount)
    response = client.post("/webhooks/xorpay", data=callback)
    assert response.status_code == 400, response.text
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments") == []


def test_signature_tamper_and_duplicate_form_fields_fail_closed(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, _ = login("signature@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    assert client.post("/webhooks/xorpay", data={**fields,"pay_price":"0.01"}).status_code == 400
    assert client.post("/webhooks/xorpay", data={**fields,"sign":"0"*32}).status_code == 400
    duplicate = urlencode(fields) + "&order_id=" + fields["order_id"]
    assert client.post("/webhooks/xorpay", content=duplicate, headers={"content-type":"application/x-www-form-urlencoded"}).status_code == 400
    assert client.post("/webhooks/xorpay", json=fields).status_code == 415
    assert service.db.all("SELECT * FROM payments") == []


def test_duplicate_callback_and_unsigned_payload_cannot_change_entitlement(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("pay-alice@example.com")
    _, other = login("pay-bob@example.com")
    old_end = time.time()+86400
    sid = subscription(service, user, expires_at=old_end)
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"],
                           more=json.dumps({"user_id":other["id"],"days":3650,"product_id":"notes"}),
                           detail=json.dumps({"user_id":other["id"],"amount":1}),
                           user_id=other["id"], days="3650", product_id="notes")
    for _ in range(3):
        response = client.post("/webhooks/xorpay", data=fields)
        assert response.status_code == 200, response.text
    row = service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    assert row["expires_at"] == old_end+30*86400
    assert row["email_enabled"] == row["agent_enabled"] == 0
    assert service.entitled(other["id"], "quantgt") is None
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert len(service.db.all("SELECT * FROM events WHERE kind='account:payment_succeeded'")) == 1
    assert client.get("/api/orders/"+order["id"]).json()["paid"] is True
    assert client.get("/api/orders/"+order["id"]+"/qr").status_code == 410


def test_callback_before_create_response_does_not_regress_to_pending(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("early@example.com")

    def early(self, data, identity):
        assert self.notify(signed_notice(self, data["order_id"], remote=identity))["received"] is True

    fake_gateway(monkeypatch, hook=early)
    response = create(client)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paid"
    assert response.json()["pay_url"] is None and response.json()["qr_url"] is None
    assert service.entitled(user["id"], "quantgt") is not None
    assert len(service.db.all("SELECT * FROM payments")) == 1


def test_provider_remote_order_collision_is_a_controlled_rejection(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    alice, owner = login("collision-alice@example.com")
    bob, other = login("collision-bob@example.com")
    fake_gateway(monkeypatch, remote_id="same-remote-id")
    first = create(alice)
    assert first.status_code == 200
    second = create(bob)
    assert second.status_code in (400, 409, 502), second.text
    assert service.entitled(other["id"], "quantgt") is None
    assert len(service.db.all("SELECT * FROM purchase_orders WHERE remote_id='same-remote-id'")) == 1
    first_notice = signed_notice(commerce, first.json()["id"])
    assert alice.post("/webhooks/xorpay", data=first_notice).status_code == 200
    assert service.entitled(owner["id"], "quantgt") is not None


def test_collision_callback_cannot_grant_second_owner(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    alice, _ = login("callback-alice@example.com")
    bob, user = login("callback-bob@example.com")
    fake_gateway(monkeypatch)
    first, second = create(alice).json(), create(bob).json()
    first_remote = service.db.one("SELECT remote_id FROM purchase_orders WHERE id=?", (first["id"],))["remote_id"]
    service.db.execute("UPDATE purchase_orders SET remote_id=NULL,status='creating' WHERE id=?", (second["id"],))
    fields = signed_notice(commerce, second["id"], remote=first_remote)
    response = bob.post("/webhooks/xorpay", data=fields)
    assert response.status_code in (400, 409), response.text
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments WHERE user_id=?", (user["id"],)) == []


def test_money_received_during_lifetime_race_is_recorded_once_for_review(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("paid-review@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    sid = subscription(service, user, expires_at=None)
    fields = signed_notice(commerce, order["id"])
    assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    first = service.db.one("SELECT * FROM purchase_orders WHERE id=?", (order["id"],))
    assert first["status"] == "review"
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert service.db.one("SELECT expires_at FROM subscriptions WHERE id=?", (sid,))["expires_at"] is None
    # Changing account state later must not let a duplicate silently fulfill an unresolved payment.
    finite = time.time()+86400
    service.db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (finite, sid))
    assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.db.one("SELECT status,paid_at FROM purchase_orders WHERE id=?", (order["id"],)) == {"status":"review", "paid_at": first["paid_at"]}
    assert service.db.one("SELECT expires_at FROM subscriptions WHERE id=?", (sid,))["expires_at"] == finite
    assert len(service.db.all("SELECT * FROM payments")) == 1


def test_timeout_keeps_original_order_and_returns_recoverable_error(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, _ = login("timeout@example.com")
    calls = []

    class TimeoutClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, method, url, data=None):
            assert url == "https://xorpay.com/api/pay/" + commerce.cfg.xorpay_aid
            calls.append(data["order_id"])
            raise httpx.ReadTimeout("synthetic gateway timeout")

    # Exercise the production adapter's own exception boundary, with no real socket.
    monkeypatch.setattr(Commerce, "api", ORIGINAL_PROVIDER_API)
    monkeypatch.setattr("hadan.commerce.httpx.Client", TimeoutClient)
    response = create(client)
    assert response.status_code == 503, response.text
    rows = service.db.all("SELECT * FROM purchase_orders")
    assert len(rows) == 1 and rows[0]["id"] == calls[0]
    fake_gateway(monkeypatch)
    retried = create(client)
    assert retried.status_code == 200, retried.text
    assert retried.json()["id"] == rows[0]["id"]
    assert len(service.db.all("SELECT * FROM purchase_orders")) == 1


def test_concurrent_duplicate_callbacks_commit_one_payment_and_period(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("callback-race@example.com")
    old_end = time.time()+86400
    sid = subscription(service, user, expires_at=old_end)
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    start = threading.Barrier(8)

    def notify(_number):
        start.wait(timeout=5)
        return Commerce(service).notify(fields)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(notify, range(8)))
    assert all(r["received"] is True for r in results)
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert service.db.one("SELECT expires_at FROM subscriptions WHERE id=?", (sid,))["expires_at"] == old_end+30*86400
    assert len(service.db.all("SELECT * FROM events WHERE kind='account:payment_succeeded'")) == 1


def test_concurrent_checkout_reuses_one_local_and_provider_order(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    _, user = login("checkout-race@example.com")
    start = threading.Barrier(2)
    requests = []
    requested = threading.Barrier(2)

    def api(self, method, path, data=None):
        requests.append(data["order_id"])
        requested.wait(timeout=5)
        return {"status":"ok", "aoid":"remote-"+data["order_id"],
                "info":{"qr":"https://qr.alipay.com/review-"+data["order_id"]}}

    monkeypatch.setattr(Commerce, "api", api)

    def begin(_number):
        start.wait(timeout=5)
        return Commerce(service).create_order(user["id"], "month", "alipay")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert len(set(requests)) == 1
    assert len(service.db.all("SELECT * FROM purchase_orders")) == 1


def test_concurrent_export_cannot_replace_other_issuers_codes(commerce_env, tmp_path):
    _, service, commerce, _ = commerce_env
    target = tmp_path / "single-export.csv"
    start = threading.Barrier(2)

    def export(_number):
        start.wait(timeout=5)
        try:
            return Commerce(service).issue_codes(days=30, count=2, output=target)
        except (ValueError, FileExistsError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(export, range(2)))
    assert sum(r is not None for r in results) == 1
    with target.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {code_digest(row["code"]) for row in rows} == {
        row["digest"] for row in service.db.all("SELECT digest FROM redemption_codes")
    }
    assert len(service.db.all("SELECT * FROM code_batches")) == 1
    assert len(rows) == 2 and stat.S_IMODE(target.stat().st_mode) == 0o600


def test_expired_order_hides_payment_links_without_claiming_payment(commerce_env, monkeypatch):
    _, service, _, login = commerce_env
    client, user = login("expired-order@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    service.db.execute("UPDATE purchase_orders SET expires_at=? WHERE id=?", (time.time()-1,order["id"]))
    response = client.get("/api/orders/"+order["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "expired" and response.json()["paid"] is False
    assert response.json()["pay_url"] is response.json()["qr_url"] is None
    assert client.get("/api/orders/"+order["id"]+"/qr").status_code == 410
    assert service.entitled(user["id"], "quantgt") is None


def test_stopping_new_payments_still_settles_signed_inflight_callback(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("disabled-hook@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    commerce.cfg.xorpay_enabled = False
    assert create(client).status_code == 503
    for _ in range(2):
        assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.entitled(user["id"], "quantgt") is not None
    assert len(service.db.all("SELECT * FROM payments")) == 1


def test_removed_provider_credentials_cannot_authenticate_pending_payment(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("removed-key@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    commerce.cfg.xorpay_enabled = False
    commerce.cfg.xorpay_secret = ""
    assert client.post("/webhooks/xorpay", data=fields).status_code == 503
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments") == []


@pytest.mark.parametrize("kind", ["lifetime", "active_renewal", "expired_renewal"])
def test_unnecessary_or_conflicting_purchase_never_calls_gateway(commerce_env, kind):
    _, service, _, login = commerce_env
    client, user = login("blocked-purchase@example.com")
    expires = None if kind == "lifetime" else time.time() + (-86400 if kind == "expired_renewal" else 86400)
    subscription(service, user, expires_at=expires, auto_renew=kind != "lifetime",
                 billing_id="sub_existing" if kind != "lifetime" else None)
    response = create(client)
    assert response.status_code == 409, response.text
    assert service.db.all("SELECT * FROM purchase_orders") == []


@pytest.mark.parametrize("provider_state", ["payed", "success"])
def test_lost_callback_detected_without_grant_until_signed_confirmation(
    commerce_env, monkeypatch, provider_state
):
    _, service, commerce, login = commerce_env
    client, user = login("lost-callback@example.com")
    other, _ = login("notice-isolation@example.com")
    published = service.publish("quantgt", "paid-archive", "Private report", "Private body",
                                files=[{"name":"report.txt", "data":b"private-attachment"}])
    attachment = service.db.one("SELECT id FROM attachments")["id"]
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    assert service.entitled(user["id"], "quantgt") is None
    calls = []

    def query(self, method, path, data=None):
        assert method == "GET" and path.startswith("query2/"+commerce.cfg.xorpay_aid+"?")
        params = parse_qs(urlsplit("https://xorpay.com/api/"+path).query)
        assert params["order_id"] == [order["id"]]
        expected = hashlib.md5((order["id"]+commerce.cfg.xorpay_secret).encode()).hexdigest()
        assert params["sign"] == [expected]
        assert commerce.cfg.xorpay_secret not in path
        calls.append(path)
        return {"status":provider_state}

    monkeypatch.setattr(Commerce, "api", query)
    # Re-open the persisted database through a fresh service before recovering.
    restarted = Commerce(Service(service.settings))
    assert restarted.order(user["id"], order["id"])["status"] == "pending"
    for _ in range(2):
        result = restarted.reconcile(order["id"])
        assert result["needs_review"] is True
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments") == []
    assert client.get("/api/content/"+published["id"]).status_code == 404
    assert client.get("/api/files/"+attachment).status_code == 404
    public = client.get("/api/orders/"+order["id"]).json()
    assert public["status"] == "verifying" and public["paid"] is False
    assert public["pay_url"] is public["qr_url"] is None
    assert create(client).status_code == 409
    notices = service.db.all("SELECT * FROM events WHERE kind='account:payment_verifying'")
    assert len(notices) == 1
    assert notices[0]["id"] in [event["id"] for event in client.get("/api/events").json()["events"]]
    assert notices[0]["id"] not in [event["id"] for event in other.get("/api/events").json()["events"]]
    assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.entitled(user["id"], "quantgt") is not None
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert client.get("/api/content/"+published["id"]).status_code == 200
    assert client.get("/api/files/"+attachment).content == b"private-attachment"
    before = len(calls)
    assert "skipped" in commerce.reconcile(order["id"])
    assert len(calls) == before


@pytest.mark.parametrize("provider_state", ["new", "expire", "not_exist", "fee_error", "unknown"])
def test_query_without_payment_confirmation_never_grants_or_erases_order(
    commerce_env, monkeypatch, provider_state
):
    _, service, commerce, login = commerce_env
    client, user = login("query-state@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    monkeypatch.setattr(Commerce, "api", lambda *_args, **_kwargs: {"status":provider_state})
    commerce.reconcile(order["id"])
    row = service.db.one("SELECT * FROM purchase_orders WHERE id=?", (order["id"],))
    assert row["status"] == "pending"
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments") == []


def test_callback_racing_query_result_cannot_regress_paid_order(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("query-race@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])

    def query(self, method, path, data=None):
        assert method == "GET"
        self.notify(fields)
        return {"status":"success"}

    monkeypatch.setattr(Commerce, "api", query)
    commerce.reconcile(order["id"])
    assert service.db.one("SELECT status FROM purchase_orders WHERE id=?", (order["id"],))["status"] == "paid"
    assert service.entitled(user["id"], "quantgt") is not None
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert service.db.all("SELECT * FROM events WHERE kind='account:payment_verifying'") == []


def test_hourly_reconciliation_schedules_once_and_surfaces_missing_callback(commerce_env, monkeypatch):
    from hadan.schedule import tick

    _, service, commerce, login = commerce_env
    client, user = login("scheduled-query@example.com")
    service.settings.ops_ai = False
    fake_gateway(monkeypatch)
    order = create(client).json()
    now = time.time()
    service.db.execute("UPDATE purchase_orders SET created_at=? WHERE id=?", (now-180, order["id"]))
    for _ in range(2):
        tick(service, datetime.fromtimestamp(now, timezone.utc))
    jobs = service.db.all("SELECT * FROM jobs WHERE kind='purchase_reconcile'")
    assert len(jobs) == 1
    monkeypatch.setattr(Commerce, "api", lambda *_args, **_kwargs: {"status":"payed"})
    Worker(service).dispatch(jobs[0])
    assert service.db.one("SELECT status FROM purchase_orders WHERE id=?", (order["id"],))["status"] == "verifying"
    incident = service.db.one("SELECT * FROM incidents WHERE kind='payment_review'")
    assert incident is not None and order["id"] in incident["details"]
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.all("SELECT * FROM payments") == []


def test_product_withdrawn_after_order_keeps_received_money_for_review(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("removed-product@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    service.db.execute("UPDATE products SET active=0 WHERE id='quantgt'")
    fields = signed_notice(commerce, order["id"])
    for _ in range(2):
        assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.one("SELECT status FROM purchase_orders WHERE id=?", (order["id"],))["status"] == "review"
    assert len(service.db.all("SELECT * FROM payments")) == 1
    assert len(service.db.all("SELECT * FROM events WHERE kind='account:payment_review'")) == 1


def test_disabling_provider_removes_existing_payment_entry_points(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, _ = login("disabled-existing-order@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    commerce.cfg.xorpay_enabled = False
    result = client.get("/api/orders/"+order["id"])
    assert result.status_code == 200
    assert result.json()["paid"] is False
    assert result.json()["pay_url"] is result.json()["qr_url"] is None
    assert client.get("/api/orders/"+order["id"]+"/qr").status_code in (409, 410, 503)
    # Keep the audit/reconciliation identity even though customers cannot keep paying.
    row = service.db.one("SELECT * FROM purchase_orders WHERE id=?", (order["id"],))
    assert row["remote_id"] and row["qr_data"]


def test_provider_callback_needs_signature_but_no_browser_session(commerce_env, monkeypatch):
    app, service, commerce, login = commerce_env
    human, user = login("server-callback@example.com")
    fake_gateway(monkeypatch)
    order = create(human).json()
    fields = signed_notice(commerce, order["id"])
    human.cookies.clear()
    with TestClient(app, raise_server_exceptions=False) as provider:
        assert not provider.cookies
        assert provider.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.entitled(user["id"], "quantgt") is not None
    assert len(service.db.all("SELECT * FROM payments")) == 1


@pytest.mark.parametrize("previous_status", ["review", "verifying"])
@pytest.mark.parametrize("outcome", ["fulfilled", "refunded"])
def test_operator_reconciliation_records_evidence_without_spending_or_granting(
    commerce_env, monkeypatch, previous_status, outcome
):
    _, service, commerce, login = commerce_env
    client, user = login("operator-resolve@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    fields = signed_notice(commerce, order["id"])
    if previous_status == "review":
        service.db.execute("UPDATE products SET active=0 WHERE id='quantgt'")
        assert client.post("/webhooks/xorpay", data=fields).status_code == 200
        service.db.execute("UPDATE products SET active=1 WHERE id='quantgt'")
    else:
        monkeypatch.setattr(Commerce, "api", lambda *_args, **_kwargs: {"status":"payed"})
        commerce.reconcile(order["id"])
    incident_id = Worker(service).incident("payment_review", "Review fixture", "order_id="+order["id"])
    if outcome == "fulfilled":
        subscription(service, user, expires_at=time.time()+30*86400, email=False, agent=False)
    before = service.db.one("SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user["id"],))

    def no_external_action(*_args, **_kwargs):
        raise AssertionError("Resolving evidence must not execute a charge/refund")

    monkeypatch.setattr(Commerce, "api", no_external_action)
    result = commerce.resolve_order(order["id"], outcome=outcome,
                                    reference="provider-receipt-review-123", actor="review-operator")
    assert result["external_charge_or_refund_executed"] is False
    assert result["status"] == "closed" and result["outcome"] == outcome
    assert service.db.one("SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user["id"],)) == before
    ledger = service.db.all("SELECT * FROM payments")
    assert len(ledger) == 1
    assert ledger[0]["provider_event"].startswith("xorpay:" if previous_status == "review" else "manual-reconciliation:")
    assert ledger[0]["amount"] == PLAN["amount_minor"]
    log = service.db.one("SELECT * FROM audit WHERE action='order_reconciled_manually'")
    detail = json.loads(log["details"])
    assert log["actor"] == "review-operator" and log["object_id"] == order["id"]
    assert detail["outcome"] == outcome and detail["previous_status"] == previous_status
    assert detail["provider_reference"] == "provider-receipt-review-123"
    assert detail["external_action_performed_by_operator"] is True
    assert service.db.one("SELECT state FROM incidents WHERE id=?", (incident_id,))["state"] == "resolved"
    assert len(service.db.all("SELECT * FROM events WHERE kind='account:payment_resolved'")) == 1
    assert client.post("/webhooks/xorpay", data=fields).status_code == 200
    assert service.db.one("SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user["id"],)) == before
    assert len(service.db.all("SELECT * FROM payments")) == 1
    with pytest.raises(Problem) as repeated:
        commerce.resolve_order(order["id"], outcome=outcome, reference="duplicate-receipt")
    assert repeated.value.status == 409
    fake_gateway(monkeypatch)
    fresh = create(client)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["id"] != order["id"]


def test_operator_cannot_mark_fulfilled_without_actual_entitlement(commerce_env, monkeypatch):
    _, service, commerce, login = commerce_env
    client, user = login("resolve-missing-rights@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    monkeypatch.setattr(Commerce, "api", lambda *_args, **_kwargs: {"status":"success"})
    commerce.reconcile(order["id"])
    with pytest.raises(Problem) as denied:
        commerce.resolve_order(order["id"], outcome="fulfilled", reference="receipt-1")
    assert denied.value.status == 409
    assert service.entitled(user["id"], "quantgt") is None
    assert service.db.one("SELECT status FROM purchase_orders WHERE id=?", (order["id"],))["status"] == "verifying"
    assert service.db.all("SELECT * FROM payments") == []
    assert service.db.all("SELECT * FROM audit WHERE action='order_reconciled_manually'") == []


@pytest.mark.parametrize("reference", ["", "  ", "receipt\nforged-line", "x"*201])
def test_operator_reconciliation_requires_valid_bounded_reference(commerce_env, monkeypatch, reference):
    _, service, commerce, login = commerce_env
    client, _ = login("resolve-reference@example.com")
    fake_gateway(monkeypatch)
    order = create(client).json()
    service.db.execute("UPDATE purchase_orders SET status='verifying' WHERE id=?", (order["id"],))
    with pytest.raises(ValueError):
        commerce.resolve_order(order["id"], outcome="refunded", reference=reference)
    assert service.db.one("SELECT status FROM purchase_orders WHERE id=?", (order["id"],))["status"] == "verifying"
    assert service.db.all("SELECT * FROM payments") == []
