"""Private offline Drive substitute: no OAuth credentials or remote calls."""

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hadan.backup import backup, restore
from hadan.health import check


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upload_backups.py"


@pytest.fixture
def uploader(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("drive_upload_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    spool, state, work = (tmp_path / name for name in ("spool", "state", "work"))
    for path in (spool, state, work):
        path.mkdir(mode=0o700)
    config = work / "rclone.conf"
    config.write_text("offline-placeholder-not-a-credential")
    config.chmod(0o600)
    remote, calls = {}, []

    def fake_rclone(arguments, cfg):
        assert cfg == config
        calls.append(arguments)
        if arguments[0] == "copyto":
            staged, target = Path(arguments[1]), arguments[2]
            assert staged.parent.parent == work
            assert not staged.is_symlink()
            assert staged.stat().st_mode & 0o777 == 0o600
            data = staged.read_bytes()
            if target in remote and remote[target] != data:
                raise RuntimeError("offline immutable conflict")
            remote[target] = data
            return ""
        assert arguments[0] == "lsjson"
        data = remote[arguments[1]]
        return json.dumps({"Size": len(data), "Hashes": {"md5": hashlib.md5(data).hexdigest()}})

    monkeypatch.setattr(module, "run_rclone", fake_rclone)
    return module, spool, config, state, work, remote, calls


def snapshot_name(timestamp=None):
    return time.strftime("%Y%m%dT%H%M%SZ.hadan.enc", time.gmtime(timestamp or time.time()))


def small_snapshot(module, path):
    path.write_bytes(module.MAGIC + b"offline-synthetic-encrypted-payload" * 2)
    path.chmod(0o640)


def test_only_encrypted_named_files_are_uploaded_and_status_contains_no_config(uploader):
    module, spool, config, state, work, remote, calls = uploader
    name = snapshot_name()
    small_snapshot(module, spool / name)
    (spool / "rclone.conf").write_text("THIS_FILE_MUST_NEVER_BE_UPLOADED")
    (spool / (name + ".partial")).write_text("unfinished")
    result = module.upload(spool, config, state, work)
    assert set(remote) == {"quant:" + name}
    assert result["latest"]["name"] == name
    recorded = (state / "last-success.json").read_text()
    assert "THIS_FILE" not in recorded and "rclone.conf" not in recorded
    assert (state / "last-success.json").stat().st_mode & 0o777 == 0o640
    assert not list(work.glob("upload-*"))


def test_snapshot_symlink_cannot_upload_uploader_configuration(uploader):
    module, spool, config, state, work, remote, calls = uploader
    (spool / snapshot_name()).symlink_to(config)
    with pytest.raises((ValueError, OSError)):
        module.upload(spool, config, state, work)
    assert remote == {} and calls == []
    assert not (state / "last-success.json").exists()


def test_fifo_is_rejected_before_blocking_open(uploader, tmp_path):
    _, spool, _, _, work, _, _ = uploader
    fifo = spool / snapshot_name()
    os.mkfifo(fifo, 0o600)
    code = """import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('u',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.copy_snapshot(Path(sys.argv[2]),Path(sys.argv[3]))
except (ValueError,OSError):
    sys.exit(0)
sys.exit(12)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPT), str(fifo), str(work / "rejected")],
            capture_output=True, timeout=2, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("FIFO blocks uploader before regular-file validation")
    assert result.returncode == 0
    assert not (work / "rejected").exists()


def test_checksum_failure_never_refreshes_success_status(uploader, monkeypatch):
    module, spool, config, state, work, remote, _ = uploader
    small_snapshot(module, spool / snapshot_name())
    previous = {"last_verified_at": 1, "verified_count": 1, "latest": {"name": "old"}}
    (state / "last-success.json").write_text(json.dumps(previous))
    actual = module.run_rclone

    def mismatch(arguments, cfg):
        result = actual(arguments, cfg)
        if arguments[0] == "lsjson":
            data = json.loads(result)
            data["Hashes"]["md5"] = "0" * 32
            return json.dumps(data)
        return result

    monkeypatch.setattr(module, "run_rclone", mismatch)
    with pytest.raises((ValueError, RuntimeError)):
        module.upload(spool, config, state, work)
    assert json.loads((state / "last-success.json").read_text()) == previous
    attempt = json.loads((state / "last-attempt.json").read_text())
    assert attempt["verified_count"] == 0
    assert attempt["failures"][0]["stage"] == "verify_remote"


def test_failed_attempt_is_redacted_preserves_success_and_reaches_health(
    harness, uploader, monkeypatch
):
    _, service, _ = harness
    module, spool, config, state, work, _, _ = uploader
    small_snapshot(module, spool / snapshot_name())
    previous = {"last_verified_at": time.time() - 1, "verified_count": 1, "latest": {"name": "previous", "snapshot_at": time.time() - 1}}
    (state / "last-success.json").write_text(json.dumps(previous))

    def failure(*_args):
        raise RuntimeError("SECRET_PROVIDER_RESPONSE_MUST_NOT_BE_RECORDED")

    monkeypatch.setattr(module, "run_rclone", failure)
    with pytest.raises(ValueError):
        module.upload(spool, config, state, work)
    assert json.loads((state / "last-success.json").read_text()) == previous
    attempt_text = (state / "last-attempt.json").read_text()
    assert "SECRET_PROVIDER" not in attempt_text
    assert json.loads(attempt_text)["failures"][0]["stage"] == "upload"
    monkeypatch.setenv("HADAN_DRIVE_BACKUP_STATUS", str(state / "last-success.json"))
    assert "latest Drive backup attempt failed" in check(service)["issues"]


def test_current_rclone_json_schema_is_accepted_without_network(uploader, monkeypatch):
    module, spool, config, state, work, remote, calls = uploader
    binary = Path("/usr/local/bin/rclone")
    if not binary.is_file():
        pytest.skip("Optional deployment rclone binary is unavailable")
    source = spool / snapshot_name()
    small_snapshot(module, source)
    # Exercise the installed CLI's real JSON encoding against a temporary local
    # file. The null config ensures no stored remote/OAuth credential is loaded.
    local = subprocess.run(
        [str(binary), "--config", "/dev/null", "lsjson", str(source), "--stat", "--hash"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    actual = module.run_rclone

    def actual_hash_schema(arguments, cfg):
        result = actual(arguments, cfg)
        return local.stdout if arguments[0] == "lsjson" else result

    monkeypatch.setattr(module, "run_rclone", actual_hash_schema)
    result = module.upload(spool, config, state, work)
    assert result["latest"]["md5"] == hashlib.md5(source.read_bytes()).hexdigest()
    assert not result["failures"]


def test_bad_old_snapshot_does_not_prevent_uploading_new_recovery_point(uploader):
    module, spool, config, state, work, remote, _ = uploader
    (spool / "20200101T000000Z.hadan.enc").write_bytes(b"corrupt" * 20)
    name = snapshot_name()
    small_snapshot(module, spool / name)
    try:
        module.upload(spool, config, state, work)
    except (ValueError, RuntimeError):
        # The job may report the old corruption after saving the fresh backup.
        pass
    assert "quant:" + name in remote, "old corruption prevented the valid newest backup"
    recorded = json.loads((state / "last-success.json").read_text())
    assert recorded["latest"]["name"] == name
    assert recorded["failures"], "partial success must not hide old snapshot failures"


def test_health_does_not_treat_rechecking_old_remote_snapshot_as_fresh_backup(
    harness, uploader, monkeypatch
):
    _, service, _ = harness
    module, spool, config, state, work, _, _ = uploader
    small_snapshot(module, spool / "20200101T000000Z.hadan.enc")
    try:
        module.upload(spool, config, state, work)
    except (ValueError, RuntimeError):
        pass
    monkeypatch.setenv("HADAN_DRIVE_BACKUP_STATUS", str(state / "last-success.json"))
    result = check(service)
    assert any("Drive backup" in issue for issue in result["issues"]), result


@pytest.mark.parametrize("invalid_time", [float("nan"), float("inf"), time.time() + 3600])
def test_health_rejects_nonfinite_or_future_snapshot_time(harness, tmp_path, monkeypatch, invalid_time):
    _, service, _ = harness
    status = tmp_path / "remote-status.json"
    status.write_text(json.dumps({"last_verified_at": time.time(), "verified_count": 1, "latest": {"snapshot_at": invalid_time}}))
    monkeypatch.setenv("HADAN_DRIVE_BACKUP_STATUS", str(status))
    assert any("Drive backup" in issue for issue in check(service)["issues"])


def test_downloaded_ciphertext_restores_with_separate_key_and_paused_delivery(
    harness, uploader, tmp_path
):
    _, service, _ = harness
    module, spool, config, state, work, remote, _ = uploader
    key = tmp_path / "separate.key"
    key.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    key.chmod(0o600)
    service.publish("quantgt", "offline-cloud-test", "Private report", "Private body", files=[{"name": "report.xlsx", "data": b"private spreadsheet"}])
    name = snapshot_name()
    backup(service, spool / name, key)
    module.upload(spool, config, state, work)
    assert len(remote) == 1
    downloaded = tmp_path / "downloaded.hadan.enc"
    downloaded.write_bytes(remote["quant:" + name])
    assert b"Private body" not in downloaded.read_bytes()
    assert key.read_bytes() not in downloaded.read_bytes()
    result = restore(downloaded, tmp_path / "restored", key)
    assert result["delivery_paused"] is True and result["bindings_revoked"] is True
    assert (tmp_path / "restored" / "hadan.sqlite3").is_file()
