"""Exercise the reference CLI against a temporary application; no network or real mail."""

import errno
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pairing_client(harness, bind_agent, tmp_path, monkeypatch):
    app, service, login = harness
    owner, user = login("pairing-owner@example.com")
    owner.post("/api/subscriptions", json={"product_id": "quantgt"})
    old, old_auth = bind_agent(owner, name="Existing connection")
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "agent_client.py"
    spec = importlib.util.spec_from_file_location("review_agent_client", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    file = tmp_path / "private-client" / "agent.token"
    calls, pairing = [], {}
    client = TestClient(app)

    def request(origin, path, credential=None, payload=None):
        assert origin == "http://127.0.0.1"
        calls.append(path)
        headers = {"Authorization": "Bearer " + credential} if credential else {}
        if path == "/api/agent/pair/status" and not pairing.get("approved"):
            response = owner.post("/api/pair/approve", json={"code": pairing["user_code"]})
            assert response.status_code == 200
            pairing["approved"] = True
        if path == "/api/agent/test/complete":
            assert file.read_text() == pairing["device_secret"]
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            tests = file.with_name(file.name + ".connection-test")
            assert json.loads((tests / "body.json").read_text())["body"]
            assert (tests / "connection-test.txt").read_bytes() == b"Hadan private attachment test\n"
        response = client.request("POST" if payload is not None else "GET", path,
                                  headers=headers, **({"json": payload} if payload is not None else {}))
        assert response.status_code == 200, (path, response.status_code)
        if path == "/api/agent/pair":
            pairing.update(response.json())
        return response.content

    monkeypatch.setattr(module, "request", request)

    def run(command="pair"):
        monkeypatch.setattr(sys, "argv", ["agent_client.py", "--url", "http://127.0.0.1",
                                         "--token-file", str(file), command])
        module.main()

    yield module, file, calls, pairing, service, old, old_auth, owner, run
    client.close()


def assert_old_kept(state):
    _, _, calls, _, service, old, old_auth, owner, _ = state
    assert "/api/agent/test/complete" not in calls
    assert service.db.one("SELECT state FROM bindings WHERE id=?", (old["id"],))["state"] == "active"
    assert owner.get("/api/events", headers=old_auth).status_code == 200


def test_pair_saves_private_credential_and_test_files_before_replacing_old_connection(pairing_client, capsys):
    _, file, _, pairing, service, old, old_auth, owner, run = pairing_client
    run()
    assert owner.get("/api/events", headers=old_auth).status_code == 401
    assert service.db.one("SELECT state FROM bindings WHERE id=?", (old["id"],))["state"] == "revoked"
    assert owner.get("/api/events", headers={"Authorization": "Bearer " + file.read_text()}).status_code == 200
    output = capsys.readouterr()
    assert pairing["device_secret"] not in output.out + output.err
    for path in file.parent.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("failed_file", ["agent.token", "body.json", "connection-test.txt"])
def test_disk_write_failure_does_not_replace_old_connection(pairing_client, monkeypatch, failed_file, capsys):
    module, _, _, pairing, _, _, _, _, run = pairing_client
    original_save = module.save

    def fail(path, data, **kwargs):
        if path.name == failed_file:
            raise OSError(errno.ENOSPC, "Synthetic full client disk")
        return original_save(path, data, **kwargs)

    monkeypatch.setattr(module, "save", fail)
    with pytest.raises(OSError, match="Synthetic"):
        run()
    assert_old_kept(pairing_client)
    output = capsys.readouterr()
    assert pairing["device_secret"] not in output.out + output.err


@pytest.mark.parametrize("directory", [False, True])
def test_fsync_failure_does_not_activate_connection(pairing_client, monkeypatch, directory):
    module, _, _, _, _, _, _, _, run = pairing_client
    original = os.fsync

    def fail(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) == directory:
            raise OSError(errno.EIO, "Synthetic fsync failure")
        return original(descriptor)

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError, match="Synthetic"):
        run()
    assert_old_kept(pairing_client)


def test_saved_token_resumes_failed_test_without_a_new_pairing(pairing_client, monkeypatch):
    module, file, calls, _, _, _, old_auth, owner, run = pairing_client
    original_save = module.save

    def fail(path, data, **kwargs):
        if path.name == "connection-test.txt":
            raise OSError(errno.ENOSPC, "Synthetic full client disk")
        return original_save(path, data, **kwargs)

    monkeypatch.setattr(module, "save", fail)
    with pytest.raises(OSError):
        run()
    assert file.exists()
    assert_old_kept(pairing_client)
    monkeypatch.setattr(module, "save", original_save)
    run("complete-pair")
    assert calls.count("/api/agent/pair") == 1
    assert owner.get("/api/events", headers=old_auth).status_code == 401


def test_lost_activation_response_keeps_recoverable_token(pairing_client, monkeypatch, capsys):
    module, file, calls, pairing, _, _, old_auth, owner, run = pairing_client
    original_request = module.request

    def lost_reply(origin, path, credential=None, payload=None):
        response = original_request(origin, path, credential, payload)
        if path == "/api/agent/test/complete":
            raise TimeoutError("Synthetic activation response lost")
        return response

    monkeypatch.setattr(module, "request", lost_reply)
    with pytest.raises(TimeoutError):
        run()
    assert owner.get("/api/events", headers=old_auth).status_code == 401
    assert owner.get("/api/events", headers={"Authorization": "Bearer " + file.read_text()}).status_code == 200
    monkeypatch.setattr(module, "request", original_request)
    run("complete-pair")
    assert calls.count("/api/agent/pair") == 1
    output = capsys.readouterr()
    assert pairing["device_secret"] not in output.out + output.err


def test_existing_credential_is_not_overwritten_or_sent_to_pairing(pairing_client):
    _, file, calls, _, _, _, _, _, run = pairing_client
    file.parent.mkdir(mode=0o700)
    file.write_text("Existing private credential")
    file.chmod(0o600)
    with pytest.raises(ValueError, match="Existing credential"):
        run()
    assert calls == []
    assert file.read_text() == "Existing private credential"


def test_exclusive_credential_write_does_not_replace_a_concurrent_file(pairing_client):
    module, file, _, _, _, _, _, _, _ = pairing_client
    file.parent.mkdir(mode=0o700)
    file.write_bytes(b"Winning credential")
    with pytest.raises(FileExistsError):
        module.save(file, b"Losing credential", exclusive=True)
    assert file.read_bytes() == b"Winning credential"
    assert sorted(p.name for p in file.parent.iterdir()) == [file.name]
