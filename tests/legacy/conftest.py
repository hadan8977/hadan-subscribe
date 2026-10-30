"""Independent source-review fixtures: no external credentials or legacy state."""

import pytest

from hadan.config import Settings
from hadan.service import Service


@pytest.fixture
def source_service(tmp_path, monkeypatch):
    for key in (
        "HADAN_SCHEDULE",
        "MARKET_EXTRA_HOLIDAYS",
        "QUANTGT_ENABLED",
        "OFFICIAL_MAIL_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        data_dir=tmp_path / "state",
        base_url="http://testserver",
        production=False,
        secret="source-review-test-secret-" * 3,
        admin_emails=("operator@example.test",),
        mail_backend="file",
        mail_from="Hadan <notify@example.test>",
        smtp_host="",
        smtp_user="",
        smtp_password="",
        stripe_key="",
        stripe_webhook_secret="",
        deepseek_key="",
        ops_ai=False,
    )
    service = Service(settings)
    service.seed()
    return service
