from __future__ import annotations

import html
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from .db import audit, enqueue, uid
from .security import action_token, decrypt, digest, mac, redact
from .service import Problem
from .transport import DeliveryError, MailTooLarge, Transport


class Worker:
    def __init__(self, service, transport=None, queue="main"):
        self.s = service
        self.db = service.db
        self.cfg = service.settings
        self.transport = transport or Transport(self.cfg)
        self.queue = queue
        self.stopping = False
        self.last_heartbeat = 0.0

    def clean(self, value):
        text = str(value)
        for value in (
            self.cfg.secret,
            self.cfg.deepseek_key,
            self.cfg.smtp_password,
            self.cfg.stripe_key,
            self.cfg.stripe_webhook_secret,
            self.cfg.xorpay_secret,
        ):
            if value:
                text = text.replace(value, "[redacted]")
        return redact(text)

    def claim(self):
        now = time.time()
        with self.db.transaction() as db:
            paused = db.execute(
                "SELECT value FROM meta WHERE key='delivery_paused'"
            ).fetchone()
            # Migration/restore pause freezes every side effect, including account/login mail,
            # source publication, billing mutations and AI jobs, without spending retry attempts.
            if paused and paused["value"] == "1":
                return None
            stale = db.execute(
                "SELECT * FROM jobs WHERE state='running' AND lease_until<? AND queue=?",
                (now, self.queue),
            ).fetchall()
            for row in stale:
                payload = json.loads(row["payload"])
                if (
                    row["kind"] in ("deliver", "account_mail")
                    and "delivery_id" in payload
                    and self.cfg.mail_backend == "smtp"
                ):
                    d = db.execute(
                        "SELECT * FROM deliveries WHERE id=?", (payload["delivery_id"],)
                    ).fetchone()
                    if d and d["channel"] == "email" and d["state"] == "sending":
                        db.execute(
                            "UPDATE deliveries SET state='uncertain',error='Worker interrupted during SMTP' WHERE id=?",
                            (d["id"],),
                        )
                        db.execute(
                            "UPDATE jobs SET state='uncertain' WHERE id=?", (row["id"],)
                        )
                        continue
                db.execute(
                    "UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL WHERE id=?",
                    (row["id"],),
                )
            # Source browsers and isolated AI verification share the small host's
            # heavy-work slot. The DB transaction makes cross-worker admission atomic.
            if self.queue in ("source", "ops") and db.execute(
                "SELECT 1 FROM jobs WHERE queue IN ('source','ops') AND state='running' AND lease_until>=? LIMIT 1", (now,)
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND queue=? AND available_at<=? ORDER BY CASE kind WHEN 'login_mail' THEN 0 ELSE 1 END,available_at LIMIT 1",
                (self.queue, now),
            ).fetchone()
            if not row:
                return None
            lease = uid()
            db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                (now + self.cfg.lease_seconds, lease, row["id"]),
            )
            result = dict(row)
            result["lease_token"] = lease
            result["attempts"] += 1
            return result

    def incident(self, kind, message, details=""):
        message = self.clean(message)
        details = self.clean(details)
        now = time.time()
        fingerprint = digest(kind + ":" + message)
        with self.db.transaction() as db:
            existing = db.execute(
                "SELECT * FROM incidents WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                if existing["state"] == "resolved":
                    # A later recurrence is a new bounded repair episode. Preserve
                    # the old incident/audit instead of spending its retry forever.
                    db.execute(
                        "UPDATE incidents SET fingerprint=? WHERE id=?",
                        (digest(fingerprint + ":" + existing["id"]), existing["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE incidents SET count=count+1,state='open',updated_at=?,details=? WHERE id=?",
                        (now, redact(details), existing["id"]),
                    )
                    return existing["id"]
            iid = uid()
            db.execute(
                "INSERT INTO incidents(id,fingerprint,kind,summary,details,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (iid, fingerprint, kind, redact(message), redact(details), now, now),
            )
            if self.cfg.ops_ai and kind not in ("diagnose", "propose_parser"):
                enqueue(
                    db,
                    "diagnose",
                    {"incident_id": iid},
                    "diagnose:"
                    + iid
                    + ":"
                    + datetime.now(timezone.utc).date().isoformat(),
                    queue="ops",
                )
            return iid

    def run_once(self):
        if time.monotonic() - self.last_heartbeat >= 30:
            self.db.execute(
                "INSERT INTO heartbeats VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,details=excluded.details",
                ("worker:" + self.queue, time.time(), "{}"),
            )
            self.last_heartbeat = time.monotonic()
        job = self.claim()
        if not job:
            return False
        try:
            self.dispatch(job)
        except Exception as exc:
            retry = not isinstance(exc, (Problem, ValueError))
            uncertain = False
            if isinstance(exc, DeliveryError):
                retry, uncertain = exc.retry, exc.uncertain
            dead = not retry or job["attempts"] >= self.cfg.max_attempts
            state = "uncertain" if uncertain else ("dead" if dead else "queued")
            delay = min(3600, 15 * 2 ** job["attempts"]) + random.uniform(0, 5)
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE jobs SET state=?,last_error=?,available_at=?,lease_until=NULL WHERE id=? AND lease_token=?",
                    (
                        state,
                        self.clean(exc),
                        time.time() + delay,
                        job["id"],
                        job["lease_token"],
                    ),
                )
                payload = json.loads(job["payload"])
                if job["kind"] in (
                    "deliver",
                    "account_mail",
                ) and "delivery_id" in payload:
                    did = payload["delivery_id"]
                    db.execute(
                        "UPDATE deliveries SET state=?,error=?,updated_at=? WHERE id=?",
                        (
                            "uncertain"
                            if uncertain
                            else ("failed" if dead else "retrying"),
                            self.clean(exc),
                            time.time(),
                            did,
                        ),
                    )
                if job["kind"] == "mcp_event_deliver":
                    db.execute(
                        "UPDATE mcp_event_deliveries SET state=?,last_error=?,updated_at=? "
                        "WHERE subscription_id=? AND event_id=?",
                        (
                            "failed" if dead else "retrying",
                            self.clean(exc),
                            time.time(),
                            payload.get("subscription_id"),
                            payload.get("event_id"),
                        ),
                    )
            if dead or uncertain:
                self.incident(job["kind"], str(exc), "job_id=" + job["id"])
        else:
            self.db.execute(
                "UPDATE jobs SET state='complete',lease_until=NULL,last_error=NULL WHERE id=? AND lease_token=?",
                (job["id"], job["lease_token"]),
            )
            self.db.execute(
                "UPDATE incidents SET state='resolved',updated_at=? WHERE state='open' AND details=?",
                (time.time(), "job_id=" + job["id"]),
            )
        return True

    def dispatch(self, job):
        p = json.loads(job["payload"])
        kind = job["kind"]
        if kind == "login_mail":
            if self.db.one(
                "SELECT email FROM suppressions WHERE email=?", (p["email"],)
            ):
                return
            code = decrypt(self.cfg.secret, p["encrypted_code"])
            current = self.db.one(
                "SELECT * FROM otp WHERE email=? AND expires_at>?",
                (p["email"], time.time()),
            )
            if not current or current["digest"] != mac(
                self.cfg.secret, p["email"] + ":" + code
            ):
                return
            self.transport.mail(
                p["email"],
                "Quant · 登录验证码 / Sign-in code",
                f"你的验证码是 {code}，10 分钟内有效。请勿将验证码交给他人。\n\nYour Quant sign-in code is {code}. It expires in 10 minutes. Do not share it with anyone.",
                identity=job["id"],
            )
        elif kind == "account_mail":
            self.deliver(p["delivery_id"])
        elif kind == "deliver":
            self.deliver(p["delivery_id"])
        elif kind == "purchase_reconcile":
            from .commerce import Commerce
            result = Commerce(self.s).reconcile(p["order_id"])
            if result.get("needs_review"):
                self.incident("payment_review", "Paid order requires a signed payment callback", "order_id=" + p["order_id"])
        elif kind == "webhook_test":
            b = self.db.one(
                "SELECT * FROM bindings WHERE id=? AND state='pending'",
                (p["binding_id"],),
            )
            if b:
                self.transport.webhook(
                    b["endpoint"],
                    {
                        "type": "connection.test",
                        "event_id": "test:" + b["id"],
                        "next": "Use your bearer token to GET /api/agent/test and /api/agent/test-file, then POST /api/agent/test/complete",
                    },
                    mac(self.cfg.secret, "hook:" + b["id"]),
                    "test:" + b["id"],
                )
                self.db.execute(
                    "UPDATE bindings SET test_hook=1 WHERE id=? AND state='pending'",
                    (b["id"],),
                )
        elif kind == "mcp_event_deliver":
            from .mcp_events import MCPEvents

            MCPEvents(self.s).deliver(p["subscription_id"], p["event_id"])
        elif kind.startswith("source:"):
            child_env = os.environ.copy()
            child_env["HADAN_DATA_DIR"] = str(self.cfg.data_dir)
            cmd = [sys.executable, "-m", "hadan.sources.jobs", kind.split(":", 1)[1]]
            if kind == "source:activate_parser":
                cmd += [p["candidate"], p["sha256"]]
            proc = subprocess.Popen(
                cmd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=self.cfg.source_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                if kind in (
                    "source:picks",
                    "source:weekly_digest",
                    "source:health_site",
                ):
                    from .sources.jobs import rollback_parser

                    rollback_parser(self.s)
                raise DeliveryError("Source task exceeded time limit")
            if proc.returncode:
                raise DeliveryError(redact(err.decode(errors="replace")[-1500:]))
        elif kind in ("billing_cancel", "billing_reconcile"):
            from .billing import Billing

            getattr(
                Billing(self.s), "cancel" if kind == "billing_cancel" else "reconcile"
            )(**p)
        elif kind in ("diagnose", "propose_parser"):
            child_env = os.environ.copy()
            child_env["HADAN_DATA_DIR"] = str(self.cfg.data_dir)
            # Models and isolated verification cannot hold the ops lease forever.
            # stdout is file-backed; only a bounded, redacted error tail enters logs.
            with tempfile.TemporaryFile(mode="w+b") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "hadan.ops", kind, p["incident_id"]],
                    env=child_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    proc.communicate(
                        timeout=min(self.cfg.ops_timeout, self.cfg.lease_seconds - 5)
                    )
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()
                    raise DeliveryError("Maintenance task exceeded time limit")
                if proc.returncode:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell() - 1500))
                    raise DeliveryError(
                        self.clean(log.read().decode(errors="replace")),
                        retry=proc.returncode != 2,
                    )
        elif kind == "admin_summary":
            self.admin_summary(job["id"])
        else:
            raise ValueError("Unknown job kind: " + kind)

    def deliver(self, did):
        paused = self.db.one("SELECT value FROM meta WHERE key='delivery_paused'")
        if paused and paused["value"] == "1":
            raise DeliveryError("Delivery paused after restore or by operator")
        d = self.db.one(
            "SELECT d.*,e.product_id,e.content_id,e.version_id,e.kind,e.summary,c.withdrawn,c.current_version,v.number FROM deliveries d JOIN events e ON e.id=d.event_id LEFT JOIN contents c ON c.id=e.content_id LEFT JOIN versions v ON v.id=e.version_id WHERE d.id=?",
            (did,),
        )
        if not d or d["state"] in ("accepted", "skipped", "uncertain"):
            return
        if d["kind"].startswith("account:"):
            return self.deliver_account(d)
        s = self.s.entitled(d["user_id"], d["product_id"])
        skip = not s or (d["withdrawn"] and d["kind"] != "withdrawn")
        if d["number"] and d["number"] != d["current_version"]:
            skip = True
        b = None
        if d["channel"] == "email":
            user = self.db.one("SELECT * FROM users WHERE id=?", (d["user_id"],))
            skip = skip or not s["email_enabled"] if s else True
            skip = skip or bool(
                self.db.one(
                    "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                )
            )
        else:
            b = self.db.one(
                "SELECT * FROM bindings WHERE id=? AND user_id=? AND state='active'",
                (d["binding_id"], d["user_id"]),
            )
            skip = skip or not b or not s["agent_enabled"] if s else True
        if skip:
            self.db.execute(
                "UPDATE deliveries SET state='skipped',updated_at=? WHERE id=?",
                (time.time(), did),
            )
            return
        # Linearization point: authorization and transition are serialized with revocations.
        # Once this commits, the attempt is in flight; network I/O never holds the DB writer.
        with self.db.transaction() as db:
            fresh = db.execute(
                "SELECT * FROM subscriptions WHERE id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",
                (s["id"], time.time()),
            ).fetchone()
            allowed = bool(
                fresh
                and fresh[
                    "email_enabled" if d["channel"] == "email" else "agent_enabled"
                ]
            )
            if d["channel"] == "webhook":
                allowed = allowed and bool(
                    db.execute(
                        "SELECT id FROM bindings WHERE id=? AND state='active' AND user_id=?",
                        (d["binding_id"], d["user_id"]),
                    ).fetchone()
                )
            else:
                allowed = (
                    allowed
                    and not db.execute(
                        "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                    ).fetchone()
                )
            content = db.execute(
                "SELECT withdrawn,current_version FROM contents WHERE id=?",
                (d["content_id"],),
            ).fetchone()
            allowed = allowed and bool(
                content
                and (not content["withdrawn"] or d["kind"] == "withdrawn")
                and (not d["number"] or d["number"] == content["current_version"])
            )
            if not allowed:
                db.execute(
                    "UPDATE deliveries SET state='skipped',updated_at=? WHERE id=?",
                    (time.time(), did),
                )
                return
            db.execute(
                "UPDATE deliveries SET state='sending',attempts=attempts+1,updated_at=? WHERE id=?",
                (time.time(), did),
            )
            s = dict(fresh)
        if d["channel"] == "webhook":
            files = []
            if d["version_id"]:
                files = self.db.all(
                    "SELECT id,name,mime,size,sha256 FROM attachments WHERE version_id=?",
                    (d["version_id"],),
                )
            payload = {
                "event_id": d["event_id"],
                "type": d["kind"],
                "summary": d["summary"],
                "content_id": d["content_id"],
                "version": d["number"],
                "attachments": files,
            }
            provider = self.transport.webhook(
                b["endpoint"],
                payload,
                mac(self.cfg.secret, "hook:" + b["id"]),
                d["event_id"],
            )
        else:
            unsubscribe = (
                self.cfg.base_url
                + "/unsubscribe/"
                + action_token(self.cfg.secret, s["id"])
            )
            url = self.cfg.base_url + "/read/" + d["content_id"]
            text = (
                d["summary"]
                + "\n\n"
                + (
                    "此内容已撤回。"
                    if d["kind"] == "withdrawn"
                    else "验证邮箱后阅读正文和下载附件：" + url
                )
            )
            rich = None
            files = []
            if s["email_format"] == "full" and d["kind"] != "withdrawn":
                c = self.s.content(d["user_id"], d["content_id"], d["number"])
                text = c["body"]
                rich = c["html"] or None
                files = [
                    self.s.attachment(d["user_id"], x["id"]) for x in c["attachments"]
                ]
            text += (
                "\n\n退订内容邮件："
                + unsubscribe
                + "\n管理订阅："
                + self.cfg.base_url
                + "/app"
            )
            if rich:
                rich += (
                    '<p><a href="'
                    + html.escape(unsubscribe)
                    + '">退订内容邮件</a> · <a href="'
                    + html.escape(self.cfg.base_url + "/app")
                    + '">管理订阅</a></p>'
                )
            try:
                provider = self.transport.mail(
                    user["email"],
                    d["summary"],
                    text,
                    identity=did,
                    html=rich,
                    attachments=files,
                    unsubscribe=unsubscribe,
                    headers={
                        "X-Quant-Event-ID": d["event_id"],
                        "X-Quant-Content-ID": d["content_id"] or "",
                        "X-Quant-Version": str(d["number"] or ""),
                    },
                )
            except MailTooLarge:
                # No SMTP operation has occurred. Preserve the update and every
                # private attachment without repeatedly attempting an oversized MIME.
                provider = self.transport.mail(
                    user["email"],
                    d["summary"],
                    d["summary"]
                    + "\n\n本次邮件超过单封大小限制，请验证邮箱后阅读完整正文和下载全部附件："
                    + url
                    + "\n\n退订内容邮件："
                    + unsubscribe,
                    identity=did,
                    unsubscribe=unsubscribe,
                    headers={
                        "X-Quant-Event-ID": d["event_id"],
                        "X-Quant-Content-ID": d["content_id"] or "",
                        "X-Quant-Version": str(d["number"] or ""),
                    },
                )
        self.db.execute(
            "UPDATE deliveries SET state='accepted',provider_id=?,error=NULL,updated_at=? WHERE id=?",
            (provider, time.time(), did),
        )

    def deliver_account(self, d):
        with self.db.transaction() as db:
            sub = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
                (d["user_id"], d["product_id"]),
            ).fetchone()
            user = db.execute(
                "SELECT email FROM users WHERE id=?", (d["user_id"],)
            ).fetchone()
            binding = db.execute(
                "SELECT * FROM bindings WHERE id=? AND user_id=? AND state='active'",
                (d["binding_id"], d["user_id"]),
            ).fetchone()
            allowed = bool(
                sub
                and sub["email_enabled" if d["channel"] == "email" else "agent_enabled"]
            )
            if d["channel"] == "webhook":
                allowed = allowed and bool(binding)
            else:
                allowed = (
                    allowed
                    and not db.execute(
                        "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                    ).fetchone()
                )
            db.execute(
                "UPDATE deliveries SET state=?,attempts=attempts+1,updated_at=? WHERE id=?",
                ("sending" if allowed else "skipped", time.time(), d["id"]),
            )
            if not allowed:
                return
        if d["channel"] == "email":
            title, _, body = d["summary"].partition("\n")
            provider = self.transport.mail(
                user["email"],
                title,
                body + "\n" + self.cfg.base_url + "/app",
                identity=d["id"],
            )
        else:
            provider = self.transport.webhook(
                binding["endpoint"],
                {
                    "event_id": d["event_id"],
                    "type": d["kind"],
                    "summary": d["summary"],
                    "content_id": None,
                    "attachments": [],
                },
                mac(self.cfg.secret, "hook:" + binding["id"]),
                d["event_id"],
            )
        self.db.execute(
            "UPDATE deliveries SET state='accepted',provider_id=?,updated_at=? WHERE id=?",
            (provider, time.time(), d["id"]),
        )

    def admin_summary(self, identity):
        counts = self.db.all(
            "SELECT state,COUNT(*) count FROM deliveries GROUP BY state"
        )
        incidents = self.db.all(
            "SELECT kind,summary,count FROM incidents WHERE state='open' LIMIT 15"
        )
        source = self.db.all(
            "SELECT key,updated_at FROM source_state WHERE key IN ('job:official_mail','job:picks','job:weekly_digest','picks:last_success')"
        )
        expiring = self.db.all(
            "SELECT u.email,s.product_id,s.expires_at FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.expires_at BETWEEN ? AND ? ORDER BY s.expires_at",
            (time.time(), time.time() + 14 * 86400),
        )
        from .health import check

        body = (
            "Hadan 运行日报\n"
            + json.dumps(
                {
                    "health": check(self.s),
                    "deliveries": counts,
                    "incidents": incidents,
                    "source_last_success": source,
                    "expiring_within_14_days": expiring,
                    "expiring_within_7_days": sum(
                        x["expires_at"] < time.time() + 7 * 86400 for x in expiring
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
            + self.cfg.base_url
            + "/admin"
        )
        for email in self.cfg.admin_emails:
            self.transport.mail(
                email, "Quant · 运行日报", body, identity=identity + digest(email)[:12]
            )

    def run(self):
        def stop(*_):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        next_schedule = 0
        while not self.stopping:
            if self.queue == "main" and time.time() >= next_schedule:
                from .schedule import tick

                tick(self.s)
                next_schedule = time.time() + 30
            if not self.run_once():
                time.sleep(1)
