"""Bulk operations and storage maintenance preserve consent and referenced content."""

from datetime import datetime
import hashlib
import os
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from hadan.cli import grant
from hadan.db import enqueue
from hadan.maintenance import bulk_members, cleanup


def test_bulk_preview_and_apply_report_invalid_duplicate_rows(source_service):
    operations = [
        {
            "email": "one@example.test",
            "until": "2030-01-09T00:00:00-05:00",
            "email_consent": True,
        },
        {"email": "not-an-address"},
        {"email": "ONE@example.test"},
    ]
    result = bulk_members(source_service, operations)
    assert not result["apply"]
    assert not source_service.db.all("SELECT * FROM users")
    assert len([row for row in result["results"] if row.get("error")]) == 2
    result = bulk_members(source_service, operations, apply=True)
    assert result["results"][0]["applied"]
    assert len(source_service.db.all("SELECT * FROM users")) == 1


def test_bulk_invalid_element_is_per_row_error_not_partial_batch_crash(source_service):
    result = bulk_members(
        source_service, [None, {"email": "reader@example.test"}], apply=True
    )
    assert result["results"][0]["error"]
    assert result["results"][1]["applied"]


def test_bulk_preview_checks_product_existence_before_claiming_row_is_valid(
    source_service,
):
    result = bulk_members(
        source_service, [{"email": "reader@example.test", "product": "does-not-exist"}]
    )
    assert result["results"][0].get("error"), (
        "A reviewable dry-run must catch the same nonexistent product as apply"
    )
    assert not source_service.db.all("SELECT * FROM users")


def test_bulk_expiry_correction_does_not_reactivate_cancelled_entitlement(
    source_service,
):
    granted = grant(source_service, "ended@example.test", "quantgt", email_consent=True)
    source_service.db.execute(
        "UPDATE subscriptions SET status='ended',email_enabled=0,agent_enabled=0 WHERE id=?",
        (granted["subscription_id"],),
    )
    bulk_members(
        source_service,
        [
            {
                "email": "ended@example.test",
                "operation": "set_expiry",
                "until": "2030-04-09T00:00:00-04:00",
            }
        ],
        apply=True,
    )
    current = source_service.db.one(
        "SELECT * FROM subscriptions WHERE id=?", (granted["subscription_id"],)
    )
    assert current["status"] == "ended", (
        "Date correction must not imply a new entitlement grant"
    )
    assert current["email_enabled"] == current["agent_enabled"] == 0


def test_bulk_renewal_uses_anchor_and_preserves_delivery_opt_out(source_service):
    granted = grant(
        source_service, "renew@example.test", "quantgt", "2030-01-09T00:00:00-05:00"
    )
    result = bulk_members(
        source_service,
        [
            {
                "email": "renew@example.test",
                "operation": "extend",
                "months": 2,
                "email_consent": True,
            }
        ],
        apply=True,
    )
    assert result["results"][0]["applied"]
    current = source_service.db.one(
        "SELECT * FROM subscriptions WHERE id=?", (granted["subscription_id"],)
    )
    assert (
        datetime.fromtimestamp(current["expires_at"], ZoneInfo("America/New_York"))
        .date()
        .isoformat()
        == "2030-03-09"
    )
    assert current["email_enabled"] == 0


def age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_cleanup_preview_preserves_all_and_apply_preserves_replay_identities(
    source_service,
):
    service = source_service
    content = service.publish(
        "quantgt",
        "retention",
        "Report",
        "Body",
        files=[{"name": "report.txt", "data": b"private content"}],
    )
    ref = service.db.one("SELECT storage_key FROM attachments")["storage_key"]
    private = service.settings.data_dir / "files" / ref
    age(private, 200)
    orphan = service.settings.data_dir / "files" / ("a" * 64)
    orphan.write_bytes(b"unused")
    age(orphan, 200)
    mail = service.settings.data_dir / "mail/fixture.eml"
    mail.write_bytes(b"local test mail")
    age(mail, 200)
    quarantine = service.settings.data_dir / "source/quarantine-fixture.eml"
    quarantine.write_bytes(b"untrusted mail")
    age(quarantine, 8)
    with service.db.transaction() as db:
        for kind, state in (
            ("source:picks", "complete"),
            ("deliver", "complete"),
            ("account_mail", "complete"),
            ("source:picks", "dead"),
        ):
            identity = kind + ":" + state
            jid = enqueue(db, kind, {}, identity)
            db.execute(
                "UPDATE jobs SET state=?,created_at=? WHERE id=?",
                (state, time.time() - 200 * 86400, jid),
            )
    preview = cleanup(service)
    assert preview["old_jobs"] == 1
    assert all(path.exists() for path in (private, orphan, mail, quarantine))
    result = cleanup(service, apply=True)
    assert result["content_versions_deleted"] == 0
    assert private.exists()
    assert not any(path.exists() for path in (orphan, mail, quarantine))
    assert service.db.one("SELECT id FROM contents WHERE id=?", (content["id"],))
    remaining = {
        (row["kind"], row["state"])
        for row in service.db.all("SELECT kind,state FROM jobs")
    }
    assert remaining == {
        ("deliver", "complete"),
        ("account_mail", "complete"),
        ("source:picks", "dead"),
    }


def test_cleanup_cannot_follow_symlink_to_private_external_file(
    source_service, tmp_path
):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep me")
    age(outside, 200)
    link = source_service.settings.data_dir / "files" / ("b" * 64)
    link.symlink_to(outside)
    cleanup(source_service, apply=True)
    assert outside.read_bytes() == b"keep me"


def test_publish_interleaved_with_cleanup_keeps_the_new_attachment(
    source_service, monkeypatch
):
    service = source_service
    payload = b"previously orphaned blob now used by a real publication"
    sha = hashlib.sha256(payload).hexdigest()
    path = service.settings.data_dir / "files" / sha
    path.write_bytes(payload)
    age(path, 200)
    original_all = service.db.all
    finished = threading.Event()
    errors = []
    threads = []

    def publish():
        try:
            service.publish(
                "quantgt",
                "concurrent-retention",
                "New report",
                "Body",
                files=[{"name": "new.txt", "data": payload}],
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def interleave(sql, params=()):
        result = original_all(sql, params)
        if sql == "SELECT storage_key FROM attachments" and not threads:
            thread = threading.Thread(target=publish, daemon=True)
            threads.append(thread)
            thread.start()
            # If GC and publication coordinate via a shared lock, publication waits
            # until cleanup releases it; otherwise it exposes the stale-ref race.
            finished.wait(timeout=1)
        return result

    monkeypatch.setattr(service.db, "all", interleave)
    cleanup(service, apply=True)
    for thread in threads:
        thread.join(timeout=5)
    assert finished.is_set() and not errors
    assert service.db.one(
        "SELECT storage_key FROM attachments WHERE storage_key=?", (sha,)
    )
    assert path.is_file() and path.read_bytes() == payload, (
        "GC must not delete a blob newly referenced by a committed publication"
    )


def test_retention_rejects_accidental_short_window(source_service):
    with pytest.raises(ValueError, match="30"):
        cleanup(source_service, apply=True, days=1)
