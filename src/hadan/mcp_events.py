"""MCP Events (webhook profile) for ChatGPT report subscriptions.

OpenAI's MCP Events extension is deliberately small: a server advertises
event definitions, accepts a webhook callback with a Standard Webhooks
secret, verifies that callback with a signed challenge, and then delivers one
signed JSON event per report publication or withdrawal.
"""
from __future__ import annotations

import base64
import concurrent.futures
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
CALLBACK_TOTAL_TIMEOUT = 20.0
DELIVERY_LEASE_SECONDS = 300
MAX_CALLBACK_ADDRESSES = 4
MAX_RESPONSE_BYTES = 64 * 1024
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-dns")


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


def _resolve_public(host: str, port: int, timeout: float | None = None) -> list[str]:
    try:
        if timeout is None:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        else:
            future = _DNS_POOL.submit(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
            infos = future.result(timeout=max(0.1, timeout))
    except concurrent.futures.TimeoutError:
        raise CallbackError("timeout", "callback host resolution timed out") from None
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


def _validate_callback(url: str, *, deadline: float | None = None) -> tuple[str, int, list[str]]:
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
    remaining = (
        max(0.1, deadline - time.monotonic())
        if deadline is not None
        else CALLBACK_TIMEOUT
    )
    addresses = _resolve_public(parts.hostname, port, timeout=remaining)[:MAX_CALLBACK_ADDRESSES]
    if not addresses:
        raise CallbackError("dns_failed", "callback host has no addresses")
    return parts.hostname, port, addresses


def validate_callback_url(url: str) -> tuple[str, int]:
    host, port, _ = _validate_callback(url)
    return host, port


def _post(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    deadline = time.monotonic() + CALLBACK_TOTAL_TIMEOUT
    parts = urlsplit(url)
    host, port, addresses = _validate_callback(url, deadline=deadline)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    last_error = None
    for address in addresses:
        connection = None
        sock = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Pin the address that was just validated, while keeping the
            # original hostname for SNI and certificate verification.
            sock = socket.create_connection(
                (address, port), timeout=min(CALLBACK_TIMEOUT, remaining)
            )
            context = ssl.create_default_context()
            wrapped = context.wrap_socket(sock, server_hostname=host)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallbackError("timeout", "callback deadline exceeded")
            wrapped.settimeout(min(CALLBACK_TIMEOUT, remaining))
            connection = http.client.HTTPSConnection(
                host, port, timeout=min(CALLBACK_TIMEOUT, remaining)
            )
            connection.sock = wrapped
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if time.monotonic() > deadline:
                raise CallbackError("timeout", "callback deadline exceeded")
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
        base = self.cfg.base_url.rstrip("/")
        return {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"],
            "capabilities": {"tools": {}, "events": {}},
            "instructions": (
                f"Quant private reports. Follow {base}/skill.md; protocol: "
                f"{base}/agent/instructions. Use MCP Events only when this client "
                "supports registered callbacks. Read each report and all attachments, "
                "verify them, then acknowledge; withdrawals and account notices follow "
                "the protocol's separate branches. Checkout requires the owner's "
                "explicit confirmation of the live plan, price, method and terms; show "
                "its QR directly. Tools alone do not provide background delivery."
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

    def _skip_delivery(self, db, delivery_id: str, reason: str, *, deactivate=None):
        now = time.time()
        db.execute(
            "UPDATE mcp_event_deliveries SET state='skipped',last_error=?,updated_at=? "
            "WHERE id=? AND state NOT IN ('accepted','skipped')",
            (reason, now, delivery_id),
        )
        if deactivate is not None:
            db.execute(
                "UPDATE mcp_event_subscriptions SET active=0,last_error=?,updated_at=? WHERE id=?",
                (reason, now, deactivate),
            )

    def deliver(self, subscription_id: str, event_id: str):
        """Claim and send one MCP event.

        Authorization, withdrawal state and the sending transition are checked
        in one database transaction. Once that commits the attempt is in
        flight; network I/O never holds the writer. Attempt fencing prevents a
        stale worker from overwriting a newer accepted/skipped result.
        """
        now = time.time()
        with self.s.db.transaction() as db:
            sub = db.execute(
                "SELECT m.*,s.status sub_status,s.expires_at sub_expires,s.agent_enabled,"
                "b.state binding_state,u.email "
                "FROM mcp_event_subscriptions m "
                "JOIN subscriptions s ON s.user_id=m.user_id AND s.product_id='quantgt' "
                "JOIN bindings b ON b.id=m.binding_id JOIN users u ON u.id=m.user_id "
                "WHERE m.id=?",
                (subscription_id,),
            ).fetchone()
            event = db.execute(
                "SELECT e.*,c.current_version,c.withdrawn,v.number version,v.title vtitle,v.body vbody,"
                "(SELECT title FROM versions WHERE content_id=e.content_id ORDER BY number DESC LIMIT 1) latest_title "
                "FROM events e LEFT JOIN contents c ON c.id=e.content_id "
                "LEFT JOIN versions v ON v.id=e.version_id WHERE e.id=?",
                (event_id,),
            ).fetchone()
            delivery = db.execute(
                "SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?",
                (subscription_id, event_id),
            ).fetchone()
            if not sub or not event or not delivery:
                raise Problem("MCP event delivery no longer exists", 404)
            # A callback accepted by the remote endpoint is a terminal transport
            # result. A reclaimed job must not post the same event again.
            if delivery["state"] in ("accepted", "skipped", "failed"):
                return
            # A recent `sending` row belongs to another in-flight attempt (or a
            # worker whose lease has not expired). Only a stale claim is
            # recoverable; this prevents duplicate posts during overlap.
            if (
                delivery["state"] == "sending"
                and delivery["updated_at"] > now - DELIVERY_LEASE_SECONDS
            ):
                return
            if event["product_id"] != "quantgt":
                self._skip_delivery(db, delivery["id"], "product mismatch")
                return
            allowed = not self.s.settings.allowed_emails or sub["email"] in self.s.settings.allowed_emails or bool(
                db.execute(
                    "SELECT email FROM access_allowlist WHERE email=?", (sub["email"],)
                ).fetchone()
            )
            if not allowed:
                self._skip_delivery(
                    db, delivery["id"], "access revoked", deactivate=subscription_id
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
                self._skip_delivery(
                    db, delivery["id"], "access revoked", deactivate=subscription_id
                )
                return
            if event["content_id"] is None or event["current_version"] is None:
                self._skip_delivery(db, delivery["id"], "content missing")
                return
            is_withdrawal = event["kind"] == "withdrawn"
            # A queued publication/update must never carry a report body after
            # the owner withdrew it. The separate withdrawal event is the only
            # notification allowed to describe the retraction.
            if not is_withdrawal and event["withdrawn"]:
                self._skip_delivery(db, delivery["id"], "content withdrawn")
                return
            if is_withdrawal and not event["withdrawn"]:
                self._skip_delivery(db, delivery["id"], "withdrawal event mismatch")
                return
            if not is_withdrawal and event["version"] is None:
                self._skip_delivery(db, delivery["id"], "version missing")
                return
            version = int(event["version"] or event["current_version"] or 1)
            if is_withdrawal:
                # Keep only a recognizable title and the retraction reason.
                title = event["latest_title"] or event["summary"] or "Quant update"
                summary = event["summary"] or ""
            else:
                title = event["vtitle"] or event["summary"] or "Quant update"
                summary = event["vbody"] or ""
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
                    "change_type": "withdrawn" if is_withdrawal else event["kind"],
                    "withdrawn": bool(is_withdrawal),
                    "published_at": _iso(event["created_at"]),
                    "url": self.cfg.base_url.rstrip("/") + "/read/" + event["content_id"],
                },
            }
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if len(body) > MAX_BODY:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='failed',last_error='payload too large',updated_at=? "
                    "WHERE id=? AND state NOT IN ('accepted','skipped','failed')",
                    (now, delivery["id"]),
                )
                return
            expected_attempts = int(delivery["attempts"]) + 1
            claimed = db.execute(
                "UPDATE mcp_event_deliveries SET state='sending',attempts=?,updated_at=? "
                "WHERE id=? AND attempts=? AND "
                "(state IN ('queued','retrying') OR (state='sending' AND updated_at<=?))",
                (
                    expected_attempts,
                    now,
                    delivery["id"],
                    int(delivery["attempts"]),
                    now - DELIVERY_LEASE_SECONDS,
                ),
            ).rowcount
            if not claimed:
                return
            callback_url = sub["callback_url"]
            encrypted_secret = sub["secret"]
        # The claim is committed; this attempt is in flight. Build the signature
        # outside the writer transaction.
        try:
            key = _secret_key(decrypt(self.cfg.secret, encrypted_secret))
        except (CallbackError, ValueError):
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='failed',last_error='invalid_secret',updated_at=? "
                    "WHERE id=? AND state='sending' AND attempts=?",
                    (time.time(), delivery["id"], expected_attempts),
                )
            raise DeliveryError("MCP callback secret is invalid", retry=False) from None
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
            status, response = _post(callback_url, body, headers)
        except CallbackError as exc:
            with self.s.db.transaction() as db:
                db.execute(
                    "UPDATE mcp_event_deliveries SET state='retrying',last_error=?,updated_at=? "
                    "WHERE id=? AND state='sending' AND attempts=?",
                    (exc.reason, time.time(), delivery["id"], expected_attempts),
                )
            raise DeliveryError("MCP callback: " + exc.reason, retry=True)
        if 200 <= status < 300:
            with self.s.db.transaction() as db:
                updated = db.execute(
                    "UPDATE mcp_event_deliveries SET state='accepted',last_error=NULL,updated_at=? "
                    "WHERE id=? AND state='sending' AND attempts=?",
                    (time.time(), delivery["id"], expected_attempts),
                ).rowcount
                if updated:
                    db.execute(
                        "UPDATE mcp_event_subscriptions SET last_error=NULL,updated_at=? WHERE id=?",
                        (time.time(), subscription_id),
                    )
            return
        retryable = status in (408, 425, 429) or status >= 500
        with self.s.db.transaction() as db:
            db.execute(
                "UPDATE mcp_event_deliveries SET state=?,last_error=?,updated_at=? "
                "WHERE id=? AND state='sending' AND attempts=?",
                (
                    "retrying" if retryable else "failed",
                    f"HTTP {status}",
                    time.time(),
                    delivery["id"],
                    expected_attempts,
                ),
            )
        if not retryable:
            raise DeliveryError("MCP callback rejected the event", retry=False)
        raise DeliveryError("MCP callback returned HTTP " + str(status), retry=True)
