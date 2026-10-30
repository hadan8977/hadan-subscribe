import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from hadan.security import action_token, decrypt
from hadan.service import Problem, Service


def test_otp_guess_limit_survives_process_restart(harness):
    _, service, _ = harness
    service.request_code("alice@example.com", "192.0.2.1")
    row = service.db.one("SELECT payload FROM jobs WHERE kind='login_mail'")
    code = decrypt(
        service.settings.secret, json.loads(row["payload"])["encrypted_code"]
    )
    wrong = "111111" if code != "111111" else "222222"
    for _ in range(5):
        with pytest.raises(Problem) as error:
            service.verify_code("alice@example.com", wrong, "192.0.2.1")
        assert error.value.status == 401
    restarted = Service(service.settings)
    with pytest.raises(Problem) as error:
        restarted.verify_code("alice@example.com", code, "192.0.2.2")
    assert error.value.status == 401
    assert not service.db.all("SELECT * FROM sessions")


def test_parallel_otp_consumption_creates_exactly_one_session(harness):
    _, service, _ = harness
    service.request_code("alice@example.com", "192.0.2.1")
    row = service.db.one("SELECT payload FROM jobs WHERE kind='login_mail'")
    code = decrypt(
        service.settings.secret, json.loads(row["payload"])["encrypted_code"]
    )

    def attempt(_):
        try:
            service.verify_code("alice@example.com", code, "192.0.2.1")
            return True
        except Problem:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    assert len(service.db.all("SELECT * FROM sessions")) == 1


def test_resend_invalidates_previous_otp_and_limits_persist(harness):
    _, service, _ = harness
    service.request_code("alice@example.com", "192.0.2.1")
    first = json.loads(
        service.db.one("SELECT payload FROM jobs WHERE kind='login_mail'")["payload"]
    )
    old = decrypt(service.settings.secret, first["encrypted_code"])
    service.request_code("alice@example.com", "192.0.2.1")
    current = service.db.one("SELECT * FROM otp WHERE email=?", ("alice@example.com",))
    from hadan.security import mac

    if current["digest"] != mac(service.settings.secret, "alice@example.com:" + old):
        with pytest.raises(Problem):
            service.verify_code("alice@example.com", old, "192.0.2.1")
    service.request_code("alice@example.com", "192.0.2.1")
    restarted = Service(service.settings)
    with pytest.raises(Problem) as error:
        restarted.request_code("alice@example.com", "192.0.2.2")
    assert error.value.status == 429


def test_csrf_and_cross_origin_mutations_are_rejected(harness):
    _, _, login = harness
    client, _ = login("alice@example.com")
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        client.post("/api/subscriptions", json={"product_id": "quantgt"}).status_code
        == 403
    )
    client.headers["X-CSRF-Token"] = "attacker"
    assert (
        client.post("/api/subscriptions", json={"product_id": "quantgt"}).status_code
        == 403
    )
    client.headers["X-CSRF-Token"] = csrf
    assert (
        client.post(
            "/api/subscriptions",
            json={"product_id": "quantgt"},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/me").json()["subscriptions"] == []
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_cross_user_content_files_events_ack_preferences_and_admin(harness, bind_agent):
    _, service, login = harness
    alice, a = login("alice@example.com")
    bob, b = login("bob@example.com")
    sub = alice.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    bob.post("/api/subscriptions", json={"product_id": "notes"})
    ab, aa = bind_agent(alice)
    bb, ba = bind_agent(bob)
    content = service.publish(
        "quantgt",
        "private",
        "Private signal",
        "Only paying readers",
        files=[{"name": "private.xlsx", "data": b"private-spreadsheet"}],
    )
    aid = service.content(a["id"], content["id"])["attachments"][0]["id"]
    for headers in ({}, ba):
        assert (
            bob.get("/api/content/" + content["id"], headers=headers).status_code == 404
        )
        assert bob.get("/api/files/" + aid, headers=headers).status_code == 404
        assert bob.get("/api/events", headers=headers).json()["events"] == []
    assert (
        bob.post("/api/events/" + content["event_id"] + "/ack", headers=ba).status_code
        == 404
    )
    assert (
        bob.patch(
            "/api/subscriptions/" + sub["id"], json={"email_enabled": False}
        ).status_code
        == 404
    )
    assert bob.post("/api/bindings/" + ab["id"] + "/activate").status_code == 404
    assert bob.delete("/api/bindings/" + ab["id"]).status_code == 404
    assert bob.get("/api/admin/status").status_code == 403
    result = bob.post(
        "/mcp",
        headers=ba,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_attachment", "arguments": {"attachment_id": aid}},
        },
    )
    assert result.json()["result"]["isError"] is True
    assert "private-spreadsheet" not in result.text
    assert alice.get("/api/files/" + aid, headers=aa).content == b"private-spreadsheet"
    assert alice.get("/api/files/" + aid).headers["cache-control"] == "no-store"
    service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    assert alice.get("/api/content/" + content["id"], headers=aa).status_code == 404
    assert alice.get("/api/files/" + aid).status_code == 404
    assert (
        alice.post(
            "/api/events/" + content["event_id"] + "/ack", headers=aa
        ).status_code
        == 404
    )


def test_pending_binding_and_replacement_revoke_old_access_preserve_pending(
    harness, bind_agent
):
    _, service, login = harness
    client, user = login("alice@example.com")
    client.post("/api/subscriptions", json={"product_id": "quantgt"})
    old, old_auth = bind_agent(client, name="old")
    event = service.publish("quantgt", "pending", "Pending", "Body")
    new = client.post("/api/bindings", json={"name": "new", "mode": "poll"}).json()
    new_auth = {"Authorization": "Bearer " + new["token"]}
    assert client.get("/api/events", headers=new_auth).status_code == 401
    assert client.post("/api/bindings/" + new["id"] + "/activate").status_code == 409
    assert client.get("/api/events", headers=old_auth).status_code == 200
    assert client.get("/api/agent/test", headers=new_auth).status_code == 200
    assert client.post("/api/agent/test/complete", headers=new_auth).status_code == 409
    assert client.get("/api/agent/test-file", headers=new_auth).status_code == 200
    assert client.post("/api/agent/test/complete", headers=new_auth).status_code == 200
    assert client.get("/api/events", headers=old_auth).status_code == 401
    assert (
        client.post(
            "/api/events/" + event["event_id"] + "/ack", headers=old_auth
        ).status_code
        == 401
    )
    assert [
        e["id"]
        for e in client.get("/api/events?pending=true", headers=new_auth).json()[
            "events"
        ]
    ] == [event["event_id"]]


def test_pairing_requires_device_secret_and_is_single_user(harness):
    app, service, login = harness
    alice, a = login("alice@example.com")
    bob, b = login("bob@example.com")
    with TestClient(app) as anonymous:
        pair = anonymous.post("/api/agent/pair", json={"name": "External agent"}).json()
        assert (
            anonymous.post(
                "/api/agent/pair/status",
                json={"pair_id": pair["pair_id"], "device_secret": "wrong"},
            ).status_code
            == 404
        )
        assert (
            alice.post(
                "/api/pair/approve", json={"code": pair["user_code"]}
            ).status_code
            == 200
        )
        assert (
            bob.post("/api/pair/approve", json={"code": pair["user_code"]}).status_code
            == 409
        )
        state = anonymous.post(
            "/api/agent/pair/status",
            json={"pair_id": pair["pair_id"], "device_secret": pair["device_secret"]},
        ).json()
        assert state["state"] == "approved"
        assert (
            service.db.one(
                "SELECT user_id FROM bindings WHERE id=?", (state["binding_id"],)
            )["user_id"]
            == a["id"]
        )


def test_narrow_unsubscribe_cannot_authenticate_and_is_idempotent(harness):
    app, service, login = harness
    client, user = login("alice@example.com")
    sub = client.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
    capability = action_token(service.settings.secret, sub["id"])
    with TestClient(app, raise_server_exceptions=False) as anonymous:
        assert (
            anonymous.get(
                "/api/events", headers={"Authorization": "Bearer " + capability}
            ).status_code
            == 401
        )
        for _ in range(2):
            assert (
                anonymous.post(
                    "/unsubscribe/" + capability, content="List-Unsubscribe=One-Click"
                ).status_code
                == 200
            )
    actual = service.db.one("SELECT * FROM subscriptions WHERE id=?", (sub["id"],))
    assert actual["email_enabled"] == 0
    assert actual["agent_enabled"] == 1
    client.post("/api/subscriptions", json={"product_id": "quantgt"})
    assert (
        service.db.one(
            "SELECT email_enabled FROM subscriptions WHERE id=?", (sub["id"],)
        )["email_enabled"]
        == 0
    )


def test_withdrawal_and_tampered_attachment_fail_closed(harness, bind_agent):
    _, service, login = harness
    client, user = login("alice@example.com")
    client.post("/api/subscriptions", json={"product_id": "quantgt"})
    _, auth = bind_agent(client)
    published = service.publish(
        "quantgt",
        "private",
        "Title",
        "Body",
        files=[{"name": "../../report.txt", "data": b"safe-data"}],
    )
    attachment = service.content(user["id"], published["id"])["attachments"][0]
    assert attachment["name"] == "report.txt"
    row, path = service.attachment(user["id"], attachment["id"])
    path.write_bytes(b"evil-data")
    assert client.get("/api/files/" + attachment["id"], headers=auth).status_code == 503
    path.write_bytes(b"safe-data")
    service.withdraw(published["id"], "Correction needed", "test")
    assert client.get("/api/files/" + attachment["id"], headers=auth).status_code == 410
    assert (
        client.get("/api/content/" + published["id"], headers=auth).status_code == 410
    )


def test_global_otp_budget_prevents_distributed_quota_exhaustion(harness, monkeypatch):
    _, service, _ = harness
    monkeypatch.setenv("OTP_HOURLY_BUDGET", "2")
    service.request_code("first@example.com", "192.0.2.1")
    service.request_code("second@example.com", "192.0.2.2")
    restarted = Service(service.settings)
    with pytest.raises(Problem) as error:
        restarted.request_code("third@example.com", "192.0.2.3")
    assert error.value.status == 429
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='login_mail'")) == 2
