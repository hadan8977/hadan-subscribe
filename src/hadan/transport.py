from __future__ import annotations

import http.client
import json
import os
import re
import smtplib
import socket
import ssl
import time
import threading
import email.policy
from email.message import EmailMessage
from email.utils import formatdate, parseaddr

from .security import mac, webhook_target


class DeliveryError(Exception):
    def __init__(self, message, *, retry=True, uncertain=False):
        super().__init__(message)
        self.retry, self.uncertain = retry, uncertain


class MailTooLarge(DeliveryError):
    """Raised before any external dispatch, so a private-link summary is safe."""


def provider_message_id(response, fallback):
    """Return the SES message ID from the SMTP DATA reply, else our Message-ID.

    SES answers a successful DATA with `250 Ok <provider-id>`. That ID is the
    value carried in SES event notifications, so it is the useful delivery
    correlation key. Never treat it as idempotency.
    """
    try:
        text = response.decode("ascii", "ignore") if isinstance(response, bytes) else str(response)
    except Exception:
        return fallback
    text = text.strip()
    if text.lower().startswith("ok "):
        value = text[3:].strip()
        if value and len(value) <= 512 and not any(c.isspace() for c in value):
            return value
    return fallback


class Transport:
    def __init__(self, settings):
        self.settings = settings

    def mail(
        self,
        recipient,
        subject,
        body,
        *,
        identity,
        html=None,
        attachments=(),
        unsubscribe=None,
        headers=None,
    ):
        cfg = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = (
            cfg.mail_from,
            recipient,
            subject,
        )
        message["Date"] = formatdate(localtime=False)
        if cfg.mail_reply_to:
            message["Reply-To"] = cfg.mail_reply_to
        message["Message-ID"] = (
            f"<{identity}@{parseaddr(cfg.mail_from)[1].split('@')[-1]}>"
        )
        if unsubscribe:
            message["List-Unsubscribe"] = f"<{unsubscribe}>"
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        for name, value in (headers or {}).items():
            if not isinstance(name, str) or not re.fullmatch(r"X-Quant-[A-Za-z0-9-]+", name):
                raise ValueError("Invalid extra mail header")
            value = str(value)
            if not value:
                continue
            if "\r" in value or "\n" in value:
                raise ValueError("Invalid extra mail header value")
            message[name] = value
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        for a, path in attachments:
            maintype, subtype = a["mime"].split("/", 1)
            message.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=a["name"],
            )
        raw = message.as_bytes(policy=email.policy.SMTP)
        if len(raw) > 24 * 1024 * 1024:
            raise MailTooLarge("邮件超过附件大小限制，请选择摘要交付", retry=False)
        if cfg.mail_backend == "file":
            path = cfg.data_dir / "mail" / (identity + ".eml")
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as out:
                os.chmod(tmp, 0o600)
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
            return message["Message-ID"]
        if cfg.mail_backend != "smtp":
            raise DeliveryError("未配置邮件通道", retry=False)
        stage = "connect"
        client = None

        def abort():
            sock = getattr(client, "sock", None)
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        deadline = threading.Timer(45, abort)
        deadline.daemon = True
        deadline.start()
        try:
            context = ssl.create_default_context()
            if cfg.smtp_tls == "implicit":
                client = smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, timeout=20, context=context
                )
            elif cfg.smtp_tls == "starttls":
                client = smtplib.SMTP(timeout=20)
                client.connect(cfg.smtp_host, cfg.smtp_port)
                client.ehlo()
                client.starttls(context=context)
            else:
                raise DeliveryError("SMTP 必须开启 TLS", retry=False)
            client.ehlo()
            if cfg.smtp_user:
                client.login(cfg.smtp_user, cfg.smtp_password)
            code, _ = client.mail(parseaddr(cfg.mail_from)[1])
            if code >= 400:
                raise DeliveryError(f"SMTP MAIL rejected: {code}", retry=code < 500)
            code, _ = client.rcpt(recipient)
            if code >= 400:
                raise DeliveryError(f"SMTP RCPT rejected: {code}", retry=code < 500)
            stage = "data"
            code, response = client.data(raw)
            if code >= 400:
                detail = (
                    response.decode("utf-8", "replace")[:300].strip()
                    if isinstance(response, bytes)
                    else str(response)[:300]
                )
                raise DeliveryError(
                    f"SMTP DATA rejected: {code} {detail}".strip(),
                    retry=code < 500,
                )
            return provider_message_id(response, message["Message-ID"])
        except DeliveryError:
            raise
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError(
                f"SMTP rejected: {exc.smtp_code}", retry=exc.smtp_code < 500
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # The remote may already have accepted DATA. A stable Message-ID is not provider idempotency.
            raise DeliveryError(
                type(exc).__name__, retry=stage != "data", uncertain=stage == "data"
            ) from exc
        finally:
            deadline.cancel()
            if client:
                try:
                    client.close()
                except OSError:
                    pass

    def webhook(self, url, payload, secret, event_id):
        parsed, address = webhook_target(url)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        signature = mac(secret, stamp + "." + raw.decode())
        # Pin the validated DNS result while retaining hostname SNI and certificate verification.
        connection = http.client.HTTPSConnection(
            parsed.hostname, 443, timeout=15, context=ssl.create_default_context()
        )
        sock = socket.create_connection((address, 443), timeout=15)

        def abort():
            try:
                (connection.sock or sock).shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(20, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=parsed.hostname, do_handshake_on_connect=False
            )
            connection.sock.do_handshake()
            connection.request(
                "POST",
                parsed.path or "/",
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Hadan-Event": event_id,
                    "X-Hadan-Timestamp": stamp,
                    "X-Hadan-Signature": "v1=" + signature,
                    "User-Agent": "Hadan-Subscriptions/0.1",
                },
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise DeliveryError(
                    f"Webhook HTTP {response.status}",
                    retry=response.status == 429 or response.status >= 500,
                )
            return str(response.status)
        finally:
            timer.cancel()
            connection.close()
            sock.close()
