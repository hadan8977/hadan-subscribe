from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import os
import secrets
import socket
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
import base64


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token() -> str:
    return secrets.token_urlsafe(32)


def mac(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def encrypt(secret: str, value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key).encrypt(value.encode()).decode()


def decrypt(secret: str, value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key).decrypt(value.encode()).decode()


def email_address(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}",
        value,
    ):
        raise ValueError("请输入有效的邮箱地址")
    return value


def redact(value: object) -> str:
    text = str(value)
    # Browser/IMAP exceptions can echo a filled value without a "password=" label.
    for name in ("HADAN_SECRET", "SMTP_PASSWORD", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                 "XORPAY_APP_SECRET", "DEEPSEEK_API_KEY", "QUANTGT_PASSWORD", "IMAP_PASSWORD"):
        secret = os.getenv(name, "")
        if secret:
            for representation in (secret, json.dumps(secret)[1:-1]):
                text = text.replace(representation, "[redacted]")
    text = re.sub(
        r"""(?i)(["']?(?:password|api[_-]?key|token|secret|authorization)["']?\s*[:=]\s*)(["'])(.*?)\2""",
        r'\1"[redacted]"',
        text,
    )
    text = re.sub(
        r"(?i)(bearer\s+|api[_-]?key[=: ]+|password[=: ]+|token[=: ]+|secret[=: ]+)[^\s,;\"']+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    return text[:4000]


def webhook_target(url: str):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.fragment
        or p.query
        or p.port not in (None, 443)
    ):
        raise ValueError("Webhook 必须使用 HTTPS 443，且不能包含凭证、查询参数或片段")
    addresses = sorted(
        {x[4][0] for x in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)}
    )
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global
        or ipaddress.ip_address(ip).is_multicast
        or ipaddress.ip_address(ip).is_reserved
        or ipaddress.ip_address(ip).is_unspecified
        for ip in addresses
    ):
        raise ValueError("Webhook 必须指向公网地址")
    return p, addresses[0]


def action_token(secret: str, subscription_id: str) -> str:
    # Narrow unsubscribe-only capability; never grants login or content access.
    return subscription_id + "." + mac(secret, "unsubscribe:" + subscription_id)


def action_id(secret: str, value: str) -> str:
    identifier, _, signature = value.partition(".")
    if not re.fullmatch(r"[a-f0-9]{32}", identifier) or not hmac.compare_digest(
        mac(secret, "unsubscribe:" + identifier), signature
    ):
        raise ValueError("退订链接无效")
    return identifier
