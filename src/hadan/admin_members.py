"""Small, audited member-management service shared by the admin web UI.

The web UI deliberately supports only individual operations. Bulk changes and
hard deletion stay out of scope so an administrator cannot accidentally alter
many subscriptions or destroy payment/history records.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .cli import grant
from .db import audit
from .security import email_address
from .service import Problem

PRODUCT = "quantgt"


def _resolve_expiry(days, until, now):
    if until:
        if days is not None:
            raise Problem("期限和到期日只能选择一个", 400)
        try:
            value = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise Problem("到期日格式无效", 400) from None
        if value.tzinfo is None:
            raise Problem("到期日必须包含时区", 400)
        ts = value.timestamp()
        if ts <= now:
            raise Problem("到期日必须晚于当前时间", 400)
        return ts
    value = 30 if days is None else days
    if type(value) is not int or not 1 <= value <= 3650:
        raise Problem("期限必须是 1-3650 天", 400)
    return now + value * 86400


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _subscription(s, user_id):
    return s.db.one(
        "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
        (user_id, PRODUCT),
    )


def _status(sub, now):
    if not sub:
        return "none"
    if sub["status"] == "ended":
        return "ended"
    if sub["status"] == "pending":
        return "pending"
    if sub["status"] == "active" and sub["expires_at"] is not None and sub["expires_at"] <= now:
        return "expired"
    return "active"


def counts(s):
    now = time.time()
    return {
        "users": s.db.one("SELECT COUNT(*) n FROM users")["n"],
        "active": s.db.one(
            "SELECT COUNT(*) n FROM subscriptions WHERE product_id=? AND status='active' "
            "AND (expires_at IS NULL OR expires_at>?)",
            (PRODUCT, now),
        )["n"],
        "expiring_7d": s.db.one(
            "SELECT COUNT(*) n FROM subscriptions WHERE product_id=? AND status='active' "
            "AND expires_at>? AND expires_at<=?",
            (PRODUCT, now, now + 7 * 86400),
        )["n"],
        "email_enabled": s.db.one(
            "SELECT COUNT(*) n FROM subscriptions WHERE product_id=? AND status='active' "
            "AND (expires_at IS NULL OR expires_at>?) AND email_enabled=1",
            (PRODUCT, now),
        )["n"],
        "agent_enabled": s.db.one(
            "SELECT COUNT(*) n FROM subscriptions WHERE product_id=? AND status='active' "
            "AND (expires_at IS NULL OR expires_at>?) AND agent_enabled=1",
            (PRODUCT, now),
        )["n"],
    }


def list_members(s, query="", status="", limit=100):
    now = time.time()
    rows = s.db.all(
        "SELECT u.id user_id,u.email,u.created_at user_created,s.id subscription_id,"
        "s.status,s.expires_at,s.email_enabled,s.agent_enabled,s.created_at subscription_created "
        "FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id AND s.product_id=? "
        "ORDER BY u.created_at DESC",
        (PRODUCT,),
    )
    needle = str(query or "").strip().lower()
    wanted = str(status or "all").strip().lower()
    result = []
    for row in rows:
        state = _status(row if row["subscription_id"] else None, now)
        if needle and needle not in row["email"].lower():
            continue
        if wanted not in ("", "all") and state != wanted:
            continue
        result.append(
            {
                "email": row["email"],
                "user_id": row["user_id"],
                "subscription_id": row["subscription_id"],
                "status": state,
                "expires_at": row["expires_at"],
                "created_at": row["subscription_created"],
                "email_enabled": bool(row["email_enabled"]),
                "agent_enabled": bool(row["agent_enabled"]),
            }
        )
    return {"counts": counts(s), "members": result[: max(1, min(int(limit), 200))]}


def _target(s, email):
    addr = email_address(email)
    user = s.db.one("SELECT * FROM users WHERE email=?", (addr,))
    return addr, user, (_subscription(s, user["id"]) if user else None)


def preview_add(s, email, days=30, until=None, email_consent=False, note=None):
    now = time.time()
    if type(email_consent) is not bool:
        raise Problem("邮件接收设置必须是布尔值", 400)
    addr, user, sub = _target(s, email)
    if sub:
        raise Problem("该邮箱已有订阅，请使用延期", 409)
    expires = _resolve_expiry(days, until, now)
    return {
        "action": "add",
        "email": addr,
        "product_id": PRODUCT,
        "expires_at": expires,
        "expires_at_iso": _iso(expires),
        "email_enabled": bool(email_consent),
        "agent_enabled": False,
        "existing_user": bool(user),
        "note": str(note or "")[:500],
    }


def add(s, email, days=30, until=None, email_consent=False, note=None, actor="admin"):
    preview = preview_add(s, email, days, until, email_consent, note)
    result = grant(
        s,
        preview["email"],
        PRODUCT,
        preview["expires_at_iso"],
        email_consent=preview["email_enabled"],
        actor=actor,
    )
    with s.db.transaction() as db:
        db.execute(
            "UPDATE access_allowlist SET note=? WHERE email=?",
            (preview["note"] or "admin add", preview["email"]),
        )
        audit(db, actor, "admin_member_add", preview["email"], {
            "expires_at": preview["expires_at_iso"],
            "email_enabled": preview["email_enabled"],
            "existing_user": preview["existing_user"],
        })
    return {
        "written": True,
        **preview,
        "email_enabled": bool(result.get("email_consent", preview["email_enabled"])),
    }


def preview_extend(s, email, days=30, until=None):
    now = time.time()
    addr, user, sub = _target(s, email)
    if not user or not sub:
        raise Problem("该邮箱没有可延期的订阅", 404)
    if sub["expires_at"] is None:
        raise Problem("当前订阅无到期日，无需延期", 409)
    if until:
        expires = _resolve_expiry(None, until, now)
        if sub["expires_at"] is not None and expires <= sub["expires_at"]:
            raise Problem("新的到期日必须晚于当前到期日", 400)
    else:
        base = max(now, float(sub["expires_at"] or 0))
        expires = base + _resolve_expiry(days, None, now) - now
    return {
        "action": "extend",
        "email": addr,
        "product_id": PRODUCT,
        "old_expires_at": sub["expires_at"],
        "expires_at": expires,
        "expires_at_iso": _iso(expires),
    }


def extend(s, email, days=30, until=None, actor="admin"):
    preview = preview_extend(s, email, days, until)
    addr, user, sub = _target(s, email)
    with s.db.transaction() as db:
        db.execute(
            "UPDATE subscriptions SET status='active',expires_at=? WHERE id=?",
            (preview["expires_at"], sub["id"]),
        )
        db.execute(
            "INSERT OR IGNORE INTO access_allowlist(email,note,created_at) VALUES(?,?,?)",
            (addr, "admin extend", time.time()),
        )
        audit(db, actor, "admin_member_extend", sub["id"], {
            "email": addr,
            "old_expires_at": preview["old_expires_at"],
            "expires_at": preview["expires_at_iso"],
        })
    return {"written": True, **preview}


def preview_remove(s, email):
    addr, user, sub = _target(s, email)
    if not user or not sub:
        raise Problem("该邮箱没有可移除的订阅", 404)
    return {
        "action": "remove",
        "email": addr,
        "product_id": PRODUCT,
        "subscription_status": sub["status"],
        "expires_at": sub["expires_at"],
        "effects": [
            "subscription.status=ended",
            "email_enabled=0",
            "agent_enabled=0",
            "sessions revoked",
            "agent bindings revoked",
            "login allowlist entry removed",
            "history/payment records retained",
        ],
    }


def remove(s, email, actor="admin"):
    preview = preview_remove(s, email)
    addr, user, sub = _target(s, email)
    with s.db.transaction() as db:
        db.execute(
            "UPDATE subscriptions SET status='ended',email_enabled=0,agent_enabled=0 WHERE id=?",
            (sub["id"],),
        )
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        db.execute(
            "UPDATE bindings SET state='revoked' WHERE user_id=? AND state IN ('pending','active')",
            (user["id"],),
        )
        db.execute(
            "UPDATE mcp_event_subscriptions SET active=0,updated_at=? WHERE user_id=?",
            (time.time(), user["id"]),
        )
        db.execute("DELETE FROM access_allowlist WHERE email=?", (addr,))
        audit(db, actor, "admin_member_remove", sub["id"], {"email": addr})
    return {"written": True, **preview}
