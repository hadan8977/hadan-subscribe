import time

from fastapi.testclient import TestClient

from hadan.cli import grant
from hadan.config import Settings
from hadan.security import digest
from hadan.web import create_app


def signed_reader(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret="paging-fixture-secret-" * 4))
    service = app.state.service
    service.seed()
    grant(service, "paging@example.invalid", "notes")
    user_id = service.db.one("SELECT id FROM users")["id"]
    session = "paging-fixture-session"
    service.db.execute(
        "INSERT INTO sessions VALUES(?,?,?)",
        (digest(session), user_id, time.time() + 600),
    )
    client = TestClient(app)
    client.cookies.set("hadan_session", session)
    return client, service, user_id


def test_latest_notices_remain_visible_and_agent_cursor_stays_forward(tmp_path):
    client, service, user_id = signed_reader(tmp_path)
    for i in range(103):
        service.publish("notes", "run-" + str(i), f"Report {i}", "Private body")
    with service.db.transaction() as db:
        service.account_notice(
            db,
            user_id,
            "notes",
            "payment_failed",
            "Latest renewal notice",
            "Renewal action",
            "latest-notice",
        )
    latest = client.get("/api/events?newest=true&limit=100").json()
    assert latest["events"][0]["kind"] == "account:payment_failed"
    assert len(latest["events"]) == 100
    older = client.get(
        "/api/events",
        params={"newest": "true", "limit": 100, "before": latest["next_before"]},
    ).json()
    assert len(older["events"]) == 4
    assert len({e["id"] for e in latest["events"] + older["events"]}) == 104
    forward = client.get("/api/events?limit=100").json()
    after = client.get("/api/events", params={"after": forward["next_cursor"]}).json()
    assert forward["events"][0]["seq"] < forward["events"][-1]["seq"]
    assert after["events"][-1]["kind"] == "account:payment_failed"
    client.close()


def test_archive_cursor_does_not_drop_equal_timestamps(tmp_path):
    client, service, _ = signed_reader(tmp_path)
    for i in range(53):
        service.publish(
            "notes", f"archive-{i}", f"Archive {i}", "Private", baseline=True
        )
    service.db.execute("UPDATE versions SET created_at=?", (time.time() - 60,))
    first = client.get("/api/library").json()
    second = client.get(
        "/api/library",
        params={"before": first["next_before"], "before_id": first["next_before_id"]},
    ).json()
    assert len(first["contents"]) == 50 and len(second["contents"]) == 3
    assert len({c["id"] for c in first["contents"] + second["contents"]}) == 53
    assert not service.db.all("SELECT * FROM deliveries")
    client.close()
