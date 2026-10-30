from __future__ import annotations
import shutil
import time
import os


def check(service):
    now = time.time()
    issues = []
    source_enabled = (
        os.getenv("QUANTGT_ENABLED") == "1" or os.getenv("OFFICIAL_MAIL_ENABLED") == "1"
    )
    for name in (
        "scheduler",
        "worker:main",
        *(("worker:source",) if source_enabled else ()),
        *(("worker:ops",) if service.settings.ops_ai else ()),
    ):
        row = service.db.one("SELECT updated_at FROM heartbeats WHERE name=?", (name,))
        threshold = {
            "worker:source": service.settings.source_timeout + 120,
            "worker:ops": service.settings.ops_timeout + 120,
        }.get(name, 180)
        if not row or now - row["updated_at"] > threshold:
            issues.append(name + " heartbeat missing/stale")
    if shutil.disk_usage(service.settings.data_dir).free < 256 * 1024 * 1024:
        issues.append("disk free below 256 MiB")
    row = service.db.one(
        "SELECT MIN(available_at) oldest FROM jobs WHERE state='queued' AND queue='main'"
    )
    if row["oldest"] and now - row["oldest"] > 1800:
        issues.append("main delivery queue delayed more than 30 minutes")
    if source_enabled:
        row = service.db.one(
            "SELECT MIN(available_at) oldest FROM jobs WHERE state='queued' AND queue='source'"
        )
        if row["oldest"] and now - row["oldest"] > 3600:
            issues.append("source queue delayed more than one hour")
    if service.settings.ops_ai:
        row = service.db.one(
            "SELECT MIN(available_at) oldest FROM jobs WHERE state='queued' AND queue='ops'"
        )
        if row["oldest"] and now - row["oldest"] > 3600:
            issues.append("maintenance queue delayed more than one hour")
    if os.getenv("QUANTGT_ENABLED") == "1":
        import json

        row = service.db.one(
            "SELECT value FROM source_state WHERE key='picks:last_success'"
        )
        if not row or now - float(json.loads(row["value"])) > 30 * 3600:
            issues.append("source picks success stale or missing")
    count = service.db.one(
        "SELECT COUNT(*) count FROM jobs WHERE state IN ('dead','uncertain')"
    )["count"]
    if count:
        issues.append(str(count) + " failed/uncertain jobs need attention")
    backup = service.db.one("SELECT value FROM meta WHERE key='last_backup'")
    if (service.settings.production or os.getenv("HADAN_BACKUP_REQUIRED") == "1") and (
        not backup or now - float(backup["value"]) > 48 * 3600
    ):
        issues.append("backup older than 48 hours or missing")
    remote_status = os.getenv("HADAN_DRIVE_BACKUP_STATUS", "")
    if remote_status:
        import json
        from pathlib import Path
        try:
            status = json.loads(Path(remote_status).read_text())
            if (not status.get("verified_count")
                or not -300 <= now - float(status["last_verified_at"]) <= 48 * 3600
                or not -300 <= now - float(status["latest"]["snapshot_at"]) <= 48 * 3600):
                issues.append("verified Drive backup older than 48 hours")
            if status.get("failures"):
                issues.append("Drive backup has failed snapshot uploads")
            attempt_path = Path(remote_status).with_name("last-attempt.json")
            if attempt_path.is_file():
                attempt = json.loads(attempt_path.read_text())
                if attempt.get("failures") and attempt.get("last_attempt_at", 0) >= status["last_verified_at"]:
                    issues.append("latest Drive backup attempt failed")
        except (OSError, ValueError, KeyError, TypeError):
            issues.append("verified Drive backup status missing or invalid")
    return {"healthy": not issues, "issues": issues, "at": now}
