"""Regression tests for MCP entrypoints, authorization errors and delivery safety.

These are deliberately focused on the pre-launch defects: production GET
probing, business errors being mistaken for authentication errors, malformed
tool arguments, and withdrawal-before-delivery leakage.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from hadan.config import Settings
from hadan.web import create_app
from hadan.worker import Worker


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce():
    verifier = "v" * 64
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def register(client, redirect):
    response = client.post(
        "/oauth/register",
        json={
            "client_name": "ChatGPT",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def oauth_access(journey, owner, redirect, scope="quant.read quant.ack offline_access"):
    info = register(owner, redirect)
    verifier, challenge = pkce()
    response = owner.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": info["client_id"],
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "http://testserver",
            "scope": scope,
            "state": "state-regression",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request_id"][0]
    me = owner.get("/api/me")
    assert me.status_code == 200, me.text
    approved = owner.post(
        "/oauth/authorize/decision",
        data={
            "request_id": request_id,
            "decision": "approve",
            "csrf_token": me.json()["csrf"],
        },
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    tokens = owner.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": info["client_id"],
            "redirect_uri": redirect,
            "code": code,
            "code_verifier": verifier,
            "resource": "http://testserver",
        },
    )
    assert tokens.status_code == 200, tokens.text
    return tokens.json(), info


def mcp(client, access, method, params=None, identifier=1):
    return client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + access},
        json={
            "jsonrpc": "2.0",
            "id": identifier,
            "method": method,
            "params": params or {},
        },
    )


def tool_call(client, access, name, arguments, identifier=1):
    return mcp(
        client,
        access,
        "tools/call",
        {"name": name, "arguments": arguments},
        identifier,
    )


def subscribe_events(agent, token, url="https://chatgpt.com/mcp-events/callback"):
    secret = "whsec_" + base64.b64encode(b"x" * 32).decode()
    response = mcp(
        agent,
        token,
        "events/subscribe",
        {
            "name": "quant.report.published",
            "arguments": {"product_id": "quantgt"},
            "delivery": {"mode": "webhook", "url": url, "secret": secret},
            "ttlMs": 3600000,
        },
    )
    return response.json(), secret


def test_production_mcp_get_probe_does_not_crash(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "state",
        base_url="https://testserver",
        production=True,
        secret="p" * 64,
        admin_emails=("owner@example.test",),
        mail_backend="file",
        mail_from="Quant <notify@example.test>",
        support_contact="support@quant.example",
        smtp_required=False,
    )
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    for path in ("/mcp", "/mcp/"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["content-type"].startswith("text/event-stream")
    assert client.get("/mcp", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_business_errors_are_not_oauth_challenges(journey):
    owner = journey.login("business-error@example.test")
    journey.subscribe(owner)
    tokens, _ = oauth_access(
        journey, owner, "https://chatgpt.com/connector/oauth/business-error"
    )
    access = tokens["access_token"]

    # Agent delivery explicitly disabled is a business refusal, not an expired login.
    journey.service.db.execute(
        "UPDATE subscriptions SET agent_enabled=0 WHERE user_id=(SELECT id FROM users WHERE email=?)",
        ("business-error@example.test",),
    )
    response = tool_call(owner, access, "read_content", {"content_id": "missing"})
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert "mcp/www_authenticate" not in payload.get("_meta", {})
    assert "Agent" in payload["content"][0]["text"]
    assert payload["_meta"]["quant/error"]["status"] == 403
    assert payload["_meta"]["quant/error"]["code"] == "agent_delivery_disabled"

    # An expired subscription is also actionable without a new OAuth login.
    journey.service.db.execute(
        "UPDATE subscriptions SET agent_enabled=1,expires_at=? WHERE user_id=(SELECT id FROM users WHERE email=?)",
        (time.time() - 1, "business-error@example.test"),
    )
    response = tool_call(owner, access, "read_content", {"content_id": "missing"})
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert "mcp/www_authenticate" not in payload.get("_meta", {})
    assert payload["_meta"]["quant/error"]["code"] == "subscription_required"


def test_insufficient_scope_still_returns_authentication_challenge(journey):
    owner = journey.login("scope-error@example.test")
    journey.subscribe(owner)
    tokens, _ = oauth_access(
        journey,
        owner,
        "https://chatgpt.com/connector/oauth/scope-error",
        scope="quant.read",
    )
    response = tool_call(
        owner, tokens["access_token"], "ack_event", {"event_id": "missing"}
    )
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert payload["_meta"]["mcp/www_authenticate"]


def test_slow_checkout_does_not_block_other_mcp_reads(journey, monkeypatch):
    import asyncio

    import httpx

    reader = journey.login("slow-checkout@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)

    def slow_start(self, binding, payload, payer_ip):
        time.sleep(1.0)
        return {
            "id": "order",
            "status": "pending",
            "paid": False,
            "amount_minor": 1500,
            "currency": "CNY",
            "qr_image": "data:image/png;base64,AA==",
            "qr_available": True,
        }

    monkeypatch.setattr("hadan.agent_checkout.AgentCheckout.start", slow_start)

    async def scenario():
        transport = httpx.ASGITransport(app=journey.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            headers = {"Authorization": "Bearer " + binding["token"]}

            async def slow_call():
                return await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "prepare_checkout",
                            "arguments": {
                                "plan_id": "plan",
                                "method": "wechat",
                                "accept_terms": True,
                                "terms_version": "v",
                            },
                        },
                    },
                )

            async def health():
                await asyncio.sleep(0.15)
                started = time.monotonic()
                response = await client.get("/health/live")
                return response, time.monotonic() - started

            slow = asyncio.create_task(slow_call())
            health_task = asyncio.create_task(health())
            response, elapsed = await health_task
            assert response.status_code == 200
            assert elapsed < 0.6, f"health check was blocked for {elapsed:.2f}s"
            result = await slow
            assert result.status_code == 200, result.text

    asyncio.run(scenario())


def test_oauth_scope_and_refresh_contract(journey):
    owner = journey.login("scope-refresh@example.test")
    journey.subscribe(owner)
    read_only, info = oauth_access(
        journey,
        owner,
        "https://chatgpt.com/connector/oauth/read-only",
        scope="quant.read",
    )
    assert "offline_access" in read_only["scope"].split()
    assert read_only["refresh_token"]
    missing_refresh = owner.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": info["client_id"],
            "refresh_token": "",
            "resource": "http://testserver",
        },
    )
    assert missing_refresh.status_code == 400
    assert missing_refresh.json()["error"] in ("invalid_request", "invalid_grant")

    offline, offline_info = oauth_access(
        journey,
        owner,
        "https://chatgpt.com/connector/oauth/offline",
        scope="quant.read offline_access",
    )
    assert "offline_access" in offline["scope"].split()
    assert offline["refresh_token"]
    rotated = owner.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": offline_info["client_id"],
            "refresh_token": offline["refresh_token"],
            "resource": "http://testserver",
        },
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != offline["refresh_token"]
    assert rotated.json()["scope"] == offline["scope"]

    # An omitted scope uses the server default; explicit Quant scopes also
    # receive offline_access so MCP connectors can rotate refresh tokens.
    omitted, omitted_info = oauth_access(
        journey,
        owner,
        "https://chatgpt.com/connector/oauth/omitted",
        scope="",
    )
    assert "offline_access" in omitted["scope"].split()
    assert omitted["refresh_token"]
    assert omitted_info["client_id"]


def test_malformed_tool_arguments_are_structured_invalid_params(journey):
    reader = journey.login("arguments@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    client = journey.client()
    access = binding["token"]

    cases = [
        ("list_events", [], -32602),
        ("read_content", {}, -32602),
        ("read_content", {"content_id": 7}, -32602),
        ("read_content", {"content_id": "x", "extra": True}, -32602),
        ("ack_event", {"event_id": True}, -32602),
        ("not_a_tool", {}, -32601),
    ]
    for name, arguments, expected in cases:
        response = tool_call(client, access, name, arguments)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "result" not in body, body
        assert body["error"]["code"] == expected, (name, body)
        if expected == -32602:
            assert body["error"]["data"]["code"] == "invalid_params"


def test_withdrawal_before_mcp_delivery_does_not_leak_body(
    journey, monkeypatch
):
    reader = journey.login("withdraw-mcp@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    monkeypatch.setattr(
        "hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443)
    )
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)
    calls = []

    def record(url, body, headers):
        calls.append((url, body, headers))
        return 204, b""

    monkeypatch.setattr("hadan.mcp_events._post", record)
    subscribed, _ = subscribe_events(journey.client(), binding["token"])
    assert subscribed["result"]["id"].startswith("sub_")

    secret_body = "SENSITIVE-BODY-MUST-NOT-LEAK-" * 80
    published = journey.service.publish(
        "quantgt",
        "withdraw-before-delivery",
        "Sensitive report",
        secret_body,
        files=[{"name": "secret.txt", "data": b"secret attachment"}],
    )
    withdrawn = journey.service.withdraw(published["id"], "Correction needed", "test")
    journey.drain()

    delivered = [body for _, body, _ in calls if body.startswith(b'{"eventId"')]
    assert len(delivered) == 1, delivered
    payload = json.loads(delivered[0])
    assert payload["eventId"] == withdrawn["event_id"]
    assert payload["data"]["change_type"] == "withdrawn"
    assert payload["data"]["withdrawn"] is True
    assert "SENSITIVE-BODY" not in delivered[0].decode()
    assert "secret attachment" not in delivered[0].decode()
    old = journey.service.db.one(
        "SELECT state,last_error FROM mcp_event_deliveries WHERE event_id=?",
        (published["event_id"],),
    )
    assert old["state"] == "skipped"
    assert old["last_error"] == "content withdrawn"


def test_accepted_delivery_does_not_hide_event_after_unsubscribe_or_timeout(
    journey, monkeypatch
):
    reader = journey.login("recovery@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    monkeypatch.setattr(
        "hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443)
    )
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)
    calls = []

    def record(url, body, headers):
        calls.append(body)
        return 204, b""

    monkeypatch.setattr("hadan.mcp_events._post", record)
    subscribed, secret = subscribe_events(journey.client(), binding["token"])
    assert subscribed["result"]["id"].startswith("sub_")
    published = journey.service.publish(
        "quantgt", "recovery-report", "Recovery report", "Recovery body"
    )
    journey.drain()
    assert calls
    listed = tool_call(
        journey.client(),
        binding["token"],
        "list_events",
        {"after": 0},
    ).json()
    visible = {
        item["id"]
        for item in json.loads(listed["result"]["content"][0]["text"])["events"]
    }
    assert published["event_id"] not in visible
    https_recovery = journey.client().get(
        "/api/events?pending=true&include_webhook=true",
        headers={"Authorization": "Bearer " + binding["token"]},
    )
    assert https_recovery.status_code == 200
    assert published["event_id"] in {
        item["id"] for item in https_recovery.json()["events"]
    }

    # Explicit recovery is allowed even while push delivery is still active.
    recovered = tool_call(
        journey.client(),
        binding["token"],
        "list_events",
        {"after": 0, "include_webhook": True},
    ).json()
    recovered_ids = {
        item["id"]
        for item in json.loads(recovered["result"]["content"][0]["text"])["events"]
    }
    assert published["event_id"] in recovered_ids

    # An accepted delivery older than the bounded processing window is also
    # eligible for automatic polling recovery.
    journey.service.db.execute(
        "UPDATE mcp_event_deliveries SET updated_at=? WHERE event_id=?",
        (time.time() - 3600, published["event_id"]),
    )
    timed_out = tool_call(
        journey.client(), binding["token"], "list_events", {"after": 0}
    ).json()
    timed_out_ids = {
        item["id"]
        for item in json.loads(timed_out["result"]["content"][0]["text"])["events"]
    }
    assert published["event_id"] in timed_out_ids

    # After unsubscribing, the accepted-but-unprocessed event must reappear
    # automatically so a failed attachment/ack step remains recoverable.
    unsubscribed = mcp(
        journey.client(),
        binding["token"],
        "events/unsubscribe",
        {
            "name": "quant.report.published",
            "arguments": {"product_id": "quantgt"},
            "delivery": {
                "mode": "webhook",
                "url": "https://chatgpt.com/mcp-events/callback",
            },
        },
    )
    assert unsubscribed.json()["result"] == {}
    listed = tool_call(
        journey.client(), binding["token"], "list_events", {"after": 0}
    ).json()
    visible = {
        item["id"]
        for item in json.loads(listed["result"]["content"][0]["text"])["events"]
    }
    assert published["event_id"] in visible

    # A processed receipt is never offered again, even in recovery mode.
    ack = tool_call(
        journey.client(),
        binding["token"],
        "ack_event",
        {"event_id": published["event_id"]},
    )
    assert json.loads(ack.json()["result"]["content"][0]["text"]) == {
        "state": "processed"
    }
    listed = tool_call(
        journey.client(),
        binding["token"],
        "list_events",
        {"after": 0, "include_webhook": True},
    ).json()
    visible = {
        item["id"]
        for item in json.loads(listed["result"]["content"][0]["text"])["events"]
    }
    assert published["event_id"] not in visible


def test_accepted_delivery_is_a_terminal_noop(journey, monkeypatch):
    reader = journey.login("accepted-noop@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    monkeypatch.setattr(
        "hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443)
    )
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)
    calls = []

    def record(url, body, headers):
        calls.append(body)
        return 204, b""

    monkeypatch.setattr("hadan.mcp_events._post", record)
    subscribed, _ = subscribe_events(journey.client(), binding["token"])
    assert subscribed["result"]["id"].startswith("sub_")
    published = journey.service.publish(
        "quantgt", "accepted-noop-report", "Noop report", "Noop body"
    )
    journey.drain()
    assert len(calls) == 1
    subscription_id = subscribed["result"]["id"]
    # Simulate a worker crash after the accepted result was committed but
    # before the job completed and was reclaimed. The accepted row is terminal:
    # a reclaimed job must not post the same event again.
    from hadan.mcp_events import MCPEvents

    MCPEvents(journey.service).deliver(subscription_id, published["event_id"])
    assert len(calls) == 1
    assert (
        journey.service.db.one(
            "SELECT state FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?",
            (subscription_id, published["event_id"]),
        )["state"]
        == "accepted"
    )


def test_expired_oauth_can_read_only_own_account_notice(journey):
    owner = journey.login("expired-notice@example.test")
    journey.subscribe(owner)
    tokens, _ = oauth_access(
        journey, owner, "https://chatgpt.com/connector/oauth/expired-notice"
    )
    access = tokens["access_token"]
    user = journey.service.db.one(
        "SELECT * FROM users WHERE email='expired-notice@example.test'"
    )
    with journey.service.db.transaction() as db:
        notice = journey.service.account_notice(
            db,
            user["id"],
            "quantgt",
            "expired",
            "Quant · 订阅已到期",
            "续费后可继续阅读。",
            "expired-notice",
        )
    journey.service.db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE user_id=?",
        (time.time() - 1, user["id"]),
    )
    listed = tool_call(
        journey.client(), access, "list_events", {"after": 0}
    ).json()
    events = json.loads(listed["result"]["content"][0]["text"])["events"]
    assert notice in {item["id"] for item in events}
    ack = tool_call(
        journey.client(), access, "ack_event", {"event_id": notice}
    ).json()
    assert json.loads(ack["result"]["content"][0]["text"]) == {"state": "processed"}

    # Paid report access remains refused, but as a business refusal rather
    # than a request to re-authenticate.
    report = tool_call(
        journey.client(),
        access,
        "read_content",
        {"content_id": "not-a-report"},
    ).json()["result"]
    assert report["isError"] is True
    assert report["_meta"]["quant/error"]["code"] == "subscription_required"
    assert "mcp/www_authenticate" not in report.get("_meta", {})

    journey.service.db.execute(
        "UPDATE subscriptions SET agent_enabled=0 WHERE user_id=?", (user["id"],)
    )
    after = tool_call(
        journey.client(), access, "list_events", {"after": 0}
    ).json()
    assert json.loads(after["result"]["content"][0]["text"])["events"] == []


def test_recent_in_flight_mcp_claim_is_not_duplicated(journey, monkeypatch):
    reader = journey.login("in-flight@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    monkeypatch.setattr(
        "hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443)
    )
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        "hadan.mcp_events._post",
        lambda url, body, headers: (calls.append(body), (204, b""))[1],
    )
    subscribed, _ = subscribe_events(journey.client(), binding["token"])
    assert subscribed["result"]["id"].startswith("sub_")
    published = journey.service.publish(
        "quantgt", "in-flight-report", "In-flight report", "Body"
    )
    subscription_id = subscribed["result"]["id"]
    journey.service.db.execute(
        "UPDATE mcp_event_deliveries SET state='sending',updated_at=? "
        "WHERE subscription_id=? AND event_id=?",
        (time.time(), subscription_id, published["event_id"]),
    )
    from hadan.mcp_events import MCPEvents

    MCPEvents(journey.service).deliver(subscription_id, published["event_id"])
    assert calls == []
    # Only a stale claim is recoverable after the lease window.
    journey.service.db.execute(
        "UPDATE mcp_event_deliveries SET updated_at=? WHERE subscription_id=? AND event_id=?",
        (time.time() - 3600, subscription_id, published["event_id"]),
    )
    MCPEvents(journey.service).deliver(subscription_id, published["event_id"])
    assert len(calls) == 1
    assert (
        journey.service.db.one(
            "SELECT state FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?",
            (subscription_id, published["event_id"]),
        )["state"]
        == "accepted"
    )


def test_withdrawal_skips_an_already_retrying_mcp_delivery(journey, monkeypatch):
    reader = journey.login("withdraw-retry@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    monkeypatch.setattr(
        "hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443)
    )
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        "hadan.mcp_events._post",
        lambda url, body, headers: (calls.append(body), (204, b""))[1],
    )
    subscribed, _ = subscribe_events(journey.client(), binding["token"])
    assert subscribed["result"]["id"].startswith("sub_")
    published = journey.service.publish(
        "quantgt", "withdraw-retry", "Retrying report", "RETRY-SECRET-BODY"
    )
    journey.service.db.execute(
        "UPDATE mcp_event_deliveries SET state='retrying',updated_at=? WHERE event_id=?",
        (time.time() - 3600, published["event_id"]),
    )
    journey.service.withdraw(published["id"], "Retracted", "test")
    journey.drain()
    assert all(b"RETRY-SECRET-BODY" not in body for body in calls)
    old = journey.service.db.one(
        "SELECT state,last_error FROM mcp_event_deliveries WHERE event_id=?",
        (published["event_id"],),
    )
    assert old == {"state": "skipped", "last_error": "content withdrawn"}


def test_agent_checkout_rejects_a_stale_quote_before_any_order(journey, monkeypatch):
    import pytest

    from hadan.agent_checkout import AgentCheckout
    from hadan.commerce import Commerce
    from hadan.service import Problem
    from hadan.terms import TERMS_VERSION

    reader = journey.login("agent-quote@example.test")
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    user = journey.service.db.one(
        "SELECT id FROM users WHERE email='agent-quote@example.test'"
    )
    journey.settings.xiaodou_enabled = True
    journey.settings.xiaodou_methods = ("wechat",)
    journey.settings.payment_plans = [
        {
            "id": "fixed",
            "title": "Quant",
            "valid_until": "2027-05-01T00:00:00+08:00",
            "amount_minor": 1500,
            "currency": "CNY",
        }
    ]
    monkeypatch.setattr("hadan.agent_checkout.Xiaodou.ready", lambda self: True)
    plan = Commerce(journey.service).plans()[0]
    payload = {
        "plan_id": plan["id"],
        "method": "wechat",
        "accept_terms": True,
        "terms_version": TERMS_VERSION,
        "quote_id": "stale-quote",
    }
    with pytest.raises(Problem) as raised:
        AgentCheckout(journey.service).authorize(
            user["id"], binding["id"], payload, "8.8.8.8"
        )
    assert raised.value.status == 409
    assert raised.value.data == {"quote_changed": True}
    assert journey.service.db.all("SELECT * FROM purchase_orders") == []
    assert journey.service.db.all("SELECT * FROM checkout_permissions") == []


def test_discovery_instructions_match_chat_checkout_flow(journey):
    response = journey.client().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
    )
    instructions = response.json()["result"]["instructions"]
    assert "website" not in instructions.lower() or "checkout" not in instructions.lower()
    assert "/skill.md" in instructions and "/agent/instructions" in instructions
    assert "owner" in instructions.lower() and "QR" in instructions
