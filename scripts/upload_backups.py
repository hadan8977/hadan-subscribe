#!/usr/bin/env python3
"""Copy encrypted snapshots to a dedicated Drive folder under a separate OS user."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone

MAGIC = b"HADAN-BACKUP-1\n"
NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z\.hadan\.enc")


def run_rclone(arguments, config):
    result = subprocess.run(
        ["/usr/local/bin/rclone", "--config", str(config), "--contimeout", "10s",
         "--timeout", "60s", "--retries", "2", "--low-level-retries", "2", *arguments],
        capture_output=True, text=True, timeout=240, check=False,
    )
    if result.returncode:
        # Never print remote config, OAuth response or provider diagnostic payloads.
        raise RuntimeError("Drive backup operation failed; inspect the dedicated uploader credentials and network")
    return result.stdout


def copy_snapshot(source, target):
    """Hold a non-symlink descriptor; never follow app-controlled links into uploader secrets."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 42 <= metadata.st_size <= 16 * 1024**3:
            raise ValueError("Invalid encrypted snapshot")
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("Only completed encrypted snapshots can be uploaded")
        stream.seek(0)
        with open(target, "xb") as output:
            os.chmod(target, 0o600)
            shutil.copyfileobj(stream, output, 65536)
        if target.stat().st_size != metadata.st_size:
            raise ValueError("Snapshot changed during copy")


def upload(spool, config, state_dir, work_dir):
    if not config.is_file():
        raise ValueError("Dedicated Drive configuration is missing")
    verified, failures = [], []
    # Secure the newest recovery point first; a damaged old file must not starve it.
    for source in sorted(spool.iterdir(), reverse=True):
        if not NAME.fullmatch(source.name):
            continue
        stage = "validate_local"
        try:
            if source.is_symlink():
                raise ValueError("Snapshot symlinks are not accepted")
            snapshot_at = datetime.strptime(source.name, "%Y%m%dT%H%M%SZ.hadan.enc").replace(tzinfo=timezone.utc).timestamp()
            with tempfile.TemporaryDirectory(prefix="upload-", dir=work_dir) as tmp:
                staged = Path(tmp) / source.name
                copy_snapshot(source, staged)
                with staged.open("rb") as stream:
                    local_md5 = hashlib.file_digest(stream, "md5").hexdigest()
                remote = "quant:" + source.name
                stage = "upload"
                run_rclone(["copyto", str(staged), remote, "--immutable", "--transfers", "1"], config)
                stage = "verify_remote"
                details = json.loads(run_rclone(["lsjson", remote, "--stat", "--hash"], config))
                hashes = {key.lower(): value for key, value in details.get("Hashes", {}).items()}
                if details.get("Size") != staged.stat().st_size or hashes.get("md5", "").lower() != local_md5:
                    raise ValueError("Drive backup checksum mismatch")
                verified.append({"name": source.name, "snapshot_at": snapshot_at, "bytes": staged.stat().st_size, "md5": local_md5})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append({"name": source.name, "stage": stage, "reason": type(exc).__name__})
    write_status(state_dir, "last-attempt", {"last_attempt_at": time.time(), "verified_count": len(verified), "failures": failures})
    if not verified:
        raise ValueError("No encrypted snapshot verified; see last-attempt.json for failure stages")
    result = {"last_verified_at": time.time(), "verified_count": len(verified), "latest": verified[0], "failures": failures}
    write_status(state_dir, "last-success", result)
    return result


def write_status(state_dir, name, result):
    temporary = state_dir / ("." + name + ".partial")
    with open(temporary, "w") as stream:
        os.chmod(temporary, 0o640)
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, state_dir / (name + ".json"))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        result = upload(Path("/var/spool/quant-subscribe-backup"), Path("/var/lib/quant-subscribe-drive/rclone.conf"),
                        Path("/var/lib/quant-subscribe-backup-status"), Path("/var/lib/quant-subscribe-drive"))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": type(exc).__name__, "action": "Inspect last-attempt.json and dedicated Drive configuration"}))
        raise SystemExit(1) from None
    print(json.dumps(result))
    if result["failures"]:
        raise SystemExit(1)
