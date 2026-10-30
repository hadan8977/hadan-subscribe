"""Alibaba Cloud DirectMail delivery feedback over EventBridge HTTPS.

DirectMail publishes CloudEvents-style envelopes through EventBridge. The
HTTP target is configured with EventBridge's fixed token header
``x-eventbridge-signature-token``; the token is the shared secret between the
EventBridge target and this endpoint. Only a verified event may change local
suppression state.

Event types:
- dm:Deliver:Fail   status 2 = invalid address -> suppress as bounce
                    status 3 = recipient marked spam -> suppress as complaint
                    status 4/other -> incident only
- dm:Feedback:FblReport -> suppress the complained mailbox
- dm:Deliver:Succeed -> record only
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from .db import audit, uid
from .security import email_address
from .service import Problem

MAX_BODY = 256 * 1024
FEEDBACK_HEADER = "x-eventbridge-signature-token"
SOURCES = {"acs:dm", "acs.dm", "acs.directmail"}
FAIL = "dm:Deliver:Fail"
FBL = "dm:Feedback:FblReport"
SUCCEED = "dm:Deliver:Succeed"
ALLOWED_TYPES = {FAIL, FBL, SUCCEED}


class DirectMailFeedback:
    def __init__(self, service):
        self.s = service
        self.cfg = service.settings

    def handle(self, raw, headers=None):
        if not self.cfg.directmail_feedback_enabled:
            raise Problem("邮件反馈通道未启用", 503)
        if len(raw) > MAX_BODY:
            raise Problem("反馈通知过大", 413)
        supplied = ""
        if headers is not None:
            supplied = headers.get(FEEDBACK_HEADER, "") or ""
        if not self.cfg.directmail_webhook_token or not hmac.compare_digest(
            supplied, self.cfg.directmail_webhook_token
        ):
            raise Problem("反馈令牌无效", 403)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Problem("反馈通知无效", 400) from None
        if not isinstance(value, dict):
            raise Problem("反馈通知无效", 400)
        if value.get("source") not in SOURCES:
            raise Problem("反馈来源不匹配", 403)
        kind = value.get("type")
        if kind not in ALLOWED_TYPES:
            raise Problem("反馈类型不支持", 400)
        event_id = value.get("id")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            raise Problem("反馈事件 ID 无效", 400)
        data = value.get("data")
        if not isinstance(data, dict):
            raise Problem("反馈通知无效", 400)
        return self._process(value, data)

    def _process(self, value, data):
        kind = value["type"]
        event_id = value["id"]
        digest = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        now = time.time()
        if kind == FBL:
            event_type = "Complaint"
            recipient = str(data.get("block_email", ""))
            message_id = str(data.get("message_id", ""))[:256]
            status = ""
        elif kind == FAIL:
            event_type = "Bounce"
            recipient = str(data.get("rcpt", ""))
            message_id = str(data.get("msg_id") or data.get("env_id") or "")[:256]
            status = str(data.get("status", ""))
        else:
            event_type = "Delivery"
            recipient = str(data.get("rcpt", ""))
            message_id = str(data.get("msg_id") or data.get("env_id") or "")[:256]
            status = str(data.get("status", ""))

        detail = json.dumps(
            {
                key: data.get(key)
                for key in (
                    "failed_type",
                    "err_code",
                    "err_msg",
                    "fbl_isp",
                    "fingerprint",
                    "send_time",
                    "deliver_time",
                    "block_time",
                )
                if data.get(key) is not None
            },
            ensure_ascii=False,
        )
        suppressed = []
        with self.s.db.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO directmail_feedback"
                "(id,event_type,message_id,recipient,status,detail,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    event_id,
                    event_type,
                    message_id,
                    recipient[:320],
                    status[:32],
                    detail,
                    now,
                ),
            ).rowcount
            if not inserted:
                return {"received": True, "duplicate": True}
            if kind == FBL:
                if self._suppress(db, recipient, "complaint", now):
                    suppressed.append(recipient.lower().strip())
                self._incident(
                    db,
                    "DirectMail FBL complaint",
                    event_id,
                    recipient,
                    detail,
                    now,
                )
            elif kind == FAIL:
                if status == "2":
                    if self._suppress(db, recipient, "bounce", now):
                        suppressed.append(recipient.lower().strip())
                elif status == "3":
                    if self._suppress(db, recipient, "complaint", now):
                        suppressed.append(recipient.lower().strip())
                    self._incident(
                        db,
                        "DirectMail spam complaint",
                        event_id,
                        recipient,
                        detail,
                        now,
                    )
                else:
                    self._incident(
                        db,
                        "DirectMail delivery failure",
                        event_id,
                        recipient,
                        detail,
                        now,
                    )
            audit(
                db,
                "directmail",
                "feedback_" + event_type.lower(),
                event_id,
                {
                    "message_id": message_id,
                    "recipient": recipient,
                    "status": status,
                    "suppressed": suppressed,
                },
            )
        return {
            "received": True,
            "event": event_type,
            "status": status,
            "suppressed": len(suppressed),
        }

    def _suppress(self, db, address, reason, now):
        try:
            address = email_address(address)
        except ValueError:
            return False
        db.execute(
            "INSERT OR REPLACE INTO suppressions(email,reason,created_at) VALUES(?,?,?)",
            (address, reason, now),
        )
        return True

    def _incident(self, db, summary, event_id, recipient, detail, now):
        fingerprint = hashlib.sha256(
            ("directmail-feedback:" + summary + ":" + event_id).encode("utf-8")
        ).hexdigest()
        existing = db.execute(
            "SELECT id FROM incidents WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE incidents SET state='open',count=count+1,updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
            return
        db.execute(
            "INSERT INTO incidents(id,fingerprint,kind,summary,details,created_at,updated_at) "
            "VALUES(?,?,'directmail_feedback',?,?,?,?)",
            (
                uid(),
                fingerprint,
                summary,
                json.dumps(
                    {"event_id": event_id, "recipient": recipient, "detail": detail},
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
