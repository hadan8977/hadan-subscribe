"""Explicit, durable agreement before any external payment operation."""
import json
import time
import re

from .db import audit, uid
from .i18n import STRINGS
from .security import digest
from .service import Problem

TERMS_VERSION = "2026-10-28"


def require_agreement(payload):
    if payload.get("accept_terms") is not True or payload.get("terms_version") != TERMS_VERSION:
        raise Problem("请先阅读并同意订阅条款与退款规则", 400)


def begin_agreement(service, user_id, operation, payload):
    """Persist first, so a provider timeout/crash cannot leave an unrecorded checkout."""
    require_agreement(payload)
    fields = ("product_id",) if operation == "checkout" else ("plan_id",)
    if any(not isinstance(payload.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", payload[key]) for key in fields):
        raise Problem("订阅方案不存在", 400)
    if operation == "purchase" and payload.get("method") not in ("wechat", "alipay"):
        raise Problem("该付款方式尚未开放", 400)
    identity = (digest(f"checkout:{user_id}:{payload.get('product_id')}:{TERMS_VERSION}:{int(time.time() // 1800)}")
                if operation == "checkout" else uid())
    with service.db.transaction() as db:
        service._limit(db, "terms:" + user_id, 16, 600)
        if not db.execute("SELECT id FROM audit WHERE actor=? AND action='terms_accepted' AND object_id=?", (user_id, identity)).fetchone():
            audit(db, user_id, "terms_accepted", identity, {
                "version": TERMS_VERSION, "operation": operation,
                "product_id": payload["product_id"] if operation == "checkout" else "quantgt",
                "plan_id": payload.get("plan_id") if operation == "purchase" else None,
                "method": payload.get("method") if operation == "purchase" else None,
                "refund_policy": {lang: words["refunds_body"] for lang, words in STRINGS.items()},
                "investment_notice": {lang: words["investment_body"] for lang, words in STRINGS.items()},
            })
    return identity


def link_agreement(db, user_id, agreement_id, provider_object):
    """Keep the first association. Renewals and callbacks never replace its text."""
    if not agreement_id:
        return  # Trusted internal calls have no browser checkout; public routes always pass it.
    if not db.execute("SELECT id FROM audit WHERE actor=? AND action='terms_accepted' AND object_id=?", (user_id, agreement_id)).fetchone():
        raise Problem("请先阅读并同意订阅条款与退款规则", 400)
    previous = db.execute("SELECT details FROM audit WHERE actor=? AND action='payment_terms_linked' AND object_id=? ORDER BY id LIMIT 1", (user_id, provider_object)).fetchone()
    if previous:
        return json.loads(previous[0])["agreement_id"]
    audit(db, user_id, "payment_terms_linked", str(provider_object), {"agreement_id": agreement_id})
    return agreement_id
