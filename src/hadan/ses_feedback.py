"""SES event feedback over an SNS HTTPS subscription.

Only a verified SNS message may change local suppression state. Delivery
events are recorded but never suppress. Permanent bounces and complaints
suppress the affected mailbox; transient bounces and rejects open an
incident for the operator/ops agent instead. The SNS message id is the
idempotency key, so a retry cannot double-count an incident.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from .db import audit, enqueue, uid
from .security import email_address
from .service import Problem

SNS_HOST = re.compile(r"^sns(?:\.[a-z0-9-]+)?\.amazonaws\.com$")
MAX_BODY = 256 * 1024
MAX_CERT = 128 * 1024


def _canonical(value):
    kind = value["Type"]
    if kind == "Notification":
        keys = ["Message", "MessageId"]
        if "Subject" in value:
            keys.append("Subject")
        keys += ["Timestamp", "TopicArn", "Type"]
    else:
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    for key in keys:
        if not isinstance(value.get(key), str):
            raise Problem("反馈通知无效", 400)
    # SNS signs "<field name>\\n<field value>\\n" pairs, not values alone.
    return "".join(f"{key}\n{value[key]}\n" for key in keys)


def _https_host(url, *, allow_query, suffix):
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and (allow_query or not parsed.query)
        and SNS_HOST.fullmatch(parsed.hostname.lower())
        and parsed.path.startswith("/")
        and parsed.path.endswith(suffix)
    )


class SesFeedback:
    def __init__(self, service, *, cert_fetcher=None, confirm_get=None):
        self.s = service
        self.cfg = service.settings
        self._cert_fetcher = cert_fetcher or self._fetch_cert
        self._confirm_get = confirm_get or self._http_get
        self._certs = {}

    def handle(self, raw, headers=None):
        if not self.cfg.ses_feedback_enabled:
            raise Problem("邮件反馈通道未启用", 503)
        if not self.cfg.ses_sns_topic_arn:
            raise Problem("邮件反馈通道未配置", 503)
        if len(raw) > MAX_BODY:
            raise Problem("反馈通知过大", 413)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Problem("反馈通知无效", 400) from None
        if not isinstance(value, dict):
            raise Problem("反馈通知无效", 400)
        if value.get("TopicArn") != self.cfg.ses_sns_topic_arn:
            raise Problem("反馈来源不匹配", 403)
        kind = value.get("Type")
        if kind not in (
            "Notification",
            "SubscriptionConfirmation",
            "UnsubscribeConfirmation",
        ):
            raise Problem("反馈通知无效", 400)
        self._verify(value)
        if kind == "SubscriptionConfirmation":
            self._confirm(value)
            return {"received": True, "type": kind}
        if kind == "UnsubscribeConfirmation":
            return {"received": True, "type": kind}
        return self._process(value)

    def _verify(self, value):
        version = value.get("SignatureVersion")
        algorithms = {"1": hashes.SHA1(), "2": hashes.SHA256()}
        algorithm = algorithms.get(version)
        if algorithm is None:
            raise Problem("签名无效", 403)
        cert = self._certificate(value.get("SigningCertURL"))
        signature = value.get("Signature")
        try:
            raw_signature = base64.b64decode(signature, validate=True)
            cert.public_key().verify(
                raw_signature,
                _canonical(value).encode("utf-8"),
                padding.PKCS1v15(),
                algorithm,
            )
        except (InvalidSignature, ValueError, TypeError):
            raise Problem("签名无效", 403) from None

    def _certificate(self, url):
        if not _https_host(url, allow_query=False, suffix=".pem"):
            raise Problem("签名证书地址无效", 403)
        now = time.time()
        cached = self._certs.get(url)
        if cached and now - cached[0] < 3600:
            return cached[1]
        try:
            cert = self._cert_fetcher(url)
        except Problem:
            raise
        except Exception:
            raise Problem("签名证书获取失败", 502) from None
        if not isinstance(cert, x509.Certificate):
            raise Problem("签名证书无效", 403)
        if len(self._certs) >= 8:
            self._certs.pop(next(iter(self._certs)))
        self._certs[url] = (now, cert)
        return cert

    def _fetch_cert(self, url):
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(url)
        if response.status_code // 100 != 2 or len(response.content) > MAX_CERT:
            raise Problem("签名证书获取失败", 502)
        try:
            return x509.load_pem_x509_certificate(response.content)
        except ValueError:
            raise Problem("签名证书无效", 403) from None

    def _confirm(self, value):
        url = value.get("SubscribeURL")
        if not _https_host(url, allow_query=True, suffix=""):
            raise Problem("订阅确认地址无效", 403)
        try:
            response = self._confirm_get(url)
        except Problem:
            raise
        except Exception:
            raise Problem("订阅确认失败", 502) from None
        if response.status_code // 100 != 2:
            raise Problem("订阅确认失败", 502)

    def _http_get(self, url):
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            return client.get(url)

    def _process(self, value):
        try:
            message = json.loads(value["Message"])
        except (KeyError, ValueError, UnicodeError, RecursionError):
            raise Problem("反馈通知无效", 400) from None
        if not isinstance(message, dict):
            raise Problem("反馈通知无效", 400)
        event_type = str(message.get("eventType", ""))[:64]
        mail = message.get("mail") if isinstance(message.get("mail"), dict) else {}
        message_id = str(mail.get("messageId", ""))[:256]
        sns_id = str(value.get("MessageId", ""))[:256]
        digest = hashlib.sha256(value["Message"].encode("utf-8")).hexdigest()
        now = time.time()
        suppressed = []
        with self.s.db.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO ses_feedback(id,event_type,message_id,payload_digest,created_at) VALUES(?,?,?,?,?)",
                (sns_id, event_type, message_id, digest, now),
            ).rowcount
            if not inserted:
                return {"received": True, "duplicate": True}
            if event_type == "Bounce":
                bounce = message.get("bounce") if isinstance(message.get("bounce"), dict) else {}
                kind = str(bounce.get("bounceType", ""))[:32]
                recipients = [
                    str(x.get("emailAddress", ""))
                    for x in bounce.get("bouncedRecipients", [])
                    if isinstance(x, dict)
                ]
                if kind == "Permanent":
                    for address in recipients:
                        if self._suppress(db, address, "bounce", now):
                            suppressed.append(address.lower().strip())
                self._incident(
                    db,
                    "SES bounce " + (kind or "unknown"),
                    message_id,
                    recipients,
                    {"event": event_type, "bounce_type": kind},
                    now,
                )
            elif event_type == "Complaint":
                complaint = (
                    message.get("complaint")
                    if isinstance(message.get("complaint"), dict)
                    else {}
                )
                recipients = [
                    str(x.get("emailAddress", ""))
                    for x in complaint.get("complainedRecipients", [])
                    if isinstance(x, dict)
                ]
                for address in recipients:
                    if self._suppress(db, address, "complaint", now):
                        suppressed.append(address.lower().strip())
                self._incident(
                    db, "SES complaint", message_id, recipients, {"event": event_type}, now
                )
            elif event_type in ("Reject", "DeliveryDelay"):
                recipients = [
                    str(x) for x in (mail.get("destination") or []) if isinstance(x, str)
                ]
                self._incident(
                    db,
                    "SES " + event_type,
                    message_id,
                    recipients,
                    {"event": event_type},
                    now,
                )
            elif event_type != "Delivery":
                recipients = [
                    str(x) for x in (mail.get("destination") or []) if isinstance(x, str)
                ]
                self._incident(
                    db,
                    "SES unknown feedback " + (event_type or "missing"),
                    message_id,
                    recipients,
                    {"event": event_type},
                    now,
                )
            audit(
                db,
                "ses",
                "feedback_" + (event_type or "unknown"),
                sns_id,
                {
                    "message_id": message_id,
                    "recipients": suppressed,
                    "bounce_type": (
                        str(message.get("bounce", {}).get("bounceType", ""))[:32]
                        if isinstance(message.get("bounce"), dict)
                        else ""
                    ),
                },
            )
        return {
            "received": True,
            "event": event_type,
            "suppressed": len(suppressed),
        }

    def _suppress(self, db, address, reason, now):
        try:
            address = email_address(address)
        except ValueError:
            return False
        db.execute(
            "INSERT OR REPLACE INTO suppressions(email,reason,created_at) VALUES(?,?,?)",
            (address, reason, now),
        )
        return True

    def _incident(self, db, summary, message_id, recipients, details, now):
        fingerprint = hashlib.sha256(
            ("ses-feedback:" + summary + ":" + message_id).encode("utf-8")
        ).hexdigest()
        existing = db.execute(
            "SELECT id FROM incidents WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE incidents SET state='open',count=count+1,updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
            return
        incident_id = uid()
        db.execute(
            "INSERT INTO incidents(id,fingerprint,kind,summary,details,created_at,updated_at) VALUES(?,?,'ses_feedback',?,?,?,?)",
            (
                incident_id,
                fingerprint,
                summary,
                json.dumps(
                    {
                        **details,
                        "message_id": message_id[:256],
                        "recipients": [r[:254] for r in recipients[:20]],
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        if self.cfg.ops_ai:
            enqueue(
                db,
                "diagnose",
                {"incident_id": incident_id},
                "diagnose:" + incident_id,
                queue="ops",
            )
