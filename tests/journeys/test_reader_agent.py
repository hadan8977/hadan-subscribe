"""Product acceptance: identity, subscriptions, private files and agent migration."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from hadan.security import action_token


def publish_via_admin(
    journey, admin, *, key="report-001", body="Portfolio update", changes="", files=None
):
    files = files or [
        ("portfolio.csv", b"symbol,weight\nEXAMPLE,0.1\n"),
        ("notes.txt", "本次说明".encode()),
    ]
    response = journey.write(
        admin,
        "POST",
        "/api/admin/publish",
        json={
            "product_id": "quantgt",
            "source_key": key,
            "title": "Portfolio / Watchlist",
            "body": body,
            "changes": changes,
            "files": [
                {"name": name, "base64": base64.b64encode(data).decode()}
                for name, data in files
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), dict(files)


def test_complete_reader_agent_revision_migration_and_unsubscribe(journey):
    reader = journey.login()
    admin = journey.login("owner@example.test")
    sub = journey.subscribe(reader)
    first, expected = publish_via_admin(journey, admin)

    created = journey.write(
        reader, "POST", "/api/bindings", json={"name": "First agent", "mode": "poll"}
    ).json()
    agent = journey.client()
    agent.headers["Authorization"] = "Bearer " + created["token"]
    assert agent.get("/api/events").status_code == 401
    assert (
        journey.write(
            reader, "POST", f"/api/bindings/{created['id']}/activate"
        ).status_code
        == 409
    )
    assert agent.get("/api/agent/test").status_code == 200
    assert agent.post("/api/agent/test/complete").status_code == 409
    assert agent.get("/api/agent/test-file").status_code == 200
    assert agent.post("/api/agent/test/complete").json()["state"] == "active"

    events = agent.get("/api/events?pending=true").json()["events"]
    assert [event["id"] for event in events] == [first["event_id"]]
    content = agent.get(f"/api/content/{first['id']}").json()
    assert content["body"] == "Portfolio update"
    assert len(content["attachments"]) == 2
    for attachment in content["attachments"]:
        downloaded = agent.get("/api/files/" + attachment["id"])
        assert downloaded.status_code == 200
        assert downloaded.content == expected[attachment["name"]]
        assert hashlib.sha256(downloaded.content).hexdigest() == attachment["sha256"]
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert journey.client().get("/api/files/" + attachment["id"]).status_code == 401

    assert agent.post(f"/api/events/{first['event_id']}/ack").json() == {
        "state": "processed"
    }
    assert agent.post(f"/api/events/{first['event_id']}/ack").status_code == 200
    assert agent.get("/api/events?pending=true").json()["events"] == []

    revision, _ = publish_via_admin(
        journey, admin, body="Corrected allocation", changes="Corrected one allocation"
    )
    assert revision["id"] == first["id"] and revision["version"] == 2
    revised = agent.get(f"/api/content/{first['id']}").json()
    assert revised["body"] == "Corrected allocation"
    assert revised["changes"] == "Corrected one allocation"
    assert (
        agent.get(f"/api/content/{first['id']}?version=1").json()["body"]
        == "Portfolio update"
    )

    second, replacement = journey.connect(reader, "Replacement agent")
    assert second["id"] != created["id"]
    for method, path in (
        ("GET", "/api/events"),
        ("GET", f"/api/content/{first['id']}"),
        ("GET", "/api/files/" + content["attachments"][0]["id"]),
        ("POST", f"/api/events/{revision['event_id']}/ack"),
    ):
        assert agent.request(method, path).status_code == 401
    pending = replacement.get("/api/events?pending=true").json()["events"]
    assert [event["id"] for event in pending] == [revision["event_id"]]
    assert pending[0]["kind"] == "revised"
    profile = reader.get("/api/me").json()
    assert len([b for b in profile["bindings"] if b["state"] == "active"]) == 1
    assert created["token"] not in json.dumps(profile)
    assert second["token"] not in json.dumps(profile)

    assert (
        journey.write(
            reader,
            "PATCH",
            f"/api/subscriptions/{sub['id']}",
            json={"agent_enabled": False},
        ).status_code
        == 200
    )
    assert replacement.get("/api/events").json()["events"] == []
    assert replacement.get(f"/api/content/{first['id']}").status_code == 403
    assert (
        replacement.get("/api/files/" + content["attachments"][0]["id"]).status_code
        == 403
    )
    assert (
        replacement.post(f"/api/events/{revision['event_id']}/ack").status_code == 404
    )
    assert reader.get(f"/api/content/{first['id']}").status_code == 200
    assert (
        journey.write(
            reader,
            "PATCH",
            f"/api/subscriptions/{sub['id']}",
            json={"email_enabled": False, "agent_enabled": True},
        ).status_code
        == 200
    )
    assert (
        replacement.get("/api/events?pending=true").json()["events"][0]["id"]
        == revision["event_id"]
    )

    assert (
        journey.write(
            reader, "POST", f"/api/subscriptions/{sub['id']}/cancel"
        ).status_code
        == 200
    )
    final = reader.get("/api/me").json()["subscriptions"][0]
    assert (final["email_enabled"], final["agent_enabled"], final["auto_renew"]) == (
        0,
        0,
        0,
    )
    assert replacement.get("/api/events").json()["events"] == []
    assert reader.get(f"/api/content/{first['id']}").status_code == 404
    journey.drain()
    assert journey.messages(login=False) == []


def test_email_and_agent_channels_are_independent_and_provider_acceptance_is_not_ack(
    journey,
):
    reader = journey.login()
    sub = journey.subscribe(reader)
    _, agent = journey.connect(reader)
    assert (
        journey.write(
            reader,
            "PATCH",
            f"/api/subscriptions/{sub['id']}",
            json={"email_format": "full"},
        ).status_code
        == 200
    )
    item = journey.service.publish(
        "quantgt",
        "both",
        "A complete report",
        "Private report body",
        files=[
            {"name": "report.csv", "data": b"a,b\n1,2\n"},
            {"name": "readme.txt", "data": b"read me"},
        ],
    )
    journey.drain()
    messages = journey.messages(login=False)
    assert len(messages) == 1
    assert (
        messages[0]
        .get_body(preferencelist=("plain",))
        .get_content()
        .startswith("Private report body")
    )
    assert [part.get_filename() for part in messages[0].iter_attachments()] == [
        "report.csv",
        "readme.txt",
    ]
    assert messages[0]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert messages[0]["List-Unsubscribe"].startswith("<http://testserver/unsubscribe/")
    pending = agent.get("/api/events?pending=true").json()["events"]
    assert [event["id"] for event in pending] == [item["event_id"]]
    assert pending[0]["processing_state"] == "pending"
    assert (
        journey.service.db.one(
            "SELECT state FROM deliveries WHERE event_id=?", (item["event_id"],)
        )["state"]
        == "accepted"
    )


def test_unsubscribe_link_get_is_safe_and_post_stops_already_queued_email(journey):
    reader = journey.login()
    sub = journey.subscribe(reader)
    _, agent = journey.connect(reader)
    item = journey.service.publish(
        "quantgt", "unsubscribe", "Queued report", "Still readable by agent"
    )
    path = "/unsubscribe/" + action_token(journey.settings.secret, sub["id"])
    anonymous = journey.client()
    assert anonymous.get(path).status_code == 200
    assert reader.get("/api/me").json()["subscriptions"][0]["email_enabled"] == 1
    assert anonymous.post(path, content="List-Unsubscribe=One-Click").status_code == 200
    assert anonymous.post(path, content="List-Unsubscribe=One-Click").status_code == 200
    assert reader.get("/api/me").json()["subscriptions"][0]["agent_enabled"] == 1
    journey.drain()
    assert journey.messages(login=False) == []
    assert agent.get(f"/api/content/{item['id']}").status_code == 200
    # Repeated subscribe must never undo the recipient's opt-out.
    assert journey.subscribe(reader)["email_enabled"] == 0
    assert (
        anonymous.post(
            path + "tampered", content="List-Unsubscribe=One-Click"
        ).status_code
        == 400
    )


def test_private_content_and_preferences_cannot_cross_users(journey):
    alice = journey.login("alice@example.test")
    bob = journey.login("bob@example.test")
    alice_sub = journey.subscribe(alice)
    journey.subscribe(bob, "notes")
    _, bob_agent = journey.connect(bob)
    item = journey.service.publish(
        "quantgt",
        "alice-only",
        "Private",
        "Only entitled readers",
        files=[{"name": "private.txt", "data": b"private"}],
    )
    content = alice.get(f"/api/content/{item['id']}").json()
    for client in (bob, bob_agent):
        assert client.get(f"/api/content/{item['id']}").status_code == 404
        assert (
            client.get("/api/files/" + content["attachments"][0]["id"]).status_code
            == 404
        )
        assert client.get("/api/events").json()["events"] == []
    assert (
        journey.write(
            bob,
            "PATCH",
            f"/api/subscriptions/{alice_sub['id']}",
            json={"email_enabled": False},
        ).status_code
        == 404
    )
    assert (
        journey.write(
            bob, "POST", f"/api/subscriptions/{alice_sub['id']}/cancel"
        ).status_code
        == 404
    )
    assert bob.get("/api/admin/status").status_code == 403
    assert bob_agent.post(f"/api/events/{item['event_id']}/ack").status_code == 404


def test_device_pairing_authorization_and_connection_test(journey):
    reader = journey.login()
    journey.subscribe(reader)
    device = journey.client()
    start = device.post(
        "/api/agent/pair", json={"name": "Existing agent", "mode": "poll"}
    )
    assert start.status_code == 200
    pair = start.json()
    assert pair["device_secret"] not in pair["verification_uri"]
    poll = {"pair_id": pair["pair_id"], "device_secret": pair["device_secret"]}
    assert device.post("/api/agent/pair/status", json=poll).json()["state"] == "pending"
    assert (
        device.post(
            "/api/agent/pair/status", json={**poll, "device_secret": "wrong"}
        ).status_code
        == 404
    )
    assert (
        journey.write(
            reader, "POST", "/api/pair/approve", json={"code": pair["user_code"]}
        ).status_code
        == 200
    )
    assert (
        journey.write(
            reader, "POST", "/api/pair/approve", json={"code": pair["user_code"]}
        ).status_code
        == 409
    )
    assert (
        device.post("/api/agent/pair/status", json=poll).json()["state"] == "approved"
    )
    device.headers["Authorization"] = "Bearer " + pair["device_secret"]
    assert device.get("/api/events").status_code == 401
    assert device.get("/api/agent/test").status_code == 200
    assert device.get("/api/agent/test-file").status_code == 200
    assert device.post("/api/agent/test/complete").status_code == 200
    assert device.get("/api/events").status_code == 200


def test_withdrawal_hides_all_versions_and_files_and_supersedes_queued_report(journey):
    reader = journey.login()
    journey.subscribe(reader)
    _, agent = journey.connect(reader)
    item = journey.service.publish(
        "quantgt",
        "retract",
        "Original",
        "Sensitive",
        files=[{"name": "report.txt", "data": b"sensitive"}],
    )
    file_id = reader.get(f"/api/content/{item['id']}").json()["attachments"][0]["id"]
    withdrawn = journey.service.withdraw(
        item["id"], "Source corrected the publication", "test-admin"
    )
    for client in (reader, agent):
        assert client.get(f"/api/content/{item['id']}?version=1").status_code == 410
        assert client.get("/api/files/" + file_id).status_code == 410
    event = next(
        e
        for e in agent.get("/api/events").json()["events"]
        if e["id"] == withdrawn["event_id"]
    )
    assert event["kind"] == "withdrawn"
    assert event["summary"] == "Source corrected the publication"
    journey.service.db.execute("UPDATE jobs SET available_at=0 WHERE state='queued'")
    journey.drain()
    messages = journey.messages(login=False)
    assert (
        len(messages) == 1
        and messages[0]["Subject"] == "Source corrected the publication"
    )
    assert list(messages[0].iter_attachments()) == []
    assert (
        "Sensitive" not in messages[0].get_body(preferencelist=("plain",)).get_content()
    )


def test_cancellation_keeps_paid_access_until_expiry_but_stops_both_channels(journey):
    reader = journey.login()
    sub = journey.subscribe(reader)
    _, agent = journey.connect(reader)
    # A local grant represents confirmed paid entitlement, without a payment provider call.
    journey.service.db.execute(
        "UPDATE subscriptions SET expires_at=?,auto_renew=1 WHERE id=?",
        (time.time() + 3600, sub["id"]),
    )
    item = journey.service.publish(
        "quantgt", "paid-grant", "Paid content", "Paid access"
    )
    assert (
        journey.write(
            reader, "POST", f"/api/subscriptions/{sub['id']}/cancel"
        ).status_code
        == 200
    )
    assert reader.get(f"/api/content/{item['id']}").status_code == 200
    assert agent.get(f"/api/content/{item['id']}").status_code == 403
    journey.service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"])
    )
    assert reader.get(f"/api/content/{item['id']}").status_code == 404
    journey.service.db.execute("UPDATE jobs SET available_at=0 WHERE state='queued'")
    journey.drain()
    assert journey.messages(login=False) == []


def test_otp_is_single_use_and_mutations_require_csrf(journey):
    reader = journey.login()
    assert (
        reader.post("/api/subscriptions", json={"product_id": "quantgt"}).status_code
        == 403
    )
    message = journey.messages(recipient="reader@example.test", login=True)[0]
    import re

    code = re.search(
        r"验证码是 (\d{6})", message.get_body(preferencelist=("plain",)).get_content()
    ).group(1)
    assert (
        journey.client()
        .post("/api/auth/verify", json={"email": "reader@example.test", "code": code})
        .status_code
        == 401
    )
    assert journey.write(reader, "POST", "/api/auth/logout").status_code == 200
    assert reader.get("/api/me").status_code == 401


def test_file_integrity_failure_is_not_delivered_as_valid_content(journey):
    reader = journey.login()
    journey.subscribe(reader)
    item = journey.service.publish(
        "quantgt",
        "corrupt",
        "Report",
        "Body",
        files=[{"name": "a.txt", "data": b"original"}],
    )
    attachment = reader.get(f"/api/content/{item['id']}").json()["attachments"][0]
    path = journey.settings.data_dir / "files" / attachment["sha256"]
    path.write_bytes(b"modified")
    response = reader.get("/api/files/" + attachment["id"])
    assert response.status_code == 503
    assert "modified" not in response.text


def test_explicit_free_resubscribe_restores_access_without_resetting_consent(journey):
    reader = journey.login()
    sub = journey.subscribe(reader)
    item = journey.service.publish(
        "quantgt", "returning-reader", "Archive", "Previously subscribed"
    )
    assert (
        journey.write(
            reader, "POST", f"/api/subscriptions/{sub['id']}/cancel"
        ).status_code
        == 200
    )
    assert reader.get(f"/api/content/{item['id']}").status_code == 404
    resumed = journey.subscribe(reader)
    assert resumed["id"] == sub["id"], "Subscriptions must stay with the same user"
    assert resumed["status"] == "active", (
        "Explicit re-subscribe must restore a canceled free subscription"
    )
    assert resumed["email_enabled"] == 0 and resumed["agent_enabled"] == 0, (
        "Restoring access does not imply fresh channel consent"
    )
    assert reader.get(f"/api/content/{item['id']}").status_code == 200


def test_multiple_subscriptions_reuse_one_default_agent_with_separate_preferences(
    journey,
):
    reader = journey.login()
    first = journey.subscribe(reader, "quantgt")
    binding, agent = journey.connect(reader)
    second = journey.subscribe(reader, "notes")
    profile = reader.get("/api/me").json()
    assert len(profile["subscriptions"]) == 2
    assert [b["id"] for b in profile["bindings"] if b["state"] == "active"] == [
        binding["id"]
    ]
    first_item = journey.service.publish(
        "quantgt", "multi-first", "Portfolio", "First subscription"
    )
    second_item = journey.service.publish(
        "notes", "multi-second", "Service change", "Second subscription"
    )
    assert {e["id"] for e in agent.get("/api/events").json()["events"]} == {
        first_item["event_id"],
        second_item["event_id"],
    }
    assert (
        journey.write(
            reader,
            "PATCH",
            f"/api/subscriptions/{second['id']}",
            json={"agent_enabled": False},
        ).status_code
        == 200
    )
    assert [e["id"] for e in agent.get("/api/events").json()["events"]] == [
        first_item["event_id"]
    ]
    assert agent.get(f"/api/content/{first_item['id']}").status_code == 200
    assert agent.get(f"/api/content/{second_item['id']}").status_code == 403
    assert reader.get(f"/api/content/{second_item['id']}").status_code == 200
    assert reader.get("/api/me").json()["subscriptions"][0]["id"] == first["id"]


def test_unfinished_replacement_keeps_existing_agent_active(journey):
    reader = journey.login()
    journey.subscribe(reader)
    _, current = journey.connect(reader)
    created = journey.write(
        reader,
        "POST",
        "/api/bindings",
        json={"name": "Unfinished replacement", "mode": "poll"},
    )
    assert created.status_code == 200
    pending = journey.client()
    pending.headers["Authorization"] = "Bearer " + created.json()["token"]
    assert current.get("/api/events").status_code == 200
    assert pending.get("/api/events").status_code == 401
    assert (
        journey.write(
            reader, "POST", f"/api/bindings/{created.json()['id']}/activate"
        ).status_code
        == 409
    )
    assert current.get("/api/events").status_code == 200
