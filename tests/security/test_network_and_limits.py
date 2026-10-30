import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from hadan.security import action_token, webhook_target
from hadan.transport import DeliveryError, Transport


def fake_dns(*ips):
    return [
        (
            socket.AF_INET6 if ":" in ip else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (ip, 443),
        )
        for ip in ips
    ]


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.1.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_webhook_rejects_non_public_and_multicast_targets(monkeypatch, ip):
    monkeypatch.setattr(
        "hadan.security.socket.getaddrinfo", lambda *_a, **_kw: fake_dns(ip)
    )
    with pytest.raises(ValueError):
        webhook_target("https://callback.example/events")


def test_webhook_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(
        "hadan.security.socket.getaddrinfo",
        lambda *_a, **_kw: fake_dns("93.184.215.14", "::1"),
    )
    with pytest.raises(ValueError):
        webhook_target("https://callback.example/events")


@pytest.mark.parametrize(
    "url",
    [
        "http://callback.example/",
        "https://user:password@callback.example/",
        "https://callback.example:22/",
        "https://callback.example/?token=secret",
        "https://callback.example/#fragment",
        "file:///etc/passwd",
    ],
)
def test_webhook_rejects_unsafe_url_forms(url):
    with pytest.raises(ValueError):
        webhook_target(url)


def test_webhook_uses_checked_address_preserves_tls_hostname_no_redirect(
    harness, monkeypatch
):
    _, service, _ = harness
    records = {"dns": [], "connect": [], "sni": [], "requests": [], "shutdown": []}

    def resolve(host, *_args, **_kwargs):
        records["dns"].append(host)
        # A second hostname lookup would return a private target (rebinding).
        return fake_dns("93.184.215.14" if len(records["dns"]) == 1 else "127.0.0.1")

    class Socket:
        def __init__(self, kind):
            self.kind = kind

        def close(self):
            pass

        def do_handshake(self):
            pass

        def shutdown(self, _how):
            records["shutdown"].append(self.kind)

    class Context:
        def wrap_socket(self, sock, server_hostname, **kwargs):
            records["sni"].append(server_hostname)
            # Real wrap_socket detaches the original socket. The deadline must
            # interrupt the returned TLS socket, not the closed original.
            return Socket("tls")

    class Timer:
        def __init__(self, interval, callback):
            records["timer"] = callback

        def start(self):
            pass

        def cancel(self):
            pass

    class Response:
        status = 302

        def read(self, length):
            return b""

    class Connection:
        def __init__(self, host, port, timeout, context):
            self.sock = None

        def request(self, method, path, body, headers):
            records["requests"].append((method, path, headers))

        def getresponse(self):
            records["timer"]()
            return Response()

        def close(self):
            pass

    def connect(address, timeout):
        records["connect"].append(address)
        return Socket("plain")

    monkeypatch.setattr("hadan.transport.threading.Timer", Timer)
    monkeypatch.setattr("hadan.security.socket.getaddrinfo", resolve)
    monkeypatch.setattr("hadan.transport.socket.create_connection", connect)
    monkeypatch.setattr("hadan.transport.ssl.create_default_context", Context)
    monkeypatch.setattr("hadan.transport.http.client.HTTPSConnection", Connection)
    with pytest.raises(DeliveryError):
        Transport(service.settings).webhook(
            "https://callback.example/events",
            {"event_id": "event-one"},
            "test-secret",
            "event-one",
        )
    assert records["dns"] == ["callback.example"]
    assert records["connect"] == [("93.184.215.14", 443)]
    assert records["sni"] == ["callback.example"]
    assert records["shutdown"] == ["tls"]
    assert len(records["requests"]) == 1
    assert records["requests"][0][2]["X-Hadan-Signature"].startswith("v1=")


def test_webhook_cannot_activate_before_endpoint_delivery_test(harness, monkeypatch):
    _, service, login = harness
    client, _ = login("alice@example.com")
    monkeypatch.setattr(
        "hadan.security.socket.getaddrinfo",
        lambda *_a, **_kw: fake_dns("93.184.215.14"),
    )
    binding = client.post(
        "/api/bindings",
        json={
            "name": "Unreachable webhook",
            "mode": "webhook",
            "endpoint": "https://callback.example/events",
        },
    ).json()
    auth = {"Authorization": "Bearer " + binding["token"]}
    assert client.get("/api/agent/test", headers=auth).status_code == 200
    assert client.get("/api/agent/test-file", headers=auth).status_code == 200
    # No worker has attempted the webhook test, and the endpoint has never acknowledged it.
    response = client.post("/api/agent/test/complete", headers=auth)
    assert response.status_code == 409, response.text
    assert (
        service.db.one("SELECT state FROM bindings WHERE id=?", (binding["id"],))[
            "state"
        ]
        == "pending"
    )


def test_invalid_content_length_returns_client_error(harness):
    app, _, _ = harness
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/auth/request",
            headers={"Content-Length": "not-a-number"},
            json={"email": "alice@example.com"},
        )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path", ["/api/auth/request", "/unsubscribe/" + "a" * 32 + ".invalid"]
)
def test_streaming_body_is_bounded_before_buffering_entire_request(harness, path):
    app, _, _ = harness
    received = 0
    sent = []
    total_chunks = 40

    async def exercise():
        async def receive():
            nonlocal received
            if received >= total_chunks:
                await asyncio.Event().wait()
            received += 1
            return {
                "type": "http.request",
                "body": b"x" * (1024 * 1024),
                "more_body": received < total_chunks,
            }

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
            ],
            "client": ("192.0.2.1", 1234),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    asyncio.run(exercise())
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    assert status == 413
    assert received <= 25, (
        "request was fully buffered before the size limit was checked"
    )


def test_unauthenticated_publish_rejects_before_reading_large_body(harness):
    app, _, _ = harness
    received = 0
    sent = []

    async def exercise():
        async def receive():
            nonlocal received
            received += 1
            return {
                "type": "http.request",
                "body": b"x" * (1024 * 1024),
                "more_body": True,
            }

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/admin/publish",
            "raw_path": b"/api/admin/publish",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
            ],
            "client": ("192.0.2.1", 1234),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(exercise())
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    assert status == 401
    assert received == 0
