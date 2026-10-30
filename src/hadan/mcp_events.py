"""MCP Events (webhook profile) for ChatGPT report subscriptions.

OpenAI's MCP Events extension is deliberately small: a server advertises
event definitions, accepts a webhook callback with a Standard Webhooks
secret, verifies that callback with a signed challenge, and then delivers one
signed JSON event per report publication or withdrawal.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .db import audit, enqueue, uid
from .security import decrypt, digest, encrypt, token
from .service import Problem
from .transport import DeliveryError

EVENT_NAME = "quant.report.published"
EVENT_TTL_MS = 30 * 86400 * 1000
MIN_TTL_MS = 3600 * 1000
MAX_TTL_MS = 90 * 86400 * 1000
MAX_BODY = 256 * 1024
CALLBACK_TIMEOUT = 12.0
MAX_RESPONSE_BYTES = 64 * 1024


class CallbackError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _iso(value: float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _secret_key(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise CallbackError("invalid_secret", "secret must start with whsec_")
    encoded = secret[6:].strip()
    if not encoded or len(encoded) > 512:
        raise CallbackError("invalid_secret", "secret is empty or too long")
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except Exception:
        try:
            raw = base64.urlsafe_b64decode(padded)
        except Exception:
            raise CallbackError("invalid_secret", "secret is not valid base64") from None
    if not 24 <= len(raw) <= 64:
        raise CallbackError("invalid_secret", "secret key must decode to 24-64 bytes")
    return raw


def _signature(key: bytes, webhook_id: str, timestamp: str, body: bytes) -> str:
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    return "v1," + base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()


def _resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise CallbackError("dns_failed", "callback host could not be resolved") from None
    if not infos:
        raise CallbackError("dns_failed", "callback host has no addresses")
    addresses = []
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise CallbackError("invalid_address", "callback address is invalid") from None
        if not ip.is_global:
            raise CallbackError("private_address", "callback must use a public address")
        if address not in addresses:
            addresses.append(address)
    return addresses


def _public_host(host: str, port: int) -> None:
    _resolve_public(host, port)


def _validate_callback(url: str) -> tuple[str, int, list[str]]:
    if not isinstance(url, str) or len(url) > 2048 or "\r" in url or "\n" in url:
        raise CallbackError("invalid_url", "invalid callback URL")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise CallbackError("invalid_url", "callback must be an HTTPS URL without credentials or fragment")
    try:
        port = parts.port or 443
    except ValueError:
        raise CallbackError("invalid_url", "invalid callback port") from None
    if not 1 <= port <= 65535:
        raise CallbackError("invalid_url", "invalid callback port")
    return parts.hostname, port, _resolve_public(parts.hostname, port)


def validate_callback_url(url: str) -> tuple[str, int]:
    host, port, _ = _validate_callback(url)
    return host, port


def _post(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    parts = urlsplit(url)
    host, port, addresses = _validate_callback(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    last_error = None
    for address in addresses:
        connection = None
        sock = None
        try:
            # Pin the address that was just validated, while keeping the
            # original hostname for SNI and certificate verification.
            sock = socket.create_connection((address, port), timeout=CALLBACK_TIMEOUT)
            context = ssl.create_default_context()
            wrapped = context.wrap_socket(sock, server_hostname=host)
            connection = http.client.HTTPSConnection(host, port, timeout=CALLBACK_TIMEOUT)
            connection.sock = wrapped
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise CallbackError("response_too_large", "callback response too large")
            return response.status, data
        except CallbackError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
            elif sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise CallbackError("timeout", type(last_error).__name__ if last_error else "connection failed")


def verify_callback(url: str, secret: str, subscription_id: str) -> None:
    key = _secret_key(secret)
    challenge = token()
    webhook_id = "msg_" + uid()
    timestamp = str(int(time.time()))
    body = json.dumps(
        {"type": "verification", "challenge": challenge},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    status, response = _post(
        url,
        body,
        {
            "Content-Type": "application/json",
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(key, webhook_id, timestamp, body),
            "X-MCP-Subscription-Id": subscription_id,
            "User-Agent": "Quant-MCP-Events/1.0",
        },
    )
    if status < 200 or status >= 300:
        raise CallbackError("challenge_failed", "callback returned HTTP " + str(status))
    try:
        echoed = json.loads(response.decode("utf-8")).get("challenge", "")
    except Exception:
        raise CallbackError("challenge_failed", "callback did not return JSON") from None
    if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
        raise CallbackError("challenge_failed", "callback challenge did not match")


def subscription_id(user_id: str, url: str, name: str, arguments: dict) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sub_" + digest(user_id + "\n" + url + "\n" + name + "\n" + canonical)[:40]


def event_definition() -> dict:
    return {
        "name": EVENT_NAME,
        "description": "A new, revised, or withdrawn Quant report is available.",
        "delivery": ["webhook"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "enum": ["quantgt"]},
            },
            "additionalProperties": False,
        },
        "payloadSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "content_id": {"type": "string"},
                "version": {"type": "integer"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "change_type": {"type": "string"},
                "withdrawn": {"type": "boolean"},
                "published_at": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": [
                "event_id",
                "content_id",
                "version",
                "title",
                "summary",
                "change_type",
                "withdrawn",
                "published_at",
                "url",
            ],
            "additionalProperties": False,
        },
    }


def _arguments(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Problem("arguments must be an object", 400)
    allowed = {"product_id"}
    if set(value) - allowed:
        raise Problem("unsupported event arguments", 400)
    product_id = value.get("product_id", "quantgt")
    if product_id != "quantgt":
        raise Problem("unsupported product_id", 400)
    return {"product_id": product_id}


def _ttl_ms(value) -> int:
    if value is None:
        return EVENT_TTL_MS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Problem("ttlMs must be a number or null", 400)
    return min(MAX_TTL_MS, max(MIN_TTL_MS, int(value)))


class MCPEvents:
    def __init__(self, service):
        self.s = service
        self.cfg = service.settings

    def discover(self) -> dict:
        return {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"],
            "capabilities": {"tools": {}, "events": {}},
            "instructions": (
                "Quant private subscription. Use read_content and read_attachment "
                "after a report event; acknowledge only after processing. MCP Events "
                "delivers new reports without agent polling. After the owner approves "
                "a checkout on the website, prepare_checkout returns a QR image to show "
                "inline; never open a browser for the owner."
            ),
            "ttlMs": 3600000,
            "cacheScope": "public",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "hadan-subscriptions",
                    "version": "0.2.0",
                }
            },
        }

    def list(self, binding) -> dict:
        return {"events": [event_definition()]}

    def subscribe(self, binding, params: dict) -> dict:
        if not isinstance(params, dict):
            raise Problem("params must be an object", 400)
        subscription = self.s.entitled(binding["user_id"], "quantgt")
        if not subscription or not subscription["agent_enabled"]:
            raise Problem("An active Quant subscription with agent delivery is required", 403)
        name = params.get("name")
        if name != EVENT_NAME:
            raise Problem("unknown event name", 400)
        arguments = _arguments(params.get("arguments"))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise Problem("webhook delivery is required", 400)
        url = delivery.get("url")
        secret = delivery.get("secret")
        if not isinstance(url, str) or not isinstance(secret, str):
            raise Problem("delivery.url and delivery.secret are required", 400)
        try:
            _secret_key(secret)
            validate_callback_url(url)
        except CallbackError as exc:
            raise Problem("invalid webhook delivery: " + exc.reason, 400) from None
        ttl = _ttl_ms(params.get("ttlMs"))
        expires = time.time() + ttl / 1000.0
        sid = subscription_id(binding["user_id"], url, name, arguments)
        with self.s.db.transaction() as db:
            self.s._limit(db, "mcp-subscribe:" + binding["user_id"], 20, 3600)
        # Verification happens before the callback can receive any private data.
        verify_callback(url, secret, sid)
        now = time.time()
        with self.s.db.transaction() as db:
            existing = db.execute(
                "SELECT * FROM mcp_event_subscriptions WHERE id=?", (sid,)
            ).fetchone()
            encrypted = encrypt(self.cfg.secret, secret)
            if existing:
                if existing["user_id"] != binding["user_id"]:
                    raise Problem("subscription belongs to another account", 403)
                db.execute(
                    "UPDATE mcp_event_subscriptions SET binding_id=?,name=?,arguments=?,callback_url=?,secret=?,refresh_before=?,active=1,updated_at=?,last_error=NULL WHERE id=?",
                    (
                        binding["id"],
                        name,
                        json.dumps(arguments, ensure_ascii=False),
                        url,
                        encrypted,
                        expires,
                        now,
                        sid,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO mcp_event_subscriptions(id,user_id,binding_id,name,arguments,callback_url,secret,refresh_before,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        binding["user_id"],
                        binding["id"],
                        name,
                        json.dumps(arguments, ensure_ascii=False),
                        url,
                        encrypted,
                        expires,
                        1,
                        now,
                        now,
                    ),
                )
            audit(
                db,
                binding["user_id"],
                "mcp_event_subscribed",
                sid,
                {"event": name, "url": url, "refresh_before": expires},
            )
        return {
            "id": sid,
            "refreshBefore": _iso(expires),
            "cursor": None,
            "truncated": bool(params.get("cursor")),
        }

    def unsubscribe(self, binding, params: dict) -> dict:
        if not isinstance(params, dict):
            raise Problem("params must be an object", 400)
        name = params.get("name")
        if name != EVENT_NAME:
            raise Problem("unknown event name", 400)
        arguments = _arguments(params.get("arguments"))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise Problem("webhook delivery is required", 400)
        url = delivery.get("url")
        if not isinstance(url, str):
            raise Problem("delivery.url is required", 400)
        sid = subscription_id(binding["user_id"], url, name, arguments)
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM mcp_event_subscriptions WHERE id=?", (sid,)
            ).fetchone()
            if row and row["user_id"] != binding["user_id"]:
                raise Problem("subscription belongs to another account", 403)
            db.execute(
                "UPDATE mcp_event_subscriptions SET active=0,updated_at=? WHERE id=?",
                (time.time(), sid),
            )
            if row:
                audit(db, binding["user_id"], "mcp_event_unsubscribed", sid, {"event": name})
        return {}

    def enqueue_for_event(self, db, event_id: str, product_id: str, *, only_user=None):
        """Queue one signed webhook per active subscription for a content event."""
        if product_id != "quantgt":
            return 0
        rows = db.execute(
            "SELECT m.id,m.user_id,m.binding_id FROM mcp_event_subscriptions m "
            "JOIN subscriptions s ON s.user_id=m.user_id AND s.product_id=? "
            "JOIN bindings b ON b.id=m.binding_id AND b.state='active' "
            "WHERE m.active=1 AND m.refresh_before>? AND s.status='active' "
            "AND (s.expires_at IS NULL OR s.expires_at>?) AND s.agent_enabled=1"
            + (" AND m.user_id=?" if only_user else ""),
            (product_id, time.time(), time.time(), *((only_user,) if only_user else ())),
        ).fetchall()
        for row in rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO mcp_event_deliveries(id,subscription_id,event_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (uid(), row["id"], event_id, "queued", time.time(), time.time()),
            )
            if cur.rowcount:
                enqueue(
                    db,
                    "mcp_event_deliver",
                    {"subscription_id": row["id"], "event_id": event_id},
                    "mcp-event:" + row["id"] + ":" + event_id,
                    queue="main",
                )
        return len(rows)

    def deliver(self, subscription_id: str, event_id: str):
        now = time.time()
        sub = self.s.db.one(
            "SELECT m.*,s.status sub_status,s.expires_at sub_expires,s.agent_enabled,b.state binding_state,u.email "
            "FROM mcp_event_subscriptions m JOIN subscriptions s ON s.user_id=m.user_id AND s.product_id='quantgt' "
            "JOIN bindings b ON b.id=m.binding_id JOIN users u ON u.id=m.user_id WHERE m.id=?",
            (subscription_id,),
        )
        event = self.s.db.one(
            "SELECT e.*,c.current_version,c.withdrawn,v.number version,v.title vtitle,v.body vbody,"
            "(SELECT title FROM versions WHERE content_id=e.content_id ORDER BY number DESC LIMIT 1) latest_title "
            "FROM events e LEFT JOIN contents c ON c.id=e.content_id "
            "LEFT JOIN versions v ON v.id=e.version_id WHERE e.id=?",
            (event_id,),
        )
        delivery = self.s.db.one(
            "SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?",
            (subscription_id, event_id),
        )
        if not sub or not event or not delivery:
            raise Problem("MCP event delivery no longer exists", 404)
        if not self.s._email_allowed(sub["email"]):
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_subscriptions SET active=0,last_error='access revoked',updated_at=? WHERE id=?",
                    (now, subscription_id),
                )
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='skipped',last_error='access revoked',updated_at=? WHERE id=?",
                    (now, delivery["id"]),
                )
            return
        if event["product_id"] != "quantgt":
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='skipped',last_error='product mismatch',updated_at=? WHERE id=?",
                    (now, delivery["id"]),
                )
            return
        if (
            not sub["active"]
            or sub["refresh_before"] <= now
            or sub["binding_state"] != "active"
            or sub["sub_status"] != "active"
            or (sub["sub_expires"] is not None and sub["sub_expires"] <= now)
            or not sub["agent_enabled"]
        ):
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_subscriptions SET active=0,last_error='access revoked',updated_at=? WHERE id=?",
                    (now, subscription_id),
                )
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='skipped',last_error='access revoked',updated_at=? WHERE id=?",
                    (now, delivery["id"]),
                )
            return
        version = int(event["version"] or event["current_version"] or 1)
        title = event["vtitle"] or event["latest_title"] or event["summary"] or "Quant update"
        summary = (event["vbody"] if event["kind"] != "withdrawn" else event["summary"]) or ""
        payload = {
            "eventId": event_id,
            "name": EVENT_NAME,
            "timestamp": _iso(event["created_at"]),
            "data": {
                "event_id": event_id,
                "content_id": event["content_id"],
                "version": version,
                "title": title,
                "summary": summary[:1000],
                "change_type": "withdrawn" if event["kind"] == "withdrawn" or event["withdrawn"] else event["kind"],
                "withdrawn": bool(event["withdrawn"] or event["kind"] == "withdrawn"),
                "published_at": _iso(event["created_at"]),
                "url": self.cfg.base_url.rstrip("/") + "/read/" + event["content_id"],
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_BODY:
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='failed',last_error='payload too large',updated_at=? WHERE id=?",
                    (now, delivery["id"]),
                )
            raise DeliveryError("MCP event payload too large", retry=False)
        key = _secret_key(decrypt(self.cfg.secret, sub["secret"]))
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(key, event_id, timestamp, body),
            "X-MCP-Subscription-Id": subscription_id,
            "User-Agent": "Quant-MCP-Events/1.0",
        }
        try:
            status, response = _post(sub["callback_url"], body, headers)
        except CallbackError as exc:
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='retrying',attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",
                    (exc.reason, now, delivery["id"]),
                )
            raise DeliveryError("MCP callback: " + exc.reason, retry=True)
        if 200 <= status < 300:
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='accepted',attempts=attempts+1,last_error=NULL,updated_at=? WHERE id=?",
                    (now, delivery["id"]),
                )
                db.execute(
                    "UPDATE mcp_event_subscriptions SET last_error=NULL,updated_at=? WHERE id=?",
                    (now, subscription_id),
                )
            return
        retryable = status in (408, 425, 429) or status >= 500
        with self.s.db.transaction() as db:
            db.execute(
                "UPDATE mcp_event_deliveries SET state=?,attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",
                ("retrying" if retryable else "failed", f"HTTP {status}", now, delivery["id"]),
            )
        if not retryable:
            raise DeliveryError("MCP callback rejected the event", retry=False)
        raise DeliveryError("MCP callback returned HTTP " + str(status), retry=True)
