"""Explicit archive manifests preserve private files and never imply a historical send."""

import copy
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from hadan.cli import grant, import_members
from hadan.migration import import_archive, legacy_payments
from hadan.security import digest
from hadan.service import Problem
from hadan.web import create_app


@pytest.fixture
def archive_manifest(tmp_path):
    root = tmp_path / "controlled-export"
    root.mkdir()
    payload = b"Synthetic historical source report; no real subscription data."
    (root / "report.txt").write_bytes(payload)
    value = {
        "root": str(root),
        "items": [
            {
                "id": "fixture-run-2026-10-01",
                "product_id": "quantgt",
                "title": "Synthetic historical report · 2026-10-01",
                "body": "Imported test edition with explicit attachment manifest.",
                "files": [
                    {
                        "path": "report.txt",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ],
    }
    path = tmp_path / "archive-manifest.json"
    path.write_text(json.dumps(value))
    return path, value, payload


def test_archive_preview_has_no_writes_then_repeated_apply_is_noop(
    source_service, archive_manifest
):
    path, value, payload = archive_manifest
    grant(source_service, "reader@example.test", "quantgt", email_consent=True)
    preview = import_archive(source_service, path)
    assert not preview["apply"] and not preview["subscriber_mail_sent"]
    assert not source_service.db.all("SELECT * FROM contents")
    applied = import_archive(source_service, path, apply=True)
    repeated = import_archive(source_service, path, apply=True)
    assert applied["items"][0]["id"] == repeated["items"][0]["id"]
    assert repeated["items"][0]["unchanged"]
    assert len(source_service.db.all("SELECT * FROM contents")) == 1
    assert len(source_service.db.all("SELECT * FROM versions")) == 1
    assert not source_service.db.all("SELECT * FROM deliveries")
    assert not source_service.db.all(
        "SELECT * FROM jobs WHERE kind IN ('deliver','account_mail')"
    )


def test_bad_later_checksum_prevents_any_earlier_item_commit(
    source_service, archive_manifest
):
    path, value, payload = archive_manifest
    second = copy.deepcopy(value["items"][0])
    second["id"] = "fixture-run-2026-10-02"
    second["files"][0]["sha256"] = "0" * 64
    value["items"].append(second)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="checksum"):
        import_archive(source_service, path, apply=True)
    assert not source_service.db.all("SELECT * FROM contents")


@pytest.mark.parametrize(
    "bad_field", ["nontext_title", "long_title", "long_body", "too_many_files"]
)
def test_all_publication_limits_are_prevalidated_before_any_import(
    source_service, archive_manifest, bad_field
):
    path, value, payload = archive_manifest
    second = copy.deepcopy(value["items"][0])
    second["id"] = "invalid-later-edition"
    if bad_field == "nontext_title":
        second["title"] = 42
    elif bad_field == "long_title":
        second["title"] = "x" * 251
    elif bad_field == "long_body":
        second["body"] = "x" * 500001
    else:
        second["files"] = second["files"] * 13
    value["items"].append(second)
    path.write_text(json.dumps(value))
    with pytest.raises((ValueError, Problem)):
        import_archive(source_service, path, apply=True)
    assert not source_service.db.all("SELECT * FROM contents"), (
        "Later invalid metadata must fail during whole-manifest prevalidation"
    )


def test_duplicate_source_identity_within_manifest_is_rejected(
    source_service, archive_manifest
):
    path, value, payload = archive_manifest
    second = copy.deepcopy(value["items"][0])
    second["title"] = "Conflicting content under the same source-run ID"
    value["items"].append(second)
    path.write_text(json.dumps(value))
    with pytest.raises((ValueError, Problem)):
        import_archive(source_service, path, apply=True)
    assert not source_service.db.all("SELECT * FROM contents")


@pytest.mark.parametrize("escape", ["parent", "symlink"])
def test_archive_root_cannot_escape_to_external_file(
    source_service, archive_manifest, tmp_path, escape
):
    path, value, payload = archive_manifest
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    if escape == "parent":
        relative = "../outside.txt"
    else:
        root = tmp_path / "controlled-export"
        (root / "outside-link.txt").symlink_to(outside)
        relative = "outside-link.txt"
    value["items"][0]["files"] = [
        {"path": relative, "sha256": hashlib.sha256(b"outside").hexdigest()}
    ]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="outside"):
        import_archive(source_service, path, apply=True)
    assert not source_service.db.all("SELECT * FROM contents")


def test_oversize_archive_file_is_rejected_before_content_is_loaded(
    source_service, archive_manifest
):
    path, value, payload = archive_manifest
    source_service.settings.max_attachment_bytes = len(payload) - 1
    with pytest.raises(ValueError, match="large"):
        import_archive(source_service, path, apply=True)
    assert not source_service.db.all("SELECT * FROM contents")


def test_imported_baseline_is_visible_in_private_library_without_event_fanout(
    source_service, archive_manifest
):
    path, value, payload = archive_manifest
    grant(source_service, "reader@example.test", "quantgt")
    user = source_service.db.one(
        "SELECT * FROM users WHERE email='reader@example.test'"
    )
    session = "archive-test-session"
    source_service.db.execute(
        "INSERT INTO sessions VALUES(?,?,?)",
        (digest(session), user["id"], time.time() + 60),
    )
    imported = import_archive(source_service, path, apply=True)["items"][0]
    app = create_app(source_service.settings)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/api/library").status_code == 401
        client.cookies.set("hadan_session", session)
        library = client.get("/api/library")
        assert library.status_code == 200
        assert [item["id"] for item in library.json()["contents"]] == [imported["id"]]
        report = client.get("/api/content/" + imported["id"])
        assert report.status_code == 200
        attachment = report.json()["attachments"][0]["id"]
        assert client.get("/api/files/" + attachment).content == payload
    assert not source_service.events(user["id"])


def test_original_payment_history_units_remain_separate_from_new_ledger(
    source_service, tmp_path
):
    original_payment = {
        "amount": 30.5,
        "currency": "CNY",
        "channel": "wechat",
        "paid_at": "2026-09-01T12:00:00-04:00",
        "ref": "fixture-original-ref",
    }
    member = {
        "email": "history@example.test",
        "status": "active",
        "expires_at": "2030-01-09T00:00:00-05:00",
        "joined_at": "2026-08-31T12:00:00-04:00",
        "note": "Fixture migration history",
        "history": [
            {
                "at": "2026-09-01T12:00:00-04:00",
                "action": "extend",
                "payment": original_payment,
            },
            {"action": "note"},
        ],
    }
    path = tmp_path / "members.json"
    path.write_text(json.dumps({"members": [member]}))
    import_members(source_service, path, apply=True)
    import_members(source_service, path, apply=True)
    legacy = legacy_payments(source_service)
    assert len(legacy) == 1
    assert {key: legacy[0][key] for key in original_payment} == original_payment
    assert "original legacy units" in legacy[0]["units"]
    assert not source_service.db.all("SELECT * FROM payments"), (
        "Do not silently multiply or mix original units into minor-unit ledger"
    )
    stored = source_service.db.one(
        "SELECT value FROM source_state WHERE key='legacy-member:history@example.test'"
    )
    assert json.loads(stored["value"]) == member
