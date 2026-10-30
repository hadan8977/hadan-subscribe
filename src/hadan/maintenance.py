from __future__ import annotations
import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from .db import audit


def cleanup(service, *, apply=False, days=90):
    from .service import artifact_lock

    with artifact_lock(service.settings.data_dir):
        return _cleanup_locked(service, apply=apply, days=days)


def _cleanup_locked(service, *, apply=False, days=90):
    """Bound operational history without deleting subscribed content or replay manifests."""
    if days < 30:
        raise ValueError("Operational retention must be at least 30 days")
    cutoff = time.time() - days * 86400
    jobs = service.db.all(
        "SELECT id FROM jobs WHERE state IN ('complete','cancelled') AND created_at<? AND kind NOT IN ('deliver','billing_reconcile','account_mail')",
        (cutoff,),
    )
    # Keep content delivery identities/payment idempotency permanently until explicit archive.
    referenced = {
        r["storage_key"] for r in service.db.all("SELECT storage_key FROM attachments")
    }
    files = [
        p
        for p in (service.settings.data_dir / "files").iterdir()
        if p.is_file()
        and p.name not in referenced
        and p.stat().st_mtime < time.time() - 86400
    ]
    temporary = [
        p
        for p in (service.settings.data_dir / "mail").glob("*.eml")
        if p.stat().st_mtime < cutoff
    ]
    quarantine = [
        p
        for p in (service.settings.data_dir / "source").glob("quarantine-*.eml")
        if p.stat().st_mtime < time.time() - 7 * 86400
    ]
    if apply:
        with service.db.transaction() as db:
            for row in jobs:
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            audit(
                db,
                "maintenance",
                "cleanup",
                "operational",
                {"jobs": len(jobs), "orphan_files": len(files)},
            )
        for p in [*files, *temporary, *quarantine]:
            if not p.is_symlink():
                p.unlink(missing_ok=True)
    return {
        "apply": apply,
        "old_jobs": len(jobs),
        "orphan_files": len(files),
        "old_local_mail": len(temporary),
        "quarantine_files": len(quarantine),
        "content_versions_deleted": 0,
    }


def bulk_members(service, operations, *, apply=False):
    from .cli import grant
    from .security import email_address

    if not isinstance(operations, list) or len(operations) > 1000:
        raise ValueError("Supply at most 1000 member operations")
    results = []
    seen = set()
    for op in operations:
        if not isinstance(op, dict):
            results.append(
                {"error": "Each member operation must be an object", "applied": False}
            )
            continue
        try:
            email = email_address(op["email"])
            product = op.get("product", "quantgt")
            kind = op.get("operation", "grant")
            if not service.db.one("SELECT id FROM products WHERE id=?", (product,)):
                raise ValueError("Unknown product")
            if kind not in {"grant", "extend", "set_expiry"}:
                raise ValueError("Unsupported bulk operation")
            if (email, product) in seen:
                raise ValueError("Duplicate member in batch")
            seen.add((email, product))
            current = service.db.one(
                "SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.email=? AND product_id=?",
                (email, product),
            )
            until = op.get("until")
            if kind == "extend":
                from .compat.membership import extend_expiry

                if not current:
                    raise ValueError("Cannot extend missing member")
                months = op.get("months", 1)
                if type(months) is not int or not 1 <= months <= 120:
                    raise ValueError("Invalid month count")
                expiry = (
                    datetime.fromtimestamp(current["expires_at"], timezone.utc)
                    if current["expires_at"]
                    else None
                )
                until = extend_expiry(
                    expiry, months, datetime.now(timezone.utc)
                ).isoformat()
            if kind == "set_expiry" and not current:
                raise ValueError("Cannot change missing member")
            if until:
                dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if not dt.tzinfo:
                    raise ValueError("Expiry must include timezone")
            result = {
                "email": email,
                "operation": kind,
                "until": until,
                "applied": apply,
            }
            if apply:
                if kind == "set_expiry":
                    expiry = (
                        datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
                        if until
                        else None
                    )
                    with service.db.transaction() as db:
                        db.execute(
                            "UPDATE subscriptions SET expires_at=? WHERE id=?",
                            (expiry, current["id"]),
                        )
                        audit(
                            db,
                            "bulk",
                            "set_expiry",
                            current["id"],
                            {"expires_at": expiry},
                        )
                else:
                    result.update(
                        grant(
                            service,
                            email,
                            product,
                            until,
                            email_consent=op.get("email_consent") is True,
                            actor="bulk",
                        )
                    )
            results.append(result)
        except (ValueError, KeyError) as exc:
            results.append(
                {"email": op.get("email", ""), "error": str(exc), "applied": False}
            )
    return {"apply": apply, "results": results}
