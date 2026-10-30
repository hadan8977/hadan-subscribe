import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from hadan.health import check
from hadan.schedule import tick
from hadan.transport import DeliveryError
from hadan.worker import Worker


def alive(pid):
    try:
        # A killed orphan may briefly remain a zombie until container init reaps it.
        return Path(f"/proc/{pid}/stat").read_text().split()[2] not in {"Z", "X"}
    except FileNotFoundError:
        return False


def test_ops_hard_timeout_kills_actual_child_and_grandchild_group(
    harness, tmp_path, monkeypatch
):
    _, service, _ = harness
    service.settings.ops_timeout = 1
    pid_file = tmp_path / "owned-child-pids.json"
    real_popen = subprocess.Popen
    launched = []
    script = """import json,os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid}))
time.sleep(60)
"""

    def substitute_fixed_command(command, **kwargs):
        assert command[:3] == [sys.executable, "-m", "hadan.ops"]
        assert command[3:] == ["diagnose", "fixture-incident"]
        assert kwargs.get("start_new_session") is True
        assert not kwargs.get("shell", False)
        assert kwargs["stdout"] != subprocess.PIPE
        proc = real_popen([sys.executable, "-c", script, str(pid_file)], **kwargs)
        launched.append(proc)
        return proc

    monkeypatch.setattr("hadan.worker.subprocess.Popen", substitute_fixed_command)
    started = time.monotonic()
    try:
        with pytest.raises(DeliveryError, match="exceeded time limit"):
            Worker(service, queue="ops").dispatch(
                {
                    "kind": "diagnose",
                    "payload": json.dumps(
                        {
                            "incident_id": "fixture-incident",
                            "command": "ignored-untrusted-command",
                        }
                    ),
                }
            )
        assert time.monotonic() - started < 5
        pids = json.loads(pid_file.read_text())
        assert not alive(pids["parent"])
        deadline = time.monotonic() + 1
        while alive(pids["child"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(pids["child"])
        assert launched[0].poll() is not None
    finally:
        for proc in launched:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)


@pytest.mark.parametrize("kind", ["diagnose", "propose_parser"])
def test_actual_ops_entrypoint_with_disabled_configuration_fails_cleanly(
    harness, monkeypatch, kind
):
    _, service, _ = harness
    for name, value in {
        "HADAN_ENV": "development",
        "HADAN_DATA_DIR": str(service.settings.data_dir),
        "HADAN_SECRET": service.settings.secret,
        "MAIL_BACKEND": "file",
        "OPS_AI_ENABLED": "0",
        "DEEPSEEK_API_KEY": "",
    }.items():
        monkeypatch.setenv(name, value)
    service.settings.ops_timeout = 10
    with pytest.raises(DeliveryError) as error:
        Worker(service, queue="ops").dispatch(
            {
                "kind": kind,
                "payload": json.dumps({"incident_id": "nonexistent-fixture"}),
            }
        )
    assert error.value.retry is False
    assert "not configured" in str(error.value)
    assert "NameError" not in str(error.value)
    assert "Traceback" not in str(error.value)


def basic_heartbeats(service, monkeypatch):
    monkeypatch.setenv("QUANTGT_ENABLED", "0")
    monkeypatch.setenv("OFFICIAL_MAIL_ENABLED", "0")
    for name in ("scheduler", "worker:main"):
        service.db.execute(
            "INSERT INTO heartbeats VALUES(?,?,'{}')", (name, time.time())
        )


def test_enabled_ops_requires_heartbeat_but_disabled_ops_does_not(harness, monkeypatch):
    _, service, _ = harness
    basic_heartbeats(service, monkeypatch)
    service.settings.ops_ai = True
    assert "worker:ops heartbeat missing/stale" in check(service)["issues"]
    service.settings.ops_ai = False
    assert "worker:ops heartbeat missing/stale" not in check(service)["issues"]


def test_ops_heartbeat_allows_a_live_long_job_but_rejects_staleness(
    harness, monkeypatch
):
    _, service, _ = harness
    basic_heartbeats(service, monkeypatch)
    service.settings.ops_ai = True
    service.db.execute(
        "INSERT INTO heartbeats VALUES('worker:ops',?,'{}')", (time.time() - 250,)
    )
    assert "worker:ops heartbeat missing/stale" not in check(service)["issues"]
    service.db.execute(
        "UPDATE heartbeats SET updated_at=? WHERE name='worker:ops'",
        (time.time() - 1000,),
    )
    assert "worker:ops heartbeat missing/stale" in check(service)["issues"]


def test_resolved_incident_recurrence_starts_a_new_bounded_episode(harness):
    _, service, _ = harness
    service.settings.ops_ai = True
    worker = Worker(service)
    first = worker.incident(
        "source:picks", "temporary source timeout", "job_id=" + "a" * 32
    )
    service.db.execute("UPDATE incidents SET state='resolved' WHERE id=?", (first,))
    second = worker.incident(
        "source:picks", "temporary source timeout", "job_id=" + "b" * 32
    )
    assert first != second
    assert (
        service.db.one("SELECT state FROM incidents WHERE id=?", (first,))["state"]
        == "resolved"
    )
    assert (
        service.db.one("SELECT state FROM incidents WHERE id=?", (second,))["state"]
        == "open"
    )
    assert (
        worker.incident(
            "source:picks", "temporary source timeout", "job_id=" + "b" * 32
        )
        == second
    )
    jobs = service.db.all("SELECT payload FROM jobs WHERE kind='diagnose'")
    assert {json.loads(j["payload"])["incident_id"] for j in jobs} == {first, second}
    assert len(jobs) == 2


def test_enabling_ai_recovers_existing_incident_once_per_day(harness, monkeypatch):
    _, service, _ = harness
    monkeypatch.setenv("QUANTGT_ENABLED", "0")
    monkeypatch.setenv("OFFICIAL_MAIL_ENABLED", "0")
    service.settings.ops_ai = False
    incident = Worker(service).incident("source:picks", "existing source failure")
    assert service.db.all("SELECT * FROM jobs WHERE kind='diagnose'") == []
    service.settings.ops_ai = True
    now = datetime.now(timezone.utc)
    tick(service, now)
    tick(service, now + timedelta(seconds=30))
    jobs = service.db.all("SELECT payload FROM jobs WHERE kind='diagnose'")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload"])["incident_id"] == incident
    tick(service, now + timedelta(days=1))
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='diagnose'")) == 2


@pytest.mark.parametrize("kind", ["diagnose", "propose_parser"])
def test_ops_failures_never_schedule_recursive_model_diagnosis(
    harness, monkeypatch, kind
):
    _, service, _ = harness
    monkeypatch.setenv("QUANTGT_ENABLED", "0")
    monkeypatch.setenv("OFFICIAL_MAIL_ENABLED", "0")
    service.settings.ops_ai = True
    Worker(service).incident(kind, "model request failure")
    tick(service)
    assert service.db.all("SELECT * FROM jobs WHERE kind='diagnose'") == []
