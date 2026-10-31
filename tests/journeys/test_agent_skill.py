"""Quant's public skill and device authorization, tested against disposable state.

The loopback test performs real HTTP, local OTP mail, private downloads and an
MCP receipt. Other tests use the actual ASGI routes without external services.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
from email import policy
from email.parser import BytesParser

import httpx
import pytest
import uvicorn

from hadan.config import Settings
from hadan.web import create_app
from hadan.worker import Worker


def start_pair(client, name="Existing reading assistant"):
    response = client.post("/api/agent/pair", json={"name": name, "mode": "poll"})
    assert response.status_code == 200, response.text
    return response.json()


def approve(journey, owner, pair):
    response = journey.write(owner, "POST", "/api/pair/approve", json={"code": pair["user_code"]})
    assert response.status_code == 200, response.text
    return response.json()


def credential_client(journey, pair):
    client = journey.client()
    client.headers["Authorization"] = "Bearer " + pair["device_secret"]
    return client


def activate(client):
    assert client.get("/api/agent/test").status_code == 200
    assert client.get("/api/agent/test-file").status_code == 200
    assert client.post("/api/agent/test/complete").status_code == 200


def test_public_skill_has_standard_entry_and_canonical_public_reference(journey):
    client = journey.client()
    pair = start_pair(client)
    response = client.get("/skill.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "no-store"
    frontmatter = response.text.split("---", 2)[1]
    assert re.search(r"(?m)^name: [a-z0-9-]+$", frontmatter)
    assert re.search(r"(?m)^description: .+", frontmatter)
    assert journey.settings.base_url + "/agent/instructions" in response.text
    assert "{{BASE_URL}}" not in response.text
    assert pair["device_secret"] not in response.text
    prompt = client.get("/agent/prompt")
    assert prompt.status_code == 200
    assert prompt.headers["content-type"].startswith("text/markdown")
    assert "Please help me subscribe to Quant" in prompt.text
    assert "{{BASE_URL}}" not in prompt.text
    assert len(prompt.text.strip().splitlines()) <= 5
    discovery = client.get("/llms.txt")
    assert discovery.status_code == 200
    assert journey.settings.base_url + "/skill.md" in discovery.text
    assert journey.settings.base_url + "/agent/prompt" in discovery.text
    home = client.get("/")
    assert 'rel="alternate" type="text/markdown" href="/skill.md"' in home.text
    assert 'href="/agent"' in home.text
    assert 'preview-assets/app-simulator.js' not in home.text
    assert client.get('/preview-assets/app-simulator.js').status_code == 404
    assert pair['device_secret'] not in discovery.text
    reference = client.get("/agent/instructions")
    assert reference.status_code == 200
    assert journey.settings.base_url + "/skill.md" in reference.text
    assert "/api/agent/status" in reference.text
    assert pair["device_secret"] not in reference.text


def test_status_requires_owner_approval_and_pending_token_cannot_read_content(journey):
    device = journey.client()
    pair = start_pair(device)
    credential = credential_client(journey, pair)
    assert device.get("/api/agent/status").status_code == 401
    assert credential.get("/api/agent/status").status_code == 401
    owner = journey.login()
    assert owner.get("/api/agent/status").status_code == 401  # A human session is not a device token.
    approve(journey, owner, pair)
    status = credential.get("/api/agent/status").json()
    assert status == {
        "binding": {"state": "pending", "mode": "poll"},
        "subscription": {"status": "none", "expires_at": None, "email_enabled": False, "agent_enabled": False},
        "entitled": False, "ready": False,
    }
    assert credential.get("/api/events").status_code == 401
    assert credential.post("/api/agent/test/complete").status_code == 409
    activate(credential)
    status = credential.get("/api/agent/status").json()
    assert status["binding"]["state"] == "active" and not status["ready"]
    assert status["subscription"]["status"] == "none"  # Tests did not buy or grant access.


def test_status_distinguishes_entitlement_channel_opt_out_expiry_and_revocation(journey):
    owner = journey.login()
    sub = journey.subscribe(owner)
    _, old_agent = journey.connect(owner)
    pair = start_pair(journey.client())
    binding = approve(journey, owner, pair)
    agent = credential_client(journey, pair)
    assert old_agent.get("/api/agent/status").json()["ready"] is True
    assert agent.get("/api/agent/status").json()["ready"] is False
    activate(agent)
    assert old_agent.get("/api/agent/status").status_code == 401
    for email_on, agent_on, expected_ready in [(False, True, True), (True, False, False), (False, False, False)]:
        changed = journey.write(owner, "PATCH", "/api/subscriptions/" + sub["id"], json={"email_enabled": email_on, "agent_enabled": agent_on})
        assert changed.status_code == 200
        response = agent.get("/api/agent/status")
        data = response.json()
        assert data["entitled"] is True and data["ready"] is expected_ready
        assert data["subscription"]["email_enabled"] is email_on
        assert data["subscription"]["agent_enabled"] is agent_on
    snapshot = journey.service.db.one("SELECT * FROM subscriptions WHERE id=?", (sub["id"],))
    for _ in range(3):
        assert agent.get("/api/agent/status").status_code == 200
    assert journey.service.db.one("SELECT * FROM subscriptions WHERE id=?", (sub["id"],)) == snapshot
    journey.service.db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (time.time() - 1, sub["id"]))
    data = agent.get("/api/agent/status").json()
    assert data["subscription"]["status"] == "expired" and not data["entitled"] and not data["ready"]
    journey.service.db.execute("UPDATE subscriptions SET status='ended' WHERE id=?", (sub["id"],))
    assert agent.get("/api/agent/status").json()["subscription"]["status"] == "ended"
    assert journey.write(owner, "DELETE", "/api/bindings/" + binding["binding_id"]).status_code == 200
    assert agent.get("/api/agent/status").status_code == 401


def test_status_ignores_foreign_ids_and_exposes_only_owners_quant_state(journey):
    alice = journey.login("alice@example.test")
    bob = journey.login("bob@example.test")
    journey.subscribe(alice, "notes")
    bob_sub = journey.subscribe(bob)
    _, alice_agent = journey.connect(alice)
    bob_id = bob.get("/api/me").json()["user"]["id"]
    response = alice_agent.get("/api/agent/status", params={"user_id": bob_id, "product_id": "notes", "subscription_id": bob_sub["id"]})
    data = response.json()
    assert data["subscription"]["status"] == "none" and not data["entitled"]
    assert set(data) == {"binding", "subscription", "entitled", "ready"}
    assert set(data["binding"]) == {"state", "mode"}
    assert set(data["subscription"]) == {"status", "expires_at", "email_enabled", "agent_enabled"}
    assert "alice@example.test" not in response.text and "bob@example.test" not in response.text
    assert bob_id not in response.text and bob_sub["id"] not in response.text
    for path, body in [("/api/subscriptions", {"product_id": "quantgt"}), ("/api/redeem", {"code": "invalid"}), ("/api/purchase", {"plan_id": "unused", "method": "alipay"})]:
        assert alice_agent.post(path, json=body).status_code == 401


def test_pair_details_require_human_session_and_return_only_unapproved_name_and_expiry(journey):
    device = journey.client()
    pair = start_pair(device, "My <desktop> reader")
    assert device.get("/api/pair/details", params={"code": pair["user_code"]}).status_code == 401
    device.headers["Authorization"] = "Bearer " + pair["device_secret"]
    assert device.get("/api/pair/details", params={"code": pair["user_code"]}).status_code == 401
    owner = journey.login()
    response = owner.get("/api/pair/details", params={"code": pair["user_code"].lower()})
    assert response.status_code == 200
    assert response.json()["name"] == "My <desktop> reader"  # The UI must render it as text.
    assert set(response.json()) == {"name", "expires_at"}
    assert time.time() < response.json()["expires_at"] <= time.time() + 600
    assert pair["device_secret"] not in response.text
    assert not journey.service.db.all("SELECT * FROM bindings")
    approve(journey, owner, pair)
    assert owner.get("/api/pair/details", params={"code": pair["user_code"]}).status_code == 409
    other = start_pair(journey.client())
    journey.service.db.execute("UPDATE pairings SET expires_at=? WHERE id=?", (time.time() - 1, other["pair_id"]))
    assert owner.get("/api/pair/details", params={"code": other["user_code"]}).status_code == 409


@pytest.mark.parametrize("code", ["", "A" * 7, "A" * 9, "GGGGGGGG", " AB12CD34", "AB12CD34\n", "ＡＢ１２ＣＤ３４"])
def test_pair_details_accept_only_eight_ascii_hex_characters(journey, code):
    owner = journey.login()
    response = owner.get("/api/pair/details", params={"code": code})
    assert response.status_code == 409
    owner_id = owner.get("/api/me").json()["user"]["id"]
    assert journey.service.db.one("SELECT count FROM rate_limits WHERE key=?", ("pair-details:" + owner_id,))["count"] == 1


def test_failed_details_budget_persists_and_is_separate_from_approval(journey):
    owner = journey.login()
    pair = start_pair(journey.client())
    for _ in range(30):
        assert owner.get("/api/pair/details", params={"code": "invalid"}).status_code == 409
    assert owner.get("/api/pair/details", params={"code": pair["user_code"]}).status_code == 429
    assert journey.write(owner, "POST", "/api/pair/approve", json={"code": pair["user_code"]}).status_code == 200


def test_failed_approval_budget_commits_before_invalid_lookup_and_resets_after_window(journey):
    owner = journey.login()
    pair = start_pair(journey.client())
    for index in range(10):
        code = "invalid" if index % 2 else "00000000"
        if code == pair["user_code"]:
            code = "FFFFFFFF"
        assert journey.write(owner, "POST", "/api/pair/approve", json={"code": code}).status_code == 409
    assert journey.write(owner, "POST", "/api/pair/approve", json={"code": pair["user_code"]}).status_code == 429
    owner_id = owner.get("/api/me").json()["user"]["id"]
    key = "pair-approve:" + owner_id
    assert journey.service.db.one("SELECT count FROM rate_limits WHERE key=?", (key,))["count"] == 10
    assert not journey.service.db.all("SELECT * FROM bindings")
    # Preview has its own budget, and the pairing has not been consumed.
    assert owner.get("/api/pair/details", params={"code": pair["user_code"]}).status_code == 200
    journey.service.db.execute("UPDATE rate_limits SET start=? WHERE key=?", (time.time() - 601, key))
    assert journey.write(owner, "POST", "/api/pair/approve", json={"code": pair["user_code"].lower()}).status_code == 200


def test_loopback_http_skill_pair_local_otp_downloads_and_mcp_receipt(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    cfg = Settings(data_dir=tmp_path / "state", base_url=origin, production=False,
                   secret="local-agent-skill-test-" * 4, allowed_emails=(), admin_emails=(),
                   mail_backend="file", mail_from="Quant <notify@example.test>", smtp_host="", smtp_user="", smtp_password="",
                   stripe_key="", stripe_webhook_secret="", xorpay_enabled=False, xorpay_aid="", xorpay_secret="", xianyu_url="", payment_plans=[], deepseek_key="", ops_ai=False)
    app = create_app(cfg)
    service = app.state.service
    service.seed()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        with httpx.Client(base_url=origin, timeout=20, trust_env=False) as device, httpx.Client(base_url=origin, timeout=20, trust_env=False) as owner:
            skill = device.get("/skill.md")
            assert skill.status_code == 200 and origin + "/agent/instructions" in skill.text
            pair = start_pair(device)
            device.headers["Authorization"] = "Bearer " + pair["device_secret"]
            assert device.get("/api/agent/status").status_code == 401
            assert owner.post("/api/auth/request", json={"email": "local-owner@example.test"}).status_code == 200
            assert Worker(service).run_once()
            mail_files = list((cfg.data_dir / "mail").glob("*.eml"))
            assert len(mail_files) == 1
            message = BytesParser(policy=policy.default).parsebytes(mail_files[0].read_bytes())
            code = re.search(r"验证码是 (\d{6})", message.get_body(preferencelist=("plain",)).get_content()).group(1)
            assert owner.post("/api/auth/verify", json={"email": "local-owner@example.test", "code": code}).status_code == 200
            me = owner.get("/api/me").json()
            owner.headers["X-CSRF-Token"] = me["csrf"]
            sub = owner.post("/api/subscriptions", json={"product_id": "quantgt"}).json()
            assert owner.patch("/api/subscriptions/" + sub["id"], json={"email_enabled": False}).status_code == 200
            details = owner.get("/api/pair/details", params={"code": pair["user_code"]})
            assert details.json()["name"] == "Existing reading assistant"
            assert owner.post("/api/pair/approve", json={"code": pair["user_code"]}).status_code == 200
            assert device.post("/api/agent/pair/status", json={"pair_id": pair["pair_id"], "device_secret": pair["device_secret"]}).json()["state"] == "approved"
            before = device.get("/api/agent/status").json()
            assert before["entitled"] and not before["ready"]
            test_body = device.get("/api/agent/test").json()
            test_file = device.get("/api/agent/test-file")
            assert test_body["body"] and test_file.status_code == 200
            inbox = tmp_path / "agent-inbox"
            inbox.mkdir()
            (inbox / "test-body.json").write_text(json.dumps(test_body))
            (inbox / "test-file.txt").write_bytes(test_file.content)
            assert device.post("/api/agent/test/complete").json()["state"] == "active"
            ready = device.get("/api/agent/status").json()
            assert ready["ready"] and ready["subscription"]["email_enabled"] is False
            assert len(service.db.all("SELECT * FROM users")) == 1
            assert len(service.db.all("SELECT * FROM subscriptions")) == 1
            for _ in range(2):
                assert device.get("/api/agent/status").json()["ready"]
            assert len(service.db.all("SELECT * FROM bindings")) == 1
            published = service.publish("quantgt", "agent-skill-http", "Synthetic Quant report", "Fixture report body", files=[{"name": "report.txt", "data": b"private fixture"}, {"name": "table.csv", "data": b"symbol,value\nFIXTURE,1\n"}])
            event = next(row for row in device.get("/api/events?pending=true").json()["events"] if row["id"] == published["event_id"])
            content = device.get(f'/api/content/{event["content_id"]}', params={"version": event["version"]}).json()
            assert content["body"] == "Fixture report body" and len(content["attachments"]) == 2
            for file in content["attachments"]:
                download = device.get("/api/files/" + file["id"])
                assert download.status_code == 200 and len(download.content) == file["size"]
                assert hashlib.sha256(download.content).hexdigest() == file["sha256"]
                (inbox / file["name"]).write_bytes(download.content)
            assert published["event_id"] in {row["id"] for row in device.get("/api/events?pending=true").json()["events"]}
            initialized = device.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert initialized.status_code == 200 and "result" in initialized.json()
            receipt = device.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ack_event", "arguments": {"event_id": published["event_id"]}}}).json()
            assert json.loads(receipt["result"]["content"][0]["text"]) == {"state": "processed"}
            assert published["event_id"] not in {row["id"] for row in device.get("/api/events?pending=true").json()["events"]}
            assert owner.get("/api/me").json()["subscriptions"][0]["email_enabled"] == 0
            assert device.post("/api/purchase", json={"plan_id": "none", "method": "alipay"}).status_code == 401
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Owned loopback server did not stop"


def test_agent_guidance_requires_monthly_and_weekly_pick_lists(journey):
    client = journey.client()
    skill = client.get("/skill.md").text
    protocol = client.get("/agent/instructions").text
    assert "Monthly Picks" in skill and "Weekly Picks" in skill
    assert "Monthly Picks" in protocol and "Weekly Picks" in protocol
    discovery = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
    ).json()["result"]["instructions"]
    assert "Monthly Picks" in discovery and "Weekly Picks" in discovery
    tools = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    ).json()["result"]["tools"]
    read_content = next(tool for tool in tools if tool["name"] == "read_content")
    assert "Monthly Picks" in read_content["description"]
    assert "Weekly Picks" in read_content["description"]
