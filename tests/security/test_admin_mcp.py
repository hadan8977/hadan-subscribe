"""Admin MCP tools: wiring, dry-run defaults and member lifecycle."""
import asyncio
import json

import pytest

from hadan import mcp_server
from hadan.config import Settings
from hadan.service import Problem, Service


def payload(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def admin(tmp_path, monkeypatch):
    svc = Service(
        Settings(
            data_dir=tmp_path,
            secret="s" * 64,
            admin_emails=("admin@example.com",),
            mail_backend="file",
            allowed_emails=("invited@example.com",),
        )
    )
    svc.seed()
    monkeypatch.setattr(mcp_server, "_service_instance", svc)
    return svc


def test_tool_surface_and_descriptions():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert {
        "list_members",
        "get_member",
        "list_deliveries",
        "list_suppressions",
        "list_allowlist",
        "add_member",
        "extend_member",
        "set_expiry",
        "set_email_enabled",
        "set_agent_enabled",
        "remove_member",
        "add_allowlist",
        "remove_allowlist",
        "suppress_email",
        "unsuppress_email",
    } <= set(tools)
    assert "confirm=true" in mcp_server.INSTRUCTIONS


def test_add_member_dry_run_then_confirm_and_allowlist_login(admin):
    dry = payload(mcp_server.add_member("new@example.com", days=30, email_consent=True))
    assert dry["dry_run"] is True
    assert admin.db.one("SELECT id FROM users WHERE email='new@example.com'") is None

    result = payload(
        mcp_server.add_member(
            "new@example.com", days=30, email_consent=True, note="manual", confirm=True
        )
    )
    assert result["written"] is True
    assert result["expires_at"]
    user = admin.db.one("SELECT * FROM users WHERE email='new@example.com'")
    sub = admin.db.one("SELECT * FROM subscriptions WHERE user_id=?", (user["id"],))
    assert sub["status"] == "active"
    assert sub["email_enabled"] == 1
    assert admin.db.one(
        "SELECT email FROM access_allowlist WHERE email='new@example.com'"
    )
    assert admin._email_allowed("new@example.com") is True

    listed = payload(mcp_server.list_members(query="new@"))
    assert listed["count"] == 1
    assert listed["members"][0]["allowlisted"] == 1


def test_extend_expiry_email_and_remove(admin):
    added = payload(
        mcp_server.add_member(
            "member@example.com", days=10, email_consent=True, confirm=True
        )
    )
    old_expiry = added["grant"]["expires_at"]

    preview = payload(mcp_server.extend_member("member@example.com", days=5))
    assert preview["dry_run"] is True
    extended = payload(
        mcp_server.extend_member("member@example.com", days=5, confirm=True)
    )
    assert extended["written"] is True
    assert extended["new_expires_at"] > extended["old_expires_at"]

    off = payload(
        mcp_server.set_email_enabled("member@example.com", False, confirm=True)
    )
    assert off["new"] is False
    sub = admin.db.one(
        "SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.email='member@example.com'"
    )
    assert sub["email_enabled"] == 0

    removed = payload(mcp_server.remove_member("member@example.com", confirm=True))
    assert removed["written"] is True
    sub = admin.db.one(
        "SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.email='member@example.com'"
    )
    assert sub["status"] == "ended"
    assert sub["email_enabled"] == 0 and sub["agent_enabled"] == 0
    assert admin.db.one(
        "SELECT email FROM access_allowlist WHERE email='member@example.com'"
    ) is None
    assert admin._email_allowed("member@example.com") is False


def test_suppression_lifecycle(admin):
    preview = payload(mcp_server.suppress_email("bad@example.com"))
    assert preview["dry_run"] is True
    assert admin.db.one("SELECT email FROM suppressions WHERE email='bad@example.com'") is None

    payload(mcp_server.suppress_email("bad@example.com", reason="manual", confirm=True))
    assert admin.db.one(
        "SELECT reason FROM suppressions WHERE email='bad@example.com'"
    )["reason"] == "manual"
    listed = payload(mcp_server.list_suppressions())
    assert listed["count"] == 1

    payload(mcp_server.unsuppress_email("bad@example.com", confirm=True))
    assert admin.db.one("SELECT email FROM suppressions WHERE email='bad@example.com'") is None


def test_database_allowlist_controls_invite_gate(admin):
    mcp_server.add_member(
        "invitee@example.com", days=30, email_consent=True, confirm=True
    )
    admin.request_code("invitee@example.com", "127.0.0.1")

    mcp_server.remove_member("invitee@example.com", confirm=True)
    with pytest.raises(Problem) as raised:
        admin.request_code("invitee@example.com", "127.0.0.1")
    assert raised.value.status == 403
    assert admin._email_allowed("invited@example.com") is True


def test_writes_require_confirm(admin):
    payload(mcp_server.add_member("dry@example.com", days=30))
    assert admin.db.one("SELECT id FROM users WHERE email='dry@example.com'") is None
    payload(mcp_server.add_allowlist("dry@example.com"))
    assert admin.db.one("SELECT email FROM access_allowlist WHERE email='dry@example.com'") is None
    payload(mcp_server.suppress_email("dry@example.com"))
    assert admin.db.one("SELECT email FROM suppressions WHERE email='dry@example.com'") is None
