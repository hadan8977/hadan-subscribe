"""Admin-only member management: previews, explicit confirmation and audits."""

from __future__ import annotations

import time


def test_admin_member_management_is_admin_only_and_audited(harness):
    app, service, login = harness
    admin, admin_user = login("owner@example.com")
    normal, _ = login("normal@example.com")
    assert normal.get("/api/admin/members").status_code == 403
    assert normal.post(
        "/api/admin/members/preview",
        json={"action": "add", "email": "blocked@example.com", "days": 30},
    ).status_code == 403

    listed = admin.get("/api/admin/members")
    assert listed.status_code == 200, listed.text
    assert listed.json()["counts"]["users"] == 2
    assert "member-admin" in admin.get("/admin").text

    email = "manual@example.com"
    preview = admin.post(
        "/api/admin/members/preview",
        json={"action": "add", "email": email, "days": 30},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["email"] == email
    assert preview.json()["email_enabled"] is False
    assert service.db.one("SELECT id FROM users WHERE email=?", (email,)) is None

    created = admin.post(
        "/api/admin/members",
        json={"action": "add", "email": email, "days": 30, "email_consent": False, "confirm": True},
    )
    assert created.status_code == 200, created.text
    user = service.db.one("SELECT * FROM users WHERE email=?", (email,))
    sub = service.db.one("SELECT * FROM subscriptions WHERE user_id=?", (user["id"],))
    assert sub["status"] == "active"
    assert sub["email_enabled"] == 0 and sub["agent_enabled"] == 0
    assert service.db.one("SELECT email FROM access_allowlist WHERE email=?", (email,))

    duplicate = admin.post(
        "/api/admin/members/preview",
        json={"action": "add", "email": email, "days": 30},
    )
    assert duplicate.status_code == 409

    extend_preview = admin.post(
        "/api/admin/members/preview",
        json={"action": "extend", "email": email, "days": 15},
    )
    assert extend_preview.status_code == 200
    extended = admin.post(
        "/api/admin/members",
        json={"action": "extend", "email": email, "days": 15, "confirm": True},
    )
    assert extended.status_code == 200, extended.text
    assert service.db.one("SELECT expires_at FROM subscriptions WHERE id=?", (sub["id"],))["expires_at"] > sub["expires_at"]

    mismatch = admin.post(
        "/api/admin/members",
        json={"action": "remove", "email": email, "confirm_email": "wrong@example.com", "confirm": True},
    )
    assert mismatch.status_code == 400
    self_remove = admin.post(
        "/api/admin/members/preview",
        json={"action": "remove", "email": "owner@example.com"},
    )
    assert self_remove.status_code == 400

    removed = admin.post(
        "/api/admin/members",
        json={"action": "remove", "email": email, "confirm_email": email, "confirm": True},
    )
    assert removed.status_code == 200, removed.text
    assert service.db.one("SELECT * FROM users WHERE email=?", (email,)) is not None
    assert service.db.one("SELECT status,email_enabled,agent_enabled FROM subscriptions WHERE id=?", (sub["id"],)) == {
        "status": "ended", "email_enabled": 0, "agent_enabled": 0
    }
    assert service.db.one("SELECT email FROM access_allowlist WHERE email=?", (email,)) is None
    assert service.db.one(
        "SELECT id FROM audit WHERE action='admin_member_remove' AND object_id=?", (sub["id"],)
    )
    assert service.db.one(
        "SELECT id FROM audit WHERE action='admin_member_add' AND object_id=?", (email,)
    )

    counts = admin.get("/api/admin/members?status=active").json()["counts"]
    assert counts["users"] == 3 and counts["active"] == 0
    assert admin.post(
        "/api/admin/members",
        json={"action": "remove", "email": email, "confirm_email": email},
    ).status_code == 400  # every write needs confirm=true
