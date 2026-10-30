import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from hadan.security import decrypt, redact
from hadan.service import Problem


def test_allowlist_blocks_new_otps_existing_sessions_and_agent_tokens(harness, bind_agent):
    app, service, login = harness
    reader, _ = login("reader@example.com")
    binding, auth = bind_agent(reader)
    service.settings.allowed_emails = ("owner@example.com",)
    with TestClient(app) as public:
        assert public.post("/api/auth/request", json={"email": "reader@example.com"}).status_code == 403
    assert reader.get("/api/me").status_code == 403
    assert reader.get("/api/events", headers=auth).status_code == 403
    owner, _ = login("owner@example.com")
    assert owner.get("/api/admin/status").status_code == 200


def test_pending_otp_cannot_bypass_a_new_allowlist(harness):
    _, service, _ = harness
    service.request_code("reader@example.com", "192.0.2.10")
    job = json.loads(service.db.one("SELECT payload FROM jobs WHERE kind='login_mail'")["payload"])
    code = decrypt(service.settings.secret, job["encrypted_code"])
    service.settings.allowed_emails = ("owner@example.com",)
    with pytest.raises(Problem) as failure:
        service.verify_code("reader@example.com", code, "192.0.2.10")
    assert failure.value.status == 403
    assert not service.db.all("SELECT * FROM sessions")


def test_unlabelled_browser_password_is_redacted_before_incident_storage(harness, monkeypatch):
    from hadan.worker import Worker
    _, service, _ = harness
    secret = 'source-credential-"quoted"-only-for-test'
    monkeypatch.setenv("QUANTGT_PASSWORD", secret)
    message = 'locator.fill(' + json.dumps(secret) + ') failed'
    assert secret not in redact(message)
    assert json.dumps(secret)[1:-1] not in redact(message)
    Worker(service).incident("source:picks", message)
    stored = json.dumps(service.db.all("SELECT * FROM incidents"))
    assert "source-credential" not in stored


def test_source_browser_rejects_root_before_opening_network(monkeypatch):
    from hadan.sources.jobs import browser_session
    monkeypatch.setenv("QUANTGT_EMAIL", "fixture@example.com")
    monkeypatch.setenv("QUANTGT_PASSWORD", "fixture-password")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(ValueError, match="unprivileged"):
        with browser_session():
            pytest.fail("A root source browser must not start")


def test_five_minute_inbox_poll_is_persistent_and_deduplicated(harness, monkeypatch):
    from hadan.schedule import tick
    _, service, _ = harness
    monkeypatch.setenv("OFFICIAL_MAIL_ENABLED", "1")
    monkeypatch.setenv("OFFICIAL_MAIL_POLL_SECONDS", "300")
    moment = datetime(2026, 10, 27, 1, 3, tzinfo=timezone.utc)
    tick(service, moment)
    tick(service, moment)
    rows = service.db.all("SELECT * FROM jobs WHERE dedupe LIKE 'inbox-poll:%'")
    assert len(rows) == 1 and rows[0]["queue"] == "source"


def test_source_and_model_work_share_one_slot_but_delivery_stays_available(harness):
    from hadan.db import enqueue
    from hadan.worker import Worker
    _, service, _ = harness
    with service.db.transaction() as db:
        enqueue(db, "source:picks", {}, "heavy-source", queue="source")
        enqueue(db, "diagnose", {}, "heavy-model", queue="ops")
        enqueue(db, "admin_summary", {}, "light-delivery", queue="main")
    source = Worker(service, queue="source").claim()
    assert source is not None
    assert Worker(service, queue="ops").claim() is None
    assert Worker(service, queue="main").claim() is not None
    assert service.db.one("SELECT attempts FROM jobs WHERE dedupe='heavy-model'")["attempts"] == 0
    service.db.execute("UPDATE jobs SET state='complete',lease_until=NULL WHERE id=?", (source["id"],))
    assert Worker(service, queue="ops").claim() is not None
