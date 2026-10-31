"""Admin MCP server for the Quant subscription database.

This server is intentionally separate from the public web app and from the
agent-facing content MCP. It exposes a small, explicit member-management
surface for the operator:

* read: members, one member, deliveries, suppressions, allowlist
* write: add/extend/set expiry, enable/disable email or agent delivery,
  soft-remove, add/remove login allowlist, suppress/unsuppress email

Every write requires ``confirm=true`` and is recorded in the audit table.
There is no arbitrary SQL, filesystem, payment, or send-email tool here.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from .cli import grant
from .config import Settings
from .db import audit
from .security import email_address
from .service import Problem, Service

INSTRUCTIONS = """\
This server manages the Quant subscription database (members/users, access
allowlist, email consent, expiry, suppressions and delivery history).

Safety rules:
- Every write tool requires confirm=true. Without it, the tool returns a
  dry-run preview and writes nothing.
- add_member creates a user, a subscription and a login allowlist entry.
  email_consent defaults to false: an added member can sign in but will not
  receive content email until you explicitly enable it.
- extend_member adds time to the later of now and the current expiry.
- remove_member is a soft removal: it ends the subscription, disables email
  and agent delivery, revokes sessions/bindings and removes the login
  allowlist entry. User, subscription and audit history stay in the database.
- Suppression is for deliverability/complaint handling; it does not by itself
  change subscription status.
- Use list_members/get_member before and after any write.
"""

server = MCPServer(name="hadan-admin", instructions=INSTRUCTIONS)
_service_instance = None


def _service() -> Service:
    global _service_instance
    if _service_instance is None:
        cfg = Settings()
        # The admin MCP does not send mail and must not require SMTP secrets.
        cfg.smtp_required = False
        svc = Service(cfg)
        svc.seed()
        _service_instance = svc
    return _service_instance


def _ok(data) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(data, ensure_ascii=False, indent=2, default=str),
            )
        ],
        is_error=False,
    )


def _err(code: str, message: str, details=None) -> CallToolResult:
    payload = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            )
        ],
        is_error=True,
    )


def _call(fn, /, *args, **kwargs) -> CallToolResult:
    try:
        return _ok(fn(*args, **kwargs))
    except Problem as exc:
        return _err("problem", exc.message, {"status": exc.status})
    except ValueError as exc:
        return _err("invalid_argument", str(exc))
    except Exception as exc:  # never leak a traceback to the MCP client
        return _err(
            "internal_error",
            str(exc),
            {"type": type(exc).__name__},
        )


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _parse_until(until: str):
    try:
        value = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("until must be an ISO date/datetime") from None
    if value.tzinfo is None:
        raise ValueError("until must include a timezone")
    return value.timestamp(), value.astimezone(timezone.utc).isoformat()


def _resolve_expiry(days, until):
    if until is not None:
        if days is not None:
            raise ValueError("provide days or until, not both")
        return _parse_until(until)
    if days is None:
        days = 30
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
        raise ValueError("days must be an integer between 1 and 3650")
    ts = time.time() + days * 86400
    return ts, _iso(ts)


def _member_email(email: str) -> str:
    return email_address(email)


def _user(s, email):
    return s.db.one("SELECT * FROM users WHERE email=?", (email,))


def _subscription(s, email, product_id):
    return s.db.one(
        "SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id "
        "WHERE u.email=? AND s.product_id=?",
        (email, product_id),
    )


def _all_subscriptions(s, email):
    rows = s.db.all(
        "SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id "
        "WHERE u.email=? ORDER BY s.product_id",
        (email,),
    )
    for row in rows:
        row["expires_at_iso"] = _iso(row.get("expires_at"))
    return rows


def _public_member(s, email):
    user = _user(s, email)
    if not user:
        raise ValueError("member not found")
    allow = s.db.one("SELECT email,note,created_at FROM access_allowlist WHERE email=?", (email,))
    suppression = s.db.one("SELECT email,reason,created_at FROM suppressions WHERE email=?", (email,))
    deliveries = s.db.all(
        "SELECT d.id,d.channel,d.state,d.provider_id,d.error,d.created_at,d.updated_at "
        "FROM deliveries d JOIN users u ON u.id=d.user_id WHERE u.email=? "
        "ORDER BY d.created_at DESC LIMIT 10",
        (email,),
    )
    for row in deliveries:
        row["created_at_iso"] = _iso(row.get("created_at"))
        row["updated_at_iso"] = _iso(row.get("updated_at"))
    return {
        "email": user["email"],
        "user_id": user["id"],
        "created_at": _iso(user["created_at"]),
        "subscriptions": _all_subscriptions(s, email),
        "allowlist": (
            {
                "email": allow["email"],
                "note": allow["note"],
                "created_at": _iso(allow["created_at"]),
            }
            if allow
            else None
        ),
        "suppression": (
            {
                "email": suppression["email"],
                "reason": suppression["reason"],
                "created_at": _iso(suppression["created_at"]),
            }
            if suppression
            else None
        ),
        "recent_deliveries": deliveries,
        "bindings": s.db.all(
            "SELECT id,name,mode,state,created_at,last_seen FROM bindings WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ),
    }


def _preview_subscription(row):
    if not row:
        return None
    return {
        "subscription_id": row["id"],
        "product_id": row["product_id"],
        "status": row["status"],
        "expires_at": _iso(row["expires_at"]),
        "email_enabled": bool(row["email_enabled"]),
        "agent_enabled": bool(row["agent_enabled"]),
    }


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@server.tool()
def list_members(
    query: str | None = None,
    status: str | None = None,
    expiring_within_days: int | None = None,
) -> CallToolResult:
    """List members and subscriptions.

    query: case-insensitive substring match on email.
    status: active, ended, canceled/cancelled, expired; omit for all.
    expiring_within_days: only subscriptions expiring between now and N days.
    """
    def run():
        s = _service()
        rows = s.db.all(
            "SELECT u.email,u.created_at,s.id subscription_id,s.product_id,s.status,"
            "s.expires_at,s.email_enabled,s.agent_enabled,s.email_format,"
            "CASE WHEN a.email IS NULL THEN 0 ELSE 1 END allowlisted "
            "FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id "
            "LEFT JOIN access_allowlist a ON a.email=u.email ORDER BY u.email,s.product_id"
        )
        now = time.time()
        out = []
        for row in rows:
            if query and query.lower() not in row["email"].lower():
                continue
            if status and (row["status"] or "").lower() != status.lower():
                continue
            if expiring_within_days is not None:
                if row["expires_at"] is None:
                    continue
                if not (now <= row["expires_at"] <= now + expiring_within_days * 86400):
                    continue
            item = dict(row)
            item["created_at"] = _iso(item["created_at"])
            item["expires_at"] = _iso(item["expires_at"])
            out.append(item)
        return {"count": len(out), "members": out}

    return _call(run)


@server.tool()
def get_member(email: str) -> CallToolResult:
    """Return one member's subscriptions, allowlist, suppression, bindings and recent deliveries."""
    return _call(lambda: _public_member(_service(), _member_email(email)))


@server.tool()
def list_deliveries(email: str | None = None, limit: int = 20) -> CallToolResult:
    """Recent delivery records, optionally filtered by member email."""
    def run():
        effective_limit = max(1, min(int(limit), 200))
        s = _service()
        if email:
            addr = _member_email(email)
            rows = s.db.all(
                "SELECT d.id,u.email,d.channel,d.state,d.provider_id,d.error,d.created_at,d.updated_at "
                "FROM deliveries d JOIN users u ON u.id=d.user_id WHERE u.email=? "
                "ORDER BY d.created_at DESC LIMIT ?",
                (addr, effective_limit),
            )
        else:
            rows = s.db.all(
                "SELECT d.id,u.email,d.channel,d.state,d.provider_id,d.error,d.created_at,d.updated_at "
                "FROM deliveries d JOIN users u ON u.id=d.user_id ORDER BY d.created_at DESC LIMIT ?",
                (effective_limit,),
            )
        for row in rows:
            row["created_at_iso"] = _iso(row["created_at"])
            row["updated_at_iso"] = _iso(row["updated_at"])
        return {"count": len(rows), "deliveries": rows}

    return _call(run)


@server.tool()
def list_suppressions() -> CallToolResult:
    """List all local email suppressions (bounce, complaint or manual)."""
    def run():
        rows = _service().db.all("SELECT email,reason,created_at FROM suppressions ORDER BY created_at DESC")
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
        return {"count": len(rows), "suppressions": rows}

    return _call(run)


@server.tool()
def list_allowlist() -> CallToolResult:
    """List the database-backed login allowlist (in addition to the env allowlist)."""
    def run():
        rows = _service().db.all("SELECT email,note,created_at FROM access_allowlist ORDER BY email")
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
        return {"count": len(rows), "allowlist": rows}

    return _call(run)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@server.tool()
def add_member(
    email: str,
    days: int | None = 30,
    until: str | None = None,
    product_id: str = "quantgt",
    email_consent: bool = False,
    note: str | None = None,
    confirm: bool = False,
) -> CallToolResult:
    """Create a member, subscription and login allowlist entry.

    confirm=false returns a preview and writes nothing.
    email_consent=false means the member can sign in but receives no content
    email until set_email_enabled is used.
    """
    def run():
        addr = _member_email(email)
        expiry, expiry_iso = _resolve_expiry(days, until)
        s = _service()
        if not s.db.one("SELECT id FROM products WHERE id=?", (product_id,)):
            raise ValueError("unknown product")
        existing = _subscription(s, addr, product_id)
        preview = {
            "action": "add_member",
            "email": addr,
            "product_id": product_id,
            "expires_at": expiry_iso,
            "email_consent": bool(email_consent),
            "existing_subscription": _preview_subscription(existing),
            "note": (note or "")[:500],
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        result = grant(
            s,
            addr,
            product_id,
            expiry_iso,
            email_consent=bool(email_consent),
            actor="mcp",
        )
        with s.db.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO access_allowlist(email,note,created_at) VALUES(?,?,?)",
                (addr, (note or "mcp add")[:500], time.time()),
            )
            db.execute(
                "UPDATE access_allowlist SET note=? WHERE email=?",
                ((note or "mcp add")[:500], addr),
            )
            audit(
                db,
                "mcp",
                "allowlist_add",
                addr,
                {"product_id": product_id, "note": (note or "")[:500]},
            )
        return {"written": True, **preview, "grant": result}

    return _call(run)


@server.tool()
def extend_member(
    email: str,
    days: int | None = 30,
    until: str | None = None,
    product_id: str = "quantgt",
    note: str | None = None,
    confirm: bool = False,
) -> CallToolResult:
    """Extend a subscription from the later of now and its current expiry."""
    def run():
        addr = _member_email(email)
        s = _service()
        row = _subscription(s, addr, product_id)
        if not row:
            raise ValueError("member/subscription not found; use add_member")
        if until is not None:
            new_ts, new_iso = _parse_until(until)
        else:
            extend_days = 30 if days is None else days
            if (
                not isinstance(extend_days, int)
                or isinstance(extend_days, bool)
                or not 1 <= extend_days <= 3650
            ):
                raise ValueError("days must be an integer between 1 and 3650")
            base = max(time.time(), float(row["expires_at"] or 0))
            new_ts = base + extend_days * 86400
            new_iso = _iso(new_ts)
        preview = {
            "action": "extend_member",
            "email": addr,
            "product_id": product_id,
            "old_expires_at": _iso(row["expires_at"]),
            "new_expires_at": new_iso,
            "note": (note or "")[:500],
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        with s.db.transaction() as db:
            db.execute(
                "UPDATE subscriptions SET status='active',expires_at=? WHERE id=?",
                (new_ts, row["id"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO access_allowlist(email,note,created_at) VALUES(?,?,?)",
                (addr, (note or "mcp extend")[:500], time.time()),
            )
            audit(
                db,
                "mcp",
                "extend",
                row["id"],
                {"expires_at": new_iso, "note": (note or "")[:500]},
            )
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def set_expiry(
    email: str,
    until: str,
    product_id: str = "quantgt",
    confirm: bool = False,
) -> CallToolResult:
    """Set an absolute, timezone-aware subscription expiry."""
    def run():
        addr = _member_email(email)
        new_ts, new_iso = _parse_until(until)
        s = _service()
        row = _subscription(s, addr, product_id)
        if not row:
            raise ValueError("member/subscription not found")
        preview = {
            "action": "set_expiry",
            "email": addr,
            "product_id": product_id,
            "old_expires_at": _iso(row["expires_at"]),
            "new_expires_at": new_iso,
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        with s.db.transaction() as db:
            db.execute(
                "UPDATE subscriptions SET expires_at=? WHERE id=?",
                (new_ts, row["id"]),
            )
            audit(db, "mcp", "set_expiry", row["id"], {"expires_at": new_iso})
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def set_email_enabled(
    email: str,
    enabled: bool,
    product_id: str = "quantgt",
    confirm: bool = False,
) -> CallToolResult:
    """Enable or disable content email for one subscription."""
    def run():
        addr = _member_email(email)
        s = _service()
        row = _subscription(s, addr, product_id)
        if not row:
            raise ValueError("member/subscription not found")
        preview = {
            "action": "set_email_enabled",
            "email": addr,
            "product_id": product_id,
            "old": bool(row["email_enabled"]),
            "new": bool(enabled),
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        with s.db.transaction() as db:
            db.execute(
                "UPDATE subscriptions SET email_enabled=? WHERE id=?",
                (int(bool(enabled)), row["id"]),
            )
            audit(db, "mcp", "set_email_enabled", row["id"], {"enabled": bool(enabled)})
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def set_agent_enabled(
    email: str,
    enabled: bool,
    product_id: str = "quantgt",
    confirm: bool = False,
) -> CallToolResult:
    """Enable or disable the member's agent channel for one subscription."""
    def run():
        addr = _member_email(email)
        s = _service()
        row = _subscription(s, addr, product_id)
        if not row:
            raise ValueError("member/subscription not found")
        preview = {
            "action": "set_agent_enabled",
            "email": addr,
            "product_id": product_id,
            "old": bool(row["agent_enabled"]),
            "new": bool(enabled),
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        with s.db.transaction() as db:
            db.execute(
                "UPDATE subscriptions SET agent_enabled=? WHERE id=?",
                (int(bool(enabled)), row["id"]),
            )
            audit(db, "mcp", "set_agent_enabled", row["id"], {"enabled": bool(enabled)})
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def remove_member(
    email: str,
    product_id: str = "quantgt",
    confirm: bool = False,
) -> CallToolResult:
    """Soft-remove a member: end subscription, disable channels, revoke sessions/bindings, remove login allowlist."""
    def run():
        addr = _member_email(email)
        s = _service()
        row = _subscription(s, addr, product_id)
        if not row:
            raise ValueError("member/subscription not found")
        user = _user(s, addr)
        preview = {
            "action": "remove_member",
            "email": addr,
            "product_id": product_id,
            "subscription": _preview_subscription(row),
            "effects": [
                "subscription.status=ended",
                "email_enabled=0",
                "agent_enabled=0",
                "sessions revoked",
                "bindings revoked",
                "login allowlist entry removed",
            ],
        }
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        with s.db.transaction() as db:
            db.execute(
                "UPDATE subscriptions SET status='ended',email_enabled=0,agent_enabled=0 WHERE id=?",
                (row["id"],),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            db.execute(
                "UPDATE bindings SET state='revoked' WHERE user_id=? AND state IN ('pending','active')",
                (user["id"],),
            )
            db.execute("DELETE FROM access_allowlist WHERE email=?", (addr,))
            audit(
                db,
                "mcp",
                "remove_member",
                row["id"],
                {"email": addr, "product_id": product_id},
            )
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def add_allowlist(
    email: str,
    note: str | None = None,
    confirm: bool = False,
) -> CallToolResult:
    """Allow an email to sign in without creating a subscription."""
    def run():
        addr = _member_email(email)
        preview = {"action": "add_allowlist", "email": addr, "note": (note or "")[:500]}
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        s = _service()
        with s.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO access_allowlist(email,note,created_at) VALUES(?,?,?)",
                (addr, (note or "mcp allowlist")[:500], time.time()),
            )
            audit(db, "mcp", "allowlist_add", addr, {"note": (note or "")[:500]})
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def remove_allowlist(email: str, confirm: bool = False) -> CallToolResult:
    """Remove a database allowlist entry; an env allowlist entry still grants access."""
    def run():
        addr = _member_email(email)
        preview = {"action": "remove_allowlist", "email": addr}
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        s = _service()
        with s.db.transaction() as db:
            count = db.execute("DELETE FROM access_allowlist WHERE email=?", (addr,)).rowcount
            audit(db, "mcp", "allowlist_remove", addr, {"deleted": count})
        return {
            "written": True,
            **preview,
            "deleted": count,
            "warning": "The env HADAN_ALLOWED_EMAILS list is not changed.",
        }

    return _call(run)


@server.tool()
def suppress_email(
    email: str,
    reason: str = "manual",
    confirm: bool = False,
) -> CallToolResult:
    """Add an email to the local suppression table (bounce/complaint/manual)."""
    def run():
        addr = _member_email(email)
        if reason not in ("bounce", "complaint", "manual"):
            raise ValueError("reason must be bounce, complaint or manual")
        preview = {"action": "suppress_email", "email": addr, "reason": reason}
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        s = _service()
        with s.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO suppressions(email,reason,created_at) VALUES(?,?,?)",
                (addr, reason, time.time()),
            )
            audit(db, "mcp", "suppress", addr, {"reason": reason})
        return {"written": True, **preview}

    return _call(run)


@server.tool()
def unsuppress_email(email: str, confirm: bool = False) -> CallToolResult:
    """Remove an email from the local suppression table."""
    def run():
        addr = _member_email(email)
        preview = {"action": "unsuppress_email", "email": addr}
        if not confirm:
            return {"dry_run": True, "would_write": preview}
        s = _service()
        with s.db.transaction() as db:
            count = db.execute("DELETE FROM suppressions WHERE email=?", (addr,)).rowcount
            audit(db, "mcp", "unsuppress", addr, {"deleted": count})
        return {"written": True, **preview, "deleted": count}

    return _call(run)


def main():
    parser = argparse.ArgumentParser(description="Quant admin MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    if args.transport == "http":
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
