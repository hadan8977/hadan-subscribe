import base64
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hadan.backup import backup, restore
from hadan.cli import grant
from hadan.config import Settings
from hadan.migration import import_archive
from hadan.service import Service


def test_archive_preview_import_library_and_migrate(tmp_path):
    service = Service(
        Settings(data_dir=tmp_path / "old", secret="test-private-key-" * 5)
    )
    service.seed()
    grant(service, "reader@example.test", "quantgt", email_consent=True)
    uid = service.db.one("SELECT id FROM users")["id"]
    raw = tmp_path / "archive"
    raw.mkdir()
    content = b"synthetic historical attachment"
    (raw / "report.txt").write_bytes(content)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "root": str(raw),
                "items": [
                    {
                        "id": "old-run-1",
                        "title": "Synthetic old report",
                        "body": "Historical body",
                        "files": [
                            {
                                "path": "report.txt",
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert not import_archive(service, manifest)["apply"]
    assert not service.db.all("SELECT * FROM contents")
    imported = import_archive(service, manifest, apply=True)
    again = import_archive(service, manifest, apply=True)
    assert again["items"][0]["unchanged"]
    assert not service.db.all("SELECT * FROM deliveries")
    cid = imported["items"][0]["id"]
    record = service.content(uid, cid)
    attachment = record["attachments"][0]["id"]
    assert service.attachment(uid, attachment)[1].read_bytes() == content
    key = tmp_path / "backup.key"
    key.write_bytes(base64.urlsafe_b64encode(b"z" * 32))
    encrypted = tmp_path / "move.enc"
    backup(service, encrypted, key)
    target = tmp_path / "new-machine"
    restore(encrypted, target, key)
    migrated = Service(Settings(data_dir=target, secret="new-private-key-" * 5))
    assert migrated.content(uid, cid)["body"] == "Historical body"
    assert migrated.attachment(uid, attachment)[1].read_bytes() == content
    assert (
        migrated.db.one("SELECT value FROM meta WHERE key='delivery_paused'")["value"]
        == "1"
    )


def test_archive_rejects_wrong_hash_and_escape_without_publishing(tmp_path):
    service = Service(Settings(data_dir=tmp_path / "data", secret="test-key-" * 10))
    service.seed()
    root = tmp_path / "raw"
    root.mkdir()
    (root / "test.txt").write_text("safe")
    manifest = tmp_path / "manifest.json"
    value = {
        "root": str(root),
        "items": [
            {
                "id": "bad",
                "title": "bad",
                "files": [{"path": "test.txt", "sha256": "0" * 64}],
            }
        ],
    }
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        import_archive(service, manifest, apply=True)
    value["items"][0]["files"][0]["path"] = "../outside.txt"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        import_archive(service, manifest, apply=True)
    assert not service.db.all("SELECT * FROM contents")


def test_atomic_release_switch_and_rollback(tmp_path):
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "release_switch", root / "scripts/switch_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("v1", "v2"):
        release = tmp_path / "releases" / name
        (release / "src/hadan").mkdir(parents=True)
        (release / "src/hadan/cli.py").write_text("# fixture")
        (release / "uv.lock").write_text("fixture")
    module.switch(tmp_path, tmp_path / "releases/v1")
    module.switch(tmp_path, tmp_path / "releases/v2")
    assert (tmp_path / "previous").resolve() == tmp_path / "releases/v1"
    module.switch(tmp_path, tmp_path / "previous")
    assert (tmp_path / "current").resolve() == tmp_path / "releases/v1"
    with pytest.raises(ValueError):
        module.switch(tmp_path, root)
