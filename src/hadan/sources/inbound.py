"""Bounded inbound selection: original signatures and explicit curator authority.

MIME envelopes are sliced as bytes. An encapsulated original is never rebuilt
with email.as_bytes() before DKIM verification.
"""
from __future__ import annotations

import base64
import hashlib
import quopri
import re
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from typing import Callable

MAX_BYTES = 20 * 1024 * 1024
MAX_HEADERS = 128 * 1024
MAX_PARTS = 128
MAX_DEPTH = 6
MAX_ORIGINALS = 8
MAX_SIGNATURES = 8


class InboundRejected(ValueError):
    pass


@dataclass(frozen=True)
class SignatureProof:
    domain: str
    index: int
    signed_headers: frozenset[str]
    identity: str


@dataclass(frozen=True)
class SelectedMessage:
    raw: bytes
    sender: str
    mode: str
    identity: str
    proof: SignatureProof | None


class Budget:
    def __init__(self):
        self.parts = 0
        self.decoded = 0
        self.originals = 0

    def visit(self, depth):
        self.parts += 1
        if depth > MAX_DEPTH or self.parts > MAX_PARTS:
            raise InboundRejected("Inbound MIME nesting or part count exceeds limit")

    def decode(self, size):
        self.decoded += size
        if self.decoded > MAX_BYTES:
            raise InboundRejected("Decoded inbound messages exceed size limit")


def entity(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise InboundRejected("Inbound message too large")
    marker = re.search(br"\r?\n\r?\n", raw)
    if marker is None or marker.start() > MAX_HEADERS:
        raise InboundRejected("Inbound headers missing or too large")
    headers = BytesHeaderParser(policy=policy.default).parsebytes(raw[:marker.end()])
    return headers, raw[marker.end():]


def unambiguous(message):
    for name in ("From", "To", "Cc", "Subject", "Message-ID", "Content-Type", "Content-Transfer-Encoding", "Content-Disposition", "MIME-Version"):
        if len(message.get_all(name, [])) > 1:
            raise InboundRejected("Ambiguous duplicate message headers")


def addresses(message, name):
    values = message.get_all(name, [])
    if len(values) != 1 or getattr(values[0], "defects", ()):
        return ()
    try:
        parsed = values[0].addresses
    except (AttributeError, ValueError):
        return ()
    result = tuple(a.addr_spec.lower() for a in parsed)
    if any(not a.username or not a.domain for a in parsed):
        return ()
    return result


def sender_of(message):
    values = addresses(message, "From")
    return values[0] if len(values) == 1 else ""


def signature_fields(value):
    fields = {}
    for component in str(value).split(";"):
        if not component.strip():
            continue
        key, sep, item = component.partition("=")
        key = key.strip().lower()
        if not sep or key in fields:
            return None
        fields[key] = item.strip()
    return fields


def verify_signature(raw, sender, *, required_headers=frozenset({"from"})):
    """Return the aligned, full-body signature that proves all required headers."""
    try:
        message, _ = entity(raw)
        unambiguous(message)
        if not sender or sender_of(message) != sender.lower():
            return None
        if any(len(message.get_all(name, [])) != 1 for name in required_headers):
            return None
        signatures = message.get_all("DKIM-Signature", [])
        if not signatures or len(signatures) > MAX_SIGNATURES:
            return None
        import dkim

        deadline = time.monotonic() + 20
        for index, signature in enumerate(signatures):
            fields = signature_fields(signature)
            if not fields or "l" in fields:
                continue
            domain = fields.get("d", "").lower()
            signed = frozenset(x.strip().lower() for x in fields.get("h", "").split(":"))
            if domain != sender.rsplit("@", 1)[-1].lower() or not required_headers <= signed:
                continue
            if time.monotonic() >= deadline:
                return None
            try:
                valid = (dkim.verify(raw, timeout=8) if index == 0 else dkim.DKIM(raw, timeout=8).verify(idx=index))
            except Exception:
                valid = False
            if valid:
                # Signature identity survives changes to unsigned outer routing headers.
                identity = hashlib.sha256((domain + ":" + re.sub(r"\s+", "", fields.get("b", ""))).encode()).hexdigest()
                return SignatureProof(domain, index, signed, identity)
    except (ValueError, UnicodeError, ImportError):
        return None
    return None


def multipart_parts(headers, body):
    boundary = headers.get_boundary()
    if not boundary or len(boundary) > 200:
        raise InboundRejected("Invalid MIME boundary")
    try:
        encoded = boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InboundRejected("Invalid MIME boundary") from exc
    pattern = re.compile(br"(?m)^--" + re.escape(encoded) + br"(?P<end>--)?[ \t]*(?:\r?\n|$)")
    boundaries = []
    for match in pattern.finditer(body):
        boundaries.append(match)
        if len(boundaries) > MAX_PARTS + 1:
            raise InboundRejected("Excessive MIME boundaries")
    if not boundaries or not boundaries[-1].group("end"):
        raise InboundRejected("Malformed or excessive MIME boundaries")
    if any(b.group("end") for b in boundaries[:-1]):
        raise InboundRejected("MIME data after closing boundary")
    for left, right in zip(boundaries, boundaries[1:]):
        part = body[left.end():right.start()]
        # The CRLF immediately before a MIME delimiter belongs to the delimiter.
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        yield part


def decoded_body(headers, body):
    encoding = str(headers.get("Content-Transfer-Encoding", "7bit")).lower().strip()
    try:
        if encoding == "base64":
            return base64.b64decode(re.sub(br"\s+", b"", body), validate=True)
        if encoding == "quoted-printable":
            return quopri.decodestring(body)
        if encoding in ("7bit", "8bit", "binary", ""):
            return body
    except (ValueError, TypeError) as exc:
        raise InboundRejected("Invalid encoded attachment") from exc
    raise InboundRejected("Unsupported MIME transfer encoding")


def attached_originals(raw, budget, depth=0):
    budget.visit(depth)
    headers, body = entity(raw)
    unambiguous(headers)
    ctype = headers.get_content_type()
    if ctype.startswith("multipart/"):
        for part in multipart_parts(headers, body):
            yield from attached_originals(part, budget, depth + 1)
    elif ctype == "message/rfc822" or (headers.get_filename() or "").lower().endswith(".eml"):
        original = decoded_body(headers, body)
        budget.decode(len(original))
        budget.originals += 1
        if budget.originals > MAX_ORIGINALS:
            raise InboundRejected("Too many attached original messages")
        yield original


def stable_identity(raw, message, sender, proof):
    message_id = str(message.get("Message-ID", "")).strip()
    if isinstance(proof, SignatureProof):
        if message_id and "message-id" in proof.signed_headers:
            return hashlib.sha256((sender + ":" + message_id).encode()).hexdigest()
        return proof.identity
    # Unit-test verifier seams may return bool; production returns a proof.
    return hashlib.sha256(raw).hexdigest()


def select_messages(raw, official_domains, curators=(), inbound_address="", *, original_verifier: Callable | None = None):
    official_domains = {x.strip().lower() for x in official_domains if x.strip()}
    curators = {x.strip().lower() for x in curators if x.strip()}
    inbound_address = inbound_address.strip().lower()
    verifier = original_verifier or verify_signature
    budget = Budget()
    selected = []
    seen = set()

    def original(candidate, depth):
        if depth > MAX_DEPTH:
            raise InboundRejected("Forwarded-message nesting exceeds limit")
        message, _ = entity(candidate)
        unambiguous(message)
        sender = sender_of(message)
        if sender and sender.rsplit("@", 1)[-1] in official_domains:
            proof = verifier(candidate, sender)
            if not proof:
                raise InboundRejected("Official sender DKIM verification failed")
            identity = stable_identity(candidate, message, sender, proof)
            if identity not in seen:
                selected.append(SelectedMessage(candidate, sender, "official", identity, proof if isinstance(proof, SignatureProof) else None))
                seen.add(identity)
            return True
        nested = list(attached_originals(candidate, budget, depth))
        if not nested:
            return False
        for inner in nested:
            if not original(inner, depth + 1):
                raise InboundRejected("Attached original has no verified official sender")
        return True

    if original(raw, 0):
        return selected
    message, _ = entity(raw)
    unambiguous(message)
    sender = sender_of(message)
    if sender not in curators:
        return []
    if not inbound_address or inbound_address not in addresses(message, "To"):
        raise InboundRejected("Curated forwarding requires the explicit inbox alias in To")
    proof = verify_signature(raw, sender, required_headers=frozenset({"from", "to", "subject"}))
    if not proof:
        raise InboundRejected("Curated forwarding requires signed From, To, Subject and full body")
    return [SelectedMessage(raw, sender, "curated", stable_identity(raw, message, sender, proof), proof)]


def body_and_files(raw):
    """Read only the selected authenticated message, never its enclosing wrapper."""
    budget = Budget()
    texts, html, files = [], [], []

    def walk(part, depth=0):
        budget.visit(depth)
        headers, body = entity(part)
        unambiguous(headers)
        ctype = headers.get_content_type()
        if ctype.startswith("multipart/"):
            for child in multipart_parts(headers, body):
                walk(child, depth + 1)
            return
        data = decoded_body(headers, body)
        budget.decode(len(data))
        filename = headers.get_filename()
        if filename or ctype == "message/rfc822":
            if len(files) >= 12:
                raise InboundRejected("Too many inbound attachments")
            files.append({"name": filename or "attached-message.eml", "data": data})
        elif ctype in ("text/plain", "text/html"):
            try:
                text = data.decode(headers.get_content_charset() or "utf-8", errors="replace")
            except LookupError as exc:
                raise InboundRejected("Unsupported message charset") from exc
            (texts if ctype == "text/plain" else html).append(text)

    walk(raw)
    if not texts:
        import bleach
        texts = [bleach.clean(value, tags=[], strip=True) for value in html]
    if not texts:
        raise InboundRejected("Inbound message has no readable body")
    body = "\n\n".join(texts)
    if len(body) > 490000:
        raise InboundRejected("Inbound body exceeds publication limit")
    return body, files
