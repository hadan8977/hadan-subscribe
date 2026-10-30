from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .db import audit, enqueue, uid
from .security import email_address, redact
from .service import Service


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def grant(s, email, product, until=None, *, email_consent=False, actor="cli"):
    email = email_address(email)
    expiry = (
        datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
        if until
        else None
    )
    if until and datetime.fromisoformat(until.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Expiry must include timezone")
    with s.db.transaction() as db:
        if not db.execute("SELECT id FROM products WHERE id=?", (product,)).fetchone():
            raise ValueError("Unknown product")
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        user_id = user[0] if user else uid()
        if not user:
            db.execute("INSERT INTO users VALUES(?,?,?)", (user_id, email, time.time()))
        db.execute(
            "INSERT OR IGNORE INTO access_allowlist(email,note,created_at) VALUES(?,?,?)",
            (email, actor, time.time()),
        )
        current = db.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
            (user_id, product),
        ).fetchone()
        sid = current["id"] if current else uid()
        if current:
            db.execute(
                "UPDATE subscriptions SET status='active',expires_at=? WHERE id=?",
                (expiry, sid),
            )
        else:
            db.execute(
                "INSERT INTO subscriptions(id,user_id,product_id,expires_at,email_enabled,agent_enabled,created_at) VALUES(?,?,?,?,?,0,?)",
                (sid, user_id, product, expiry, int(email_consent), time.time()),
            )
            db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
        audit(
            db,
            actor,
            "grant",
            sid,
            {
                "expires_at": expiry,
                "new_email_consent": email_consent if not current else "preserved",
            },
        )
    return {
        "subscription_id": sid,
        "email": email,
        "expires_at": expiry,
        "existing_preferences_preserved": bool(current),
    }


def import_members(s, path, apply=False, consent=False):
    value = json.loads(Path(path).read_text())
    members = value.get("members", value) if isinstance(value, dict) else value
    if isinstance(members, dict):
        members = [dict(v, email=k) for k, v in members.items()]
    if not isinstance(members, list):
        raise ValueError("Expected legacy members object or list")
    preview = []
    for item in members:
        email = email_address(item["email"])
        status = item.get("status", "active")
        expires = item.get("expires_at")
        if status == "expired" and not expires:
            raise ValueError("Expired legacy membership needs an explicit expiry")
        if s.db.one(
            "SELECT key FROM source_state WHERE key=?", ("legacy-member:" + email,)
        ):
            preview.append(
                {
                    "email": email,
                    "skipped": "already imported; current expiry and consent preserved",
                }
            )
            continue
        # Unknown/legacy consent is never inferred merely from an active payment.
        result = {
            "email": email,
            "status": status,
            "expires_at": expires,
            "email_enabled": consent and status not in ("cancelled", "expired"),
        }
        if apply:
            grant_result = grant(
                s,
                email,
                "quantgt",
                expires,
                email_consent=result["email_enabled"],
                actor="legacy-import",
            )
            if status == "cancelled":
                s.db.execute(
                    "UPDATE subscriptions SET status='ended',email_enabled=0,agent_enabled=0 WHERE id=?",
                    (grant_result["subscription_id"],),
                )
            with s.db.transaction() as db:
                marker = "legacy-member:" + email
                if not db.execute(
                    "SELECT key FROM source_state WHERE key=?", (marker,)
                ).fetchone():
                    db.execute(
                        "INSERT INTO source_state VALUES(?,?,?)",
                        (marker, json.dumps(item, ensure_ascii=False), time.time()),
                    )
                    audit(
                        db,
                        "legacy-import",
                        "legacy_history",
                        grant_result["subscription_id"],
                        {
                            "joined_at": item.get("joined_at"),
                            "notes": item.get("notes", item.get("note")),
                            "history": item.get("history", []),
                        },
                    )
        preview.append(result)
    return {
        "applied": apply,
        "members": preview,
        "warning": "Existing channel opt-outs are preserved. Import expiry is not consent. Agent binding requires email verification.",
    }


def doctor(cfg):
    errors = cfg.preflight()
    details = {
        "python": sys.version.split()[0],
        "data_dir": str(cfg.data_dir),
        "sqlite_journal": "DELETE",
        "mail_backend": cfg.mail_backend,
        "production_ready": not errors,
        "missing": errors,
        "sources": {
            "enabled": os.getenv("QUANTGT_ENABLED") == "1",
            "credentials_present": bool(
                os.getenv("QUANTGT_EMAIL") and os.getenv("QUANTGT_PASSWORD")
            ),
            "imap_present": bool(
                os.getenv("IMAP_HOST")
                and os.getenv("IMAP_USERNAME")
                and os.getenv("IMAP_PASSWORD")
            ),
        },
        "maintenance": {
            "configured": cfg.ops_ai and bool(cfg.deepseek_key),
            "isolated_verifier": bool(shutil.which("bwrap")),
            "daily_call_limit": int(os.getenv("OPS_MAX_CALLS_PER_DAY", "10")),
        },
        "payments_configured": {
            "stripe": bool(cfg.stripe_key and cfg.stripe_webhook_secret),
            "xorpay": bool(cfg.xorpay_enabled and cfg.xorpay_aid and cfg.xorpay_secret and cfg.payment_plans),
            "xiaodou": bool(cfg.xiaodou_enabled and cfg.xiaodou_app_id and cfg.xiaodou_private_key_file and cfg.payment_plans),
            "redemption_codes": True,
            "usdt": False,
        },
        "backup_key_configured": bool(os.getenv("HADAN_BACKUP_KEY_FILE")),
    }
    return details


def parser():
    p = argparse.ArgumentParser(prog="hadan")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("doctor")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    worker = commands.add_parser("worker")
    worker.add_argument("--queue", choices=["main", "source", "ops"], default="main")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("status")
    commands.add_parser("pause")
    resume = commands.add_parser("resume")
    resume.add_argument("--confirm-current-preferences", action="store_true")
    pub = commands.add_parser("publish")
    pub.add_argument(
        "file",
        help="JSON with product_id,source_key,title,body, optional attachments paths",
    )
    imp = commands.add_parser("import-members")
    imp.add_argument("file")
    imp.add_argument("--apply", action="store_true")
    imp.add_argument("--email-consent", action="store_true")
    members = commands.add_parser("members")
    members.add_argument("--expiring-days", type=int)
    g = commands.add_parser("grant")
    g.add_argument("email")
    g.add_argument("--product", default="quantgt")
    g.add_argument("--until")
    g.add_argument("--email-consent", action="store_true")
    exp = commands.add_parser("set-expiry")
    exp.add_argument("subscription_id")
    exp.add_argument("until")
    payment = commands.add_parser("record-payment")
    payment.add_argument("email")
    payment.add_argument("amount", type=int, help="Minor currency units, e.g. cents")
    payment.add_argument("--currency", default="usd")
    payment.add_argument("--product", default="quantgt")
    payment.add_argument("--reference", required=True)
    pay_report = commands.add_parser("payments")
    pay_report.add_argument("--legacy", action="store_true")
    commands.add_parser("orders")
    resolve_order = commands.add_parser("resolve-order")
    resolve_order.add_argument("order_id")
    resolve_order.add_argument("--outcome", choices=["fulfilled", "refunded"], required=True)
    resolve_order.add_argument("--reference", required=True, help="Provider receipt for an already completed external reconciliation; this command does not refund or grant access")
    codes = commands.add_parser("issue-codes")
    codes.add_argument("--days", type=int, required=True)
    codes.add_argument("--count", type=int, default=1)
    codes.add_argument("--output", required=True, help="Private CSV output; plaintext codes are never printed")
    codes.add_argument("--product", default="quantgt")
    codes.add_argument("--expires", help="Optional timezone-aware redemption expiry")
    revoke_codes = commands.add_parser("revoke-codes")
    revoke_codes.add_argument("batch_id")
    commands.add_parser("code-batches")
    commands.add_parser("inbox-cutover")
    archive = commands.add_parser("import-archive")
    archive.add_argument("manifest")
    archive.add_argument("--apply", action="store_true")
    product = commands.add_parser("product")
    product.add_argument("id")
    product.add_argument("--mode", choices=["paid", "free"], required=True)
    product.add_argument("--price-id")
    product.add_argument("--name")
    bulk = commands.add_parser("bulk-members")
    bulk.add_argument("file")
    bulk.add_argument("--apply", action="store_true")
    clean = commands.add_parser("cleanup")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--days", type=int, default=90)
    sup = commands.add_parser("suppress")
    sup.add_argument("email")
    sup.add_argument("reason", choices=["bounce", "complaint", "manual"])
    job = commands.add_parser("run")
    job.add_argument(
        "kind",
        choices=[
            "picks",
            "weekly_digest",
            "official_mail",
            "health_site",
            "health",
            "daily_admin_status",
        ],
    )
    replay = commands.add_parser("replay")
    replay.add_argument("event_id")
    replay.add_argument("--apply", action="store_true")
    replay.add_argument("--user-id")
    back = commands.add_parser("backup")
    back.add_argument("destination")
    back.add_argument("--key-file", default=os.getenv("HADAN_BACKUP_KEY_FILE"))
    restore = commands.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("destination")
    restore.add_argument("--key-file", default=os.getenv("HADAN_BACKUP_KEY_FILE"))
    patch = commands.add_parser("verify-patch")
    patch.add_argument("patch")
    patch.add_argument("destination")
    patch.add_argument("--checkout", default=".")
    commands.add_parser("check-health")
    return p


def main():
    load_dotenv(override=False)
    args = parser().parse_args()
    cfg = Settings()
    # Only the main mail worker (and the explicit doctor check) needs SMTP
    # credentials. Web and source processes validate the common production
    # settings without inheriting a send credential they never use.
    cfg.smtp_required = args.command == "doctor" or (
        args.command == "worker" and args.queue == "main"
    )
    try:
        if args.command == "doctor":
            output(doctor(cfg))
            return
        if args.command == "restore":
            from .backup import restore

            if not args.key_file:
                raise ValueError("Supply separate backup decryption key file")
            output(restore(args.archive, args.destination, args.key_file))
            return
        if args.command == "verify-patch":
            from .ops import verify_patch

            output(
                verify_patch(
                    args.checkout, Path(args.patch).read_text(), args.destination
                )
            )
            return
        if args.command == "serve":
            import uvicorn
            from .web import create_app

            uvicorn.run(
                create_app(cfg),
                host=args.host,
                port=args.port,
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1",
                access_log=False,
                limit_concurrency=32,
                timeout_keep_alive=5,
            )
            return
        s = Service(cfg)
        if args.command == "init":
            s.seed()
            output(
                {
                    "initialized": str(cfg.data_dir),
                    "mail": cfg.mail_backend,
                    "note": "No real subscriber data imported",
                }
            )
        elif args.command == "worker":
            from .worker import Worker

            w = Worker(s, queue=args.queue)
            output({"worked": w.run_once()}) if args.once else w.run()
        elif args.command == "status":
            output(
                {
                    "jobs": s.db.all(
                        "SELECT kind,state,COUNT(*) count FROM jobs GROUP BY kind,state"
                    ),
                    "incidents": s.db.all(
                        "SELECT id,kind,summary,state FROM incidents"
                    ),
                    "heartbeats": s.db.all("SELECT * FROM heartbeats"),
                }
            )
        elif args.command in ("pause", "resume"):
            if args.command == "resume" and not args.confirm_current_preferences:
                raise ValueError(
                    "Compare final source snapshot and current consent, then pass --confirm-current-preferences"
                )
            s.db.execute(
                "INSERT INTO meta VALUES('delivery_paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if args.command == "pause" else "0",),
            )
            output({"paused": args.command == "pause"})
        elif args.command == "publish":
            p = json.loads(Path(args.file).read_text())
            files = [
                {"name": Path(x).name, "data": Path(x).read_bytes()}
                for x in p.pop("attachments", [])
            ]
            output(s.publish(**p, files=files, actor="cli"))
        elif args.command == "import-members":
            output(import_members(s, args.file, args.apply, args.email_consent))
        elif args.command == "grant":
            output(
                grant(
                    s,
                    args.email,
                    args.product,
                    args.until,
                    email_consent=args.email_consent,
                )
            )
        elif args.command == "members":
            sql = (
                "SELECT u.email,s.* FROM subscriptions s JOIN users u ON u.id=s.user_id"
            )
            params = ()
            if args.expiring_days is not None:
                sql += " WHERE s.expires_at BETWEEN ? AND ?"
                params = (time.time(), time.time() + args.expiring_days * 86400)
            output(s.db.all(sql, params))
        elif args.command == "set-expiry":
            dt = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
            if not dt.tzinfo:
                raise ValueError("Use timezone-aware expiry")
            with s.db.transaction() as db:
                count = db.execute(
                    "UPDATE subscriptions SET expires_at=? WHERE id=?",
                    (dt.timestamp(), args.subscription_id),
                ).rowcount
                if not count:
                    raise ValueError("Subscription not found")
                audit(
                    db,
                    "cli",
                    "set_expiry",
                    args.subscription_id,
                    {"expires_at": dt.isoformat()},
                )
            output({"updated": True})
        elif args.command == "record-payment":
            user = s.db.one(
                "SELECT id FROM users WHERE email=?", (email_address(args.email),)
            )
            if not user or args.amount < 0:
                raise ValueError("Unknown member or invalid amount")
            with s.db.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO payments VALUES(?,?,?,?,?,?,?)",
                    (
                        uid(),
                        user["id"],
                        args.product,
                        "manual:" + args.reference,
                        args.amount,
                        args.currency,
                        time.time(),
                    ),
                )
                audit(
                    db,
                    "cli",
                    "manual_payment",
                    user["id"],
                    {"reference": args.reference},
                )
            output({"recorded": True, "entitlement_changed": False})
        elif args.command == "issue-codes":
            from .commerce import Commerce
            expiry = None
            if args.expires:
                parsed = datetime.fromisoformat(args.expires.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Code expiry requires a timezone")
                expiry = parsed.timestamp()
            output(Commerce(s).issue_codes(product_id=args.product, days=args.days, count=args.count,
                                          output=args.output, expires_at=expiry))
        elif args.command == "revoke-codes":
            from .commerce import Commerce
            output(Commerce(s).revoke_batch(args.batch_id))
        elif args.command == "code-batches":
            output(s.db.all("SELECT b.id,b.product_id,b.days,b.created_at,c.state,COUNT(*) quantity FROM code_batches b JOIN redemption_codes c ON c.batch_id=b.id GROUP BY b.id,c.state ORDER BY b.created_at DESC"))
        elif args.command == "orders":
            output(s.db.all("SELECT id,user_id,plan_id,method,amount_minor,currency,status,created_at,paid_at FROM purchase_orders ORDER BY created_at DESC LIMIT 200"))
        elif args.command == "resolve-order":
            from .commerce import Commerce
            output(Commerce(s).resolve_order(args.order_id, outcome=args.outcome, reference=args.reference))
        elif args.command == "inbox-cutover":
            from .sources.jobs import cutover_mailbox
            output(cutover_mailbox(s))
        elif args.command == "payments":
            if args.legacy:
                from .migration import legacy_payments

                output(legacy_payments(s))
            else:
                output(s.db.all("SELECT * FROM payments ORDER BY created_at DESC"))
        elif args.command == "import-archive":
            from .migration import import_archive

            output(import_archive(s, args.manifest, apply=args.apply))
        elif args.command == "product":
            if not s.db.one("SELECT id FROM products WHERE id=?", (args.id,)):
                raise ValueError("Unknown product")
            if args.price_id and not args.price_id.startswith("price_"):
                raise ValueError("Expected a Stripe Price ID")
            with s.db.transaction() as db:
                db.execute(
                    "UPDATE products SET paid=?,price_id=?,name=COALESCE(?,name) WHERE id=?",
                    (int(args.mode == "paid"), args.price_id, args.name, args.id),
                )
                audit(
                    db,
                    "cli",
                    "product_configured",
                    args.id,
                    {"mode": args.mode, "price_id": args.price_id},
                )
            output({"configured": args.id, "mode": args.mode})
        elif args.command == "bulk-members":
            from .maintenance import bulk_members

            output(
                bulk_members(
                    s, json.loads(Path(args.file).read_text()), apply=args.apply
                )
            )
        elif args.command == "cleanup":
            from .maintenance import cleanup

            output(cleanup(s, apply=args.apply, days=args.days))
        elif args.command == "suppress":
            s.db.execute(
                "INSERT OR REPLACE INTO suppressions VALUES(?,?,?)",
                (email_address(args.email), args.reason, time.time()),
            )
            output({"suppressed": True})
        elif args.command == "run":
            with s.db.transaction() as db:
                jid = enqueue(
                    db,
                    "admin_summary"
                    if args.kind == "daily_admin_status"
                    else "source:" + args.kind,
                    {},
                    "manual:" + uid(),
                    queue="main" if args.kind == "daily_admin_status" else "source",
                )
            output({"job_id": jid})
        elif args.command == "replay":
            output(s.replay(args.event_id, args.user_id, apply=args.apply))
        elif args.command == "backup":
            from .backup import backup

            if not args.key_file:
                raise ValueError("Supply separate backup encryption key file")
            output(backup(s, args.destination, args.key_file))
        elif args.command == "check-health":
            from .health import check
            from .worker import Worker

            result = check(s)
            watcher = Worker(s)
            current = {watcher.clean(issue) for issue in result["issues"]}
            for issue in current:
                watcher.incident("health", issue)
            with s.db.transaction() as db:
                for row in db.execute("SELECT id,summary FROM incidents WHERE kind='health' AND state='open'").fetchall():
                    if row["summary"] not in current:
                        db.execute("UPDATE incidents SET state='resolved',updated_at=? WHERE id=?", (time.time(), row["id"]))
            output(result)
            if not result["healthy"]:
                raise SystemExit(1)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": redact(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
