import json

import pytest
from fastapi.testclient import TestClient

from hadan.config import Settings
from hadan.security import decrypt
from hadan.web import create_app


@pytest.fixture
def harness(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        secret="s" * 64,
        mail_backend="file",
        admin_emails=("owner@example.com",),
    )
    app = create_app(settings)
    service = app.state.service
    service.seed()
    clients = []

    def login(email):
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        result = client.post("/api/auth/request", json={"email": email})
        assert result.status_code == 200, result.text
        jobs = service.db.all(
            "SELECT payload FROM jobs WHERE kind='login_mail' ORDER BY created_at DESC"
        )
        job = next(
            json.loads(row["payload"])
            for row in jobs
            if json.loads(row["payload"])["email"] == email
        )
        code = decrypt(settings.secret, job["encrypted_code"])
        result = client.post("/api/auth/verify", json={"email": email, "code": code})
        assert result.status_code == 200, result.text
        me = client.get("/api/me").json()
        client.headers["X-CSRF-Token"] = me["csrf"]
        return client, me["user"]

    yield app, service, login
    for client in clients:
        client.close()


def bind(client, *, name="Personal Agent"):
    response = client.post("/api/bindings", json={"name": name, "mode": "poll"})
    assert response.status_code == 200, response.text
    binding = response.json()
    auth = {"Authorization": "Bearer " + binding["token"]}
    assert client.get("/api/agent/test", headers=auth).status_code == 200
    assert client.get("/api/agent/test-file", headers=auth).status_code == 200
    assert client.post("/api/agent/test/complete", headers=auth).status_code == 200
    return binding, auth


@pytest.fixture
def bind_agent():
    return bind
