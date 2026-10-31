from __future__ import annotations

import os
import json
import re
from email.utils import parseaddr
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("HADAN_DATA_DIR", "var")).resolve()
    )
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "HADAN_BASE_URL", "http://127.0.0.1:8080"
        ).rstrip("/")
    )
    production: bool = field(
        default_factory=lambda: os.getenv("HADAN_ENV", "development") == "production"
    )
    secret: str = field(default_factory=lambda: os.getenv("HADAN_SECRET", ""))
    admin_emails: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip().lower()
            for x in os.getenv("HADAN_ADMIN_EMAILS", "").split(",")
            if x.strip()
        )
    )
    allowed_emails: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip().lower() for x in os.getenv("HADAN_ALLOWED_EMAILS", "").split(",") if x.strip()
    ))
    mail_backend: str = field(default_factory=lambda: os.getenv("MAIL_BACKEND", "file"))
    mail_from: str = field(
        default_factory=lambda: os.getenv("MAIL_FROM", "Quant <notify@example.invalid>")
    )
    mail_sender: str = field(
        default_factory=lambda: os.getenv("MAIL_SENDER", "").strip()
    )
    mail_reply_to: str = field(default_factory=lambda: os.getenv("MAIL_REPLY_TO", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_tls: str = field(default_factory=lambda: os.getenv("SMTP_TLS", "starttls"))
    ses_feedback_enabled: bool = field(
        default_factory=lambda: os.getenv("SES_FEEDBACK_ENABLED", "0") == "1"
    )
    ses_sns_topic_arn: str = field(
        default_factory=lambda: os.getenv("SES_SNS_TOPIC_ARN", "").strip()
    )
    directmail_feedback_enabled: bool = field(
        default_factory=lambda: os.getenv("DIRECTMAIL_FEEDBACK_ENABLED", "0") == "1"
    )
    directmail_webhook_token: str = field(
        default_factory=lambda: os.getenv("DIRECTMAIL_WEBHOOK_TOKEN", "").strip()
    )
    # The web and source processes do not send mail. Only the main worker and
    # the explicit doctor check require SMTP credentials in production.
    smtp_required: bool = field(default=True, compare=False)
    stripe_key: str = field(default_factory=lambda: os.getenv("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(
        default_factory=lambda: os.getenv("STRIPE_WEBHOOK_SECRET", "")
    )
    xorpay_enabled: bool = field(default_factory=lambda: os.getenv("XORPAY_ENABLED", "0") == "1")
    xorpay_aid: str = field(default_factory=lambda: os.getenv("XORPAY_APP_ID", ""))
    xorpay_secret: str = field(default_factory=lambda: os.getenv("XORPAY_APP_SECRET", ""))
    xiaodou_enabled: bool = field(default_factory=lambda: os.getenv("XIAODOU_ENABLED", "0") == "1")
    xiaodou_base_url: str = field(default_factory=lambda: os.getenv("XIAODOU_BASE_URL", "").rstrip("/"))
    xiaodou_app_id: str = field(default_factory=lambda: os.getenv("XIAODOU_APP_ID", ""))
    xiaodou_private_key_file: str = field(default_factory=lambda: os.getenv("XIAODOU_PRIVATE_KEY_FILE", ""))
    xiaodou_cashier_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip().rstrip("/") for x in os.getenv("XIAODOU_CASHIER_ORIGINS", "").split(",") if x.strip()))
    # Enable each channel only after this merchant's channel/scope is confirmed.
    xiaodou_methods: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip() for x in os.getenv("XIAODOU_METHODS", "").split(",") if x.strip()))
    xiaodou_trade_scope: str = field(default_factory=lambda: os.getenv("XIAODOU_TRADE_SCOPE", "CARD_VIRTUAL"))
    xianyu_url: str = field(default_factory=lambda: os.getenv("XIANYU_ITEM_URL", ""))
    payment_plans: list = field(default_factory=lambda: json.loads(os.getenv("QUANT_PLANS_JSON", "[]")))
    deepseek_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_model: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    )
    ops_ai: bool = field(
        default_factory=lambda: os.getenv("OPS_AI_ENABLED", "0") == "1"
    )
    max_attachment_bytes: int = 16 * 1024 * 1024
    session_seconds: int = 7 * 86400
    lease_seconds: int = 300
    source_timeout: int = 240
    ops_timeout: int = 270
    max_attempts: int = 5

    def __post_init__(self):
        if not self.mail_sender:
            self.mail_sender = parseaddr(self.mail_from)[1]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hadan.sqlite3"

    def initialize(self) -> None:
        from secrets import token_urlsafe

        if self.production:
            errors = self.preflight(require_smtp=self.smtp_required)
            if errors:
                raise ValueError(
                    "Production configuration incomplete: " + "; ".join(errors)
                )
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.secret:
            path = self.data_dir / ".local-secret"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self.secret = path.read_text().strip()
            else:
                self.secret = token_urlsafe(48)
                with os.fdopen(fd, "w") as out:
                    out.write(self.secret)
        for name in ("files", "mail", "source", "ops"):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)

    def preflight(self, *, require_smtp: bool = True) -> list[str]:
        errors = []
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.query
            or url.path not in ("", "/")
        ):
            errors.append("HADAN_BASE_URL must be a public HTTPS origin")
        if len(self.secret) < 48:
            errors.append("HADAN_SECRET must contain at least 48 random characters")
        if not self.admin_emails:
            errors.append("HADAN_ADMIN_EMAILS is required")
        if require_smtp:
            if (
                self.mail_backend != "smtp"
                or not self.smtp_host
                or not self.smtp_user
                or not self.smtp_password
            ):
                errors.append(
                    "verified SMTP configuration is required (file mail is development only)"
                )
            if self.smtp_tls not in ("starttls", "implicit"):
                errors.append("SMTP_TLS must be starttls or implicit")
            if (
                "example.invalid" in self.mail_from
                or "\n" in self.mail_from
                or "\r" in self.mail_from
            ):
                errors.append("MAIL_FROM must be a verified sender address")
            if "\n" in self.mail_reply_to or "\r" in self.mail_reply_to:
                errors.append("MAIL_REPLY_TO must be a single mailbox")
            if (
                "\n" in self.mail_sender
                or "\r" in self.mail_sender
                or "@" not in self.mail_sender
            ):
                errors.append("MAIL_SENDER must be a single email address")
        if self.production and self.mail_backend == "smtp":
            if not (self.ses_feedback_enabled or self.directmail_feedback_enabled):
                errors.append(
                    "an email feedback channel (SES SNS or DirectMail EventBridge) must be enabled"
                )
        if self.ses_feedback_enabled and not self.ses_sns_topic_arn:
            errors.append("SES_SNS_TOPIC_ARN is required when SES feedback is enabled")
        if self.directmail_feedback_enabled and len(self.directmail_webhook_token) < 32:
            errors.append(
                "DIRECTMAIL_WEBHOOK_TOKEN must contain at least 32 characters when DirectMail feedback is enabled"
            )
        if self.ses_sns_topic_arn and not re.fullmatch(
            r"arn:aws:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}",
            self.ses_sns_topic_arn,
        ):
            errors.append("SES_SNS_TOPIC_ARN is not a valid SNS topic ARN")
        return errors
