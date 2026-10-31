"""Exercise the pinned HTTPS callback transport without external network."""
from __future__ import annotations

import ssl
import pytest
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hadan import mcp_events


class Handler(BaseHTTPRequestHandler):
    body = b""

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        Handler.body = self.rfile.read(length)
        payload = b'{"challenge":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def test_pinned_callback_post_uses_validated_address_and_path(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        monkeypatch.setattr(
            "hadan.mcp_events._resolve_public", lambda host, port, timeout=None: ["127.0.0.1"]
        )
        monkeypatch.setattr(
            "hadan.mcp_events.ssl.create_default_context",
            lambda: ssl._create_unverified_context(),
        )
        status, body = mcp_events._post(
            f"https://localhost:{port}/callback?token=1",
            b'{"a":1}',
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert body == b'{"challenge":"ok"}'
        assert Handler.body == b'{"a":1}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_callback_has_a_total_deadline_including_resolution(monkeypatch):
    import time

    from hadan.mcp_events import CallbackError

    monkeypatch.setattr(mcp_events, "CALLBACK_TOTAL_TIMEOUT", 0.15)

    def slow_validation(url, *, deadline=None):
        time.sleep(0.25)
        return "example.com", 443, ["1.1.1.1"]

    monkeypatch.setattr(mcp_events, "_validate_callback", slow_validation)
    with pytest.raises(CallbackError) as raised:
        mcp_events._post(
            "https://example.com/callback",
            b"{}",
            {"Content-Type": "application/json"},
        )
    assert raised.value.reason == "timeout"
