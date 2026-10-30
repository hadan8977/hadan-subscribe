"""Acceptance fixtures. Every mailbox and database belongs to pytest's tmp_path."""

from __future__ import annotations

import re
from contextlib import ExitStack
from email import policy
from email.parser import BytesParser

import pytest
from fastapi.testclient import TestClient

from hadan.config import Settings
from hadan.web import create_app
from hadan.worker import Worker


class Journey:
    def __init__(self, settings):
        self.settings = settings
        self.app = create_app(settings)
        self.service = self.app.state.service
        self.service.seed()
        self.clients = ExitStack()

    def client(self):
        return self.clients.enter_context(
            TestClient(self.app, base_url=self.settings.base_url)
        )

    def messages(self, *, recipient=None, login=None):
        messages = []
        for path in sorted((self.settings.data_dir / "mail").glob("*.eml")):
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            if recipient is not None and message["To"] != recipient:
                continue
            is_login = message["Subject"] == "Quant · 登录验证码 / Sign-in code"
            if login is not None and is_login != login:
                continue
            messages.append(message)
        return messages

    def login(self, email="reader@example.test"):
        client = self.client()
        requested = client.post("/api/auth/request", json={"email": email})
        assert requested.status_code == 200, requested.text
        assert Worker(self.service).run_once(), "OTP must be runnable immediately"
        messages = self.messages(recipient=email.lower().strip(), login=True)
        assert messages, "The local mailbox must receive the OTP"
        # Only one request per email in a normal journey; use newest local mail if repeated.
        body = messages[-1].get_body(preferencelist=("plain",)).get_content()
        code = re.search(r"验证码是 (\d{6})", body).group(1)
        verified = client.post("/api/auth/verify", json={"email": email, "code": code})
        assert verified.status_code == 200, verified.text
        return client

    def write(self, client, method, path, *, json=None):
        me = client.get("/api/me")
        assert me.status_code == 200, me.text
        return client.request(
            method, path, json=json, headers={"X-CSRF-Token": me.json()["csrf"]}
        )

    def subscribe(self, client, product="quantgt"):
        response = self.write(
            client, "POST", "/api/subscriptions", json={"product_id": product}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def connect(self, client, name="Reading agent"):
        response = self.write(
            client, "POST", "/api/bindings", json={"name": name, "mode": "poll"}
        )
        assert response.status_code == 200, response.text
        binding = response.json()
        agent = self.client()
        agent.headers["Authorization"] = "Bearer " + binding["token"]
        assert agent.get("/api/agent/test").status_code == 200
        assert (
            agent.get("/api/agent/test-file").content
            == b"Hadan private attachment test\n"
        )
        activated = agent.post("/api/agent/test/complete")
        assert activated.status_code == 200, activated.text
        return binding, agent

    def drain(self, *, worker=None):
        worker = worker or Worker(self.service)
        for _ in range(30):
            if not worker.run_once():
                return
        raise AssertionError("Worker did not become idle within 30 jobs")


@pytest.fixture
def journey(tmp_path):
    # Explicit settings prevent inherited production credentials from being used.
    settings = Settings(
        data_dir=tmp_path / "state",
        base_url="http://testserver",
        production=False,
        secret="test-only-secret-" * 4,
        admin_emails=("owner@example.test",),
        mail_backend="file",
        mail_from="Hadan Test <notify@example.test>",
        smtp_host="",
        smtp_user="",
        smtp_password="",
        stripe_key="",
        stripe_webhook_secret="",
        deepseek_key="",
        ops_ai=False,
    )
    result = Journey(settings)
    try:
        yield result
    finally:
        result.clients.close()
