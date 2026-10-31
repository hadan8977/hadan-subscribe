"""OAuth 2.1 + MCP Events acceptance tests.

External webhook delivery is replaced with a local recorder; the OAuth and
JSON-RPC shapes are exercised through the real ASGI app and worker.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from hadan.mcp_events import CallbackError, _secret_key, _signature, validate_callback_url, verify_callback
from hadan.worker import Worker


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce():
    verifier = "v" * 64
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def register(client, redirect="https://chatgpt.com/connector/oauth/test-callback"):
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


def start_authorize(client, client_id, redirect, challenge, state="state-123"):
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "http://testserver",
            "scope": "quant.read quant.ack offline_access",
            "state": state,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    return parse_qs(urlsplit(location).query)["request_id"][0]


def approve(client, request_id):
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    response = client.post(
        "/oauth/authorize/decision",
        data={
            "request_id": request_id,
            "decision": "approve",
            "csrf_token": me.json()["csrf"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def token(client, client_id, redirect, code, verifier):
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "code": code,
            "code_verifier": verifier,
            "resource": "http://testserver",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def mcp(client, access, method, params=None, identifier=1):
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + access},
        json={"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def fresh_otp(journey, email):
    client = journey.client()
    assert client.post("/api/auth/request", json={"email": email}).status_code == 200
    assert Worker(journey.service).run_once()
    messages = journey.messages(recipient=email.lower().strip(), login=True)
    assert messages
    body = messages[-1].get_body(preferencelist=("plain",)).get_content()
    return client, re.search(r"验证码是 (\d{6})", body).group(1)


def test_oauth_metadata_and_anonymous_mcp_discovery(journey):
    client = journey.client()
    protected = client.get("/.well-known/oauth-protected-resource")
    assert protected.status_code == 200
    assert protected.json()["resource"] == "http://testserver/mcp"
    assert protected.json()["authorization_servers"] == ["http://testserver"]
    path_aware = client.get("/.well-known/oauth-protected-resource/mcp")
    assert path_aware.json()["resource"] == "http://testserver/mcp"
    metadata = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"User-Agent": "Python/3.14 aiohttp/3.14.4"},
    )
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["issuer"] == "http://testserver"
    assert "S256" in body["code_challenge_methods_supported"]
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["userinfo_endpoint"].endswith("/oauth/userinfo")
    assert body["jwks_uri"].endswith("/.well-known/jwks.json")
    assert body["subject_types_supported"] == ["public"]
    assert client.get("/oauth/userinfo").json() == {"sub": "anonymous"}
    assert client.get("/.well-known/jwks.json").json() == {"keys": []}
    # ChatGPT validates anonymous discovery before starting OAuth.
    discover = client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"},
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
    )
    assert discover.status_code == 200
    assert discover.json()["result"]["resultType"] == "complete"
    assert "events" in discover.json()["result"]["capabilities"]
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools.status_code == 200
    by_name = {t["name"]: t for t in tools.json()["result"]["tools"]}
    assert {"read_content", "prepare_checkout", "get_checkout_status", "get_checkout_qr"} <= set(by_name)
    assert by_name["prepare_checkout"]["securitySchemes"][0]["scopes"] == ["quant.read"]
    # ChatGPT's validator sends valid JSON-RPC as application/octet-stream.
    octet = client.post(
        "/mcp",
        content='{"jsonrpc":"2.0","id":7,"method":"server/discover","params":{}}',
        headers={"Content-Type": "application/octet-stream"},
    )
    assert octet.status_code == 200
    assert octet.json()["result"]["resultType"] == "complete"
    empty = client.post(
        "/mcp",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert empty.status_code == 204
    assert empty.content == b""
    assert client.get("/.well-known/openid-configuration").status_code == 404
    probe = client.get("/mcp")
    assert probe.status_code == 200
    assert probe.headers["content-type"] == "text/event-stream"
    assert client.get("/mcp/").status_code == 200
    assert client.post("/mcp/", json={"jsonrpc": "2.0", "id": 6, "method": "server/discover"}).status_code == 200
    unauthorized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_content", "arguments": {}}},
    )
    assert unauthorized.status_code == 200
    assert unauthorized.json()["result"]["isError"] is True
    challenge = unauthorized.json()["result"]["_meta"]["mcp/www_authenticate"][0]
    assert "resource_metadata=" in challenge and "error_description=" in challenge
    bad_token = client.post(
        "/mcp",
        headers={"Authorization": "Bearer invalid"},
        json={"jsonrpc": "2.0", "id": 4, "method": "server/discover"},
    )
    assert bad_token.status_code == 401
    bad_tool = client.post(
        "/mcp",
        headers={"Authorization": "Bearer invalid"},
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "read_content", "arguments": {}}},
    )
    assert bad_tool.status_code == 401


def test_registration_rejects_non_chatgpt_redirect(journey):
    response = journey.client().post(
        "/oauth/register",
        json={"client_name": "bad", "redirect_uris": ["https://evil.example/callback"]},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_registration_accepts_grok_and_cursor_callbacks(journey):
    client = journey.client()
    cursor = client.post(
        "/oauth/register",
        json={
            "client_name": "Cursor",
            "redirect_uris": [
                "https://www.cursor.com/agents/mcp/oauth/callback",
                "https://www.cursor.com/bot/mcp/oauth/callback",
                "cursor://anysphere.cursor-mcp/oauth/callback",
                "http://localhost:8787/callback",
            ],
            "token_endpoint_auth_method": "none",
        },
    )
    assert cursor.status_code == 201, cursor.text
    grok = client.post(
        "/oauth/register",
        json={
            "client_name": "Grok",
            "redirect_uris": ["https://grok.com/connectors-oauth-exchange-code/"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert grok.status_code == 201, grok.text


def test_oauth_code_pkce_refresh_and_revoke(journey, monkeypatch):
    owner = journey.login("oauth-owner@example.test")
    journey.subscribe(owner)
    client = owner
    redirect = "https://chatgpt.com/connector/oauth/test-callback"
    client_info = register(client, redirect)
    assert "client_secret" not in client_info
    assert "quant.read" in client_info["scope"]
    verifier, challenge = pkce()
    request_id = start_authorize(client, client_info["client_id"], redirect, challenge)
    # The consent page is only available to the signed-in owner.
    page = client.get("/oauth/authorize", params={"request_id": request_id, "manual": "1"})
    assert page.status_code == 200
    assert "oauth-decision-form" in page.text
    location = approve(client, request_id)
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["state-123"]
    assert query["iss"] == ["http://testserver"]
    code = query["code"][0]

    # A code is single-use and PKCE is mandatory.
    bad = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_info["client_id"],
            "redirect_uri": redirect,
            "code": code,
            "code_verifier": "w" * 64,
            "resource": "http://testserver",
        },
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    tokens = token(client, client_info["client_id"], redirect, code, verifier)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]
    reused = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_info["client_id"],
            "redirect_uri": redirect,
            "code": code,
            "code_verifier": verifier,
            "resource": "http://testserver",
        },
    )
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    # Raw tokens are never stored; only digests are.
    raw = tokens["access_token"]
    row = journey.service.db.one("SELECT digest FROM oauth_tokens WHERE kind='access'")
    assert row["digest"] != raw
    assert len(row["digest"]) == 64

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_info["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": "http://testserver",
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new_tokens = refreshed.json()
    assert new_tokens["access_token"] != tokens["access_token"]
    basic = base64.b64encode((client_info["client_id"] + ":").encode()).decode()
    basic_refresh = client.post(
        "/oauth/token",
        headers={"Authorization": "Basic " + basic},
        data={
            "grant_type": "refresh_token",
            "refresh_token": new_tokens["refresh_token"],
            "resource": "http://testserver",
        },
    )
    assert basic_refresh.status_code == 200, basic_refresh.text
    new_tokens = basic_refresh.json()

    discover = mcp(client, new_tokens["access_token"], "server/discover")
    assert discover["result"]["resultType"] == "complete"
    assert "2026-07-28" in discover["result"]["supportedVersions"]
    assert "events" in discover["result"]["capabilities"]
    events = mcp(client, new_tokens["access_token"], "events/list")
    assert events["result"]["events"][0]["name"] == "quant.report.published"
    # A rotated refresh token cannot be reused; the current access token
    # remains valid until its own expiry or revocation.
    old_refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_info["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": "http://testserver",
        },
    )
    assert old_refresh.status_code == 400
    assert client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + new_tokens["access_token"]},
        json={"jsonrpc": "2.0", "id": 8, "method": "server/discover"},
    ).status_code == 200
    revoked = client.post("/oauth/revoke", data={"token": new_tokens["access_token"]})
    assert revoked.status_code == 200
    after = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + new_tokens["access_token"]},
        json={"jsonrpc": "2.0", "id": 9, "method": "server/discover"},
    )
    assert after.status_code == 401
    assert "www-authenticate" in after.headers


def test_mcp_events_subscribe_and_deliver(journey, monkeypatch):
    owner = journey.login("events-owner@example.test")
    journey.subscribe(owner)
    redirect = "https://chatgpt.com/connector/oauth/events-callback"
    client_info = register(owner, redirect)
    verifier, challenge = pkce()
    request_id = start_authorize(owner, client_info["client_id"], redirect, challenge)
    location = approve(owner, request_id)
    code = parse_qs(urlsplit(location).query)["code"][0]
    tokens = token(owner, client_info["client_id"], redirect, code, verifier)
    access = tokens["access_token"]

    calls = []
    monkeypatch.setattr("hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443))
    monkeypatch.setattr("hadan.mcp_events.verify_callback", lambda *args: None)

    def record(url, body, headers):
        calls.append((url, body, headers))
        if body.startswith(b'{"type":"verification"'):
            return 204, b""
        return 204, b""

    monkeypatch.setattr("hadan.mcp_events._post", record)
    secret = "whsec_" + base64.b64encode(b"x" * 32).decode()
    subscribed = mcp(
        owner,
        access,
        "events/subscribe",
        {
            "name": "quant.report.published",
            "arguments": {"product_id": "quantgt"},
            "delivery": {
                "mode": "webhook",
                "url": "https://chatgpt.com/mcp-events/callback",
                "secret": secret,
            },
            "ttlMs": 3600000,
        },
    )
    assert subscribed["result"]["id"].startswith("sub_")
    assert subscribed["result"]["cursor"] is None
    assert subscribed["result"]["refreshBefore"]

    published = journey.service.publish(
        "quantgt",
        "events-report",
        "Event delivery report",
        "The report body for MCP Events.",
        files=[{"name": "report.txt", "data": b"event attachment"}],
    )
    assert published["event_id"]
    journey.drain()
    delivered = [
        item for item in calls if not item[1].startswith(b'{"type":"verification"')
    ]
    assert delivered, "MCP event delivery was not attempted"
    _, body, headers = delivered[-1]
    payload = json.loads(body)
    assert payload["name"] == "quant.report.published"
    assert payload["data"]["content_id"] == published["id"]
    assert payload["data"]["version"] == 1
    assert headers["webhook-id"] == payload["eventId"]
    assert headers["webhook-signature"].startswith("v1,")
    assert headers["X-MCP-Subscription-Id"] == subscribed["result"]["id"]
    row = journey.service.db.one("SELECT state FROM mcp_event_deliveries")
    assert row["state"] == "accepted"
    read = mcp(
        owner,
        access,
        "tools/call",
        {
            "name": "read_content",
            "arguments": {"content_id": published["id"]},
        },
    )
    content = json.loads(read["result"]["content"][0]["text"])
    assert content["body"] == "The report body for MCP Events."
    ack = mcp(
        owner,
        access,
        "tools/call",
        {"name": "ack_event", "arguments": {"event_id": published["event_id"]}},
    )
    assert json.loads(ack["result"]["content"][0]["text"]) == {"state": "processed"}
    unsubscribed = mcp(
        owner,
        access,
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
    assert unsubscribed["result"] == {}
    row = journey.service.db.one("SELECT active FROM mcp_event_subscriptions")
    assert row["active"] == 0


def test_callback_url_rejects_private_and_plain_http():
    for url in ("http://example.com/callback", "https://127.0.0.1/callback", "https://localhost/callback"):
        with pytest.raises(CallbackError):
            validate_callback_url(url)


def test_callback_challenge_is_signed_with_standard_webhooks(monkeypatch):
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    key = _secret_key(secret)
    captured = {}

    def fake_post(url, body, headers):
        captured.update({"url": url, "body": body, "headers": headers})
        return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()

    monkeypatch.setattr("hadan.mcp_events._post", fake_post)
    verify_callback("https://example.com/callback", secret, "sub_example")
    headers = captured["headers"]
    expected = _signature(key, headers["webhook-id"], headers["webhook-timestamp"], captured["body"])
    assert headers["webhook-signature"] == expected
    assert headers["X-MCP-Subscription-Id"] == "sub_example"
    assert headers["webhook-id"].startswith("msg_")


def test_callback_challenge_mismatch_is_rejected(monkeypatch):
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()

    monkeypatch.setattr(
        "hadan.mcp_events._post",
        lambda url, body, headers: (200, b'{"challenge":"wrong"}'),
    )
    with pytest.raises(CallbackError) as exc:
        verify_callback("https://example.com/callback", secret, "sub_example")
    assert exc.value.reason == "challenge_failed"


def test_oauth_decision_requires_csrf(journey):
    owner = journey.login("csrf-owner@example.test")
    redirect = "https://chatgpt.com/connector/oauth/csrf-callback"
    info = register(owner, redirect)
    verifier, challenge = pkce()
    request_id = start_authorize(owner, info["client_id"], redirect, challenge)
    response = owner.post(
        "/oauth/authorize/decision",
        data={"request_id": request_id, "decision": "approve", "csrf_token": "bad"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_authorize_rejects_unregistered_redirect(journey):
    owner = journey.login("redirect-owner@example.test")
    info = register(owner)
    verifier, challenge = pkce()
    response = owner.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": info["client_id"],
            "redirect_uri": "https://evil.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def oauth_access(owner, redirect):
    info = register(owner, redirect)
    verifier, challenge = pkce()
    request_id = start_authorize(owner, info["client_id"], redirect, challenge)
    location = approve(owner, request_id)
    code = parse_qs(urlsplit(location).query)["code"][0]
    tokens = token(owner, info["client_id"], redirect, code, verifier)
    return tokens["access_token"]


def test_events_subscribe_invalid_secret_is_invalid_params(journey):
    owner = journey.login("bad-secret@example.test")
    journey.subscribe(owner)
    access = oauth_access(owner, "https://chatgpt.com/connector/oauth/bad-secret")
    response = mcp(
        owner,
        access,
        "events/subscribe",
        {
            "name": "quant.report.published",
            "arguments": {},
            "delivery": {
                "mode": "webhook",
                "url": "https://chatgpt.com/mcp-events/callback",
                "secret": "whsec_short",
            },
        },
    )
    assert response["error"]["code"] == -32602


def test_events_subscribe_callback_failure_is_32015(journey, monkeypatch):
    owner = journey.login("callback-fail@example.test")
    journey.subscribe(owner)
    access = oauth_access(owner, "https://chatgpt.com/connector/oauth/callback-fail")
    monkeypatch.setattr("hadan.mcp_events.validate_callback_url", lambda url: ("chatgpt.com", 443))
    def fail(*args):
        raise CallbackError("challenge_failed")
    monkeypatch.setattr("hadan.mcp_events.verify_callback", fail)
    response = mcp(
        owner,
        access,
        "events/subscribe",
        {
            "name": "quant.report.published",
            "arguments": {},
            "delivery": {
                "mode": "webhook",
                "url": "https://chatgpt.com/mcp-events/callback",
                "secret": "whsec_" + base64.b64encode(b"x" * 32).decode(),
            },
        },
    )
    assert response["error"]["code"] == -32015
    assert response["error"]["data"]["reason"] == "challenge_failed"


def test_mcp_accepts_chatgpt_origin_and_callback_query(journey, monkeypatch):
    client = journey.client()
    response = client.post(
        "/mcp",
        headers={"Origin": "https://chatgpt.com"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_content", "arguments": {}}},
    )
    assert response.status_code == 200
    assert response.json()["result"]["_meta"]["mcp/www_authenticate"]
    monkeypatch.setattr("hadan.mcp_events._public_host", lambda host, port: None)
    assert validate_callback_url("https://chatgpt.com/callback?subscription=abc") == ("chatgpt.com", 443)


def test_signed_in_first_oauth_get_completes_without_consent_click(journey):
    client = journey.login("auto-signed-in@example.test")
    redirect = "https://chatgpt.com/connector/oauth/auto-signed-in"
    info = register(client, redirect)
    verifier, challenge = pkce()
    request_id = start_authorize(client, info["client_id"], redirect, challenge)
    response = client.get(
        "/oauth/authorize",
        params={"request_id": request_id},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    tokens = token(client, info["client_id"], redirect, query["code"][0], verifier)
    assert tokens["access_token"]


def test_email_verification_completes_first_oauth_connection_without_consent_click(journey):
    client, code = fresh_otp(journey, "auto-oauth@example.test")
    redirect = "https://chatgpt.com/connector/oauth/auto-first"
    info = register(client, redirect)
    verifier, challenge = pkce()
    request_id = start_authorize(client, info["client_id"], redirect, challenge)
    verified = client.post(
        "/api/auth/verify",
        json={"email": "auto-oauth@example.test", "code": code, "oauth_request_id": request_id},
    )
    assert verified.status_code == 200, verified.text
    target = verified.json()["oauth_redirect"]
    location = parse_qs(urlsplit(target).query)
    assert location["state"] == ["state-123"]
    tokens = token(client, info["client_id"], redirect, location["code"][0], verifier)
    assert tokens["access_token"]


def test_email_verification_completes_first_pairing_without_approval_click(journey):
    device = journey.client()
    pair = device.post("/api/agent/pair", json={"name": "Auto pairing agent", "mode": "poll"}).json()
    client, code = fresh_otp(journey, "auto-pair@example.test")
    verified = client.post(
        "/api/auth/verify",
        json={"email": "auto-pair@example.test", "code": code, "pair_code": pair["user_code"]},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["pair_binding_id"]
    state = device.post(
        "/api/agent/pair/status",
        json={"pair_id": pair["pair_id"], "device_secret": pair["device_secret"]},
    ).json()
    assert state["state"] == "approved"


def test_subscription_status_tool_works_before_purchase(journey):
    owner = journey.login("status-before-purchase@example.test")
    access = oauth_access(owner, "https://chatgpt.com/connector/oauth/status-before")
    result = mcp(
        owner,
        access,
        "tools/call",
        {"name": "get_subscription_status", "arguments": {}},
    )
    assert result["result"].get("isError") is not True
    value = json.loads(result["result"]["content"][0]["text"])
    assert value["subscription"]["status"] == "none"
    assert value["subscribe_url"].endswith("/app?flow=subscribe")
    assert isinstance(value["plans"], list)
    assert value["payment_by"] == "owner" and value["automatic_renewal"] is False
    assert value["terms_version"] and value["terms_url"].endswith("/terms")
    assert isinstance(value["payment_methods"], list)
    schedule = mcp(
        owner,
        access,
        "tools/call",
        {"name": "get_scan_schedule", "arguments": {}},
    )
    assert schedule["result"].get("isError") is not True
    timing = json.loads(schedule["result"]["content"][0]["text"])
    policy = timing["polling_policy"]
    assert policy["mode"] == "scan_windows_only"
    assert policy["checks_per_scan"] == 3
    assert policy["do_not_poll_between_scans"] is True
    assert "recommended_poll_interval_seconds" not in timing
    assert "inbox_polls" not in timing
    assert timing["scans"] and all(
        "checks_new_york" in item
        and "checks_utc" not in item
        and "checks_asia_shanghai" not in item
        and len(item["checks_new_york"]) == 3
        and all(
            datetime.fromisoformat(check) > datetime.fromisoformat(item["scan_at_new_york"])
            for check in item["checks_new_york"]
        )
        for item in timing["scans"]
    )


def test_device_agent_schedule_route(journey):
    owner = journey.login("device-schedule@example.test")
    journey.subscribe(owner)
    _, agent = journey.connect(owner)
    response = agent.get("/api/agent/schedule")
    assert response.status_code == 200
    schedule = response.json()
    assert schedule["polling_policy"]["mode"] == "scan_windows_only"
    assert schedule["polling_policy"]["checks_per_scan"] == 3
    assert "inbox_polls" not in schedule
