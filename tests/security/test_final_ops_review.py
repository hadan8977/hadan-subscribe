"""Small independent review of the final operational entrypoints, no live services."""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hadan.db import enqueue
from hadan.worker import Worker


def test_simultaneous_heavy_claims_are_atomic_without_blocking_main(harness):
    _, service, _ = harness
    with service.db.transaction() as db:
        enqueue(db, "source:picks", {}, "review-source", queue="source")
        enqueue(db, "diagnose", {}, "review-ops", queue="ops")
        enqueue(db, "admin_summary", {}, "review-main", queue="main")
    barrier = threading.Barrier(3)

    def claim(queue):
        barrier.wait(timeout=5)
        return queue, Worker(service, queue=queue).claim()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(pool.map(claim, ("source", "ops", "main")))
    assert results["main"] is not None
    assert sum(results[q] is not None for q in ("source", "ops")) == 1
    loser = next(q for q in ("source", "ops") if results[q] is None)
    assert service.db.one("SELECT attempts FROM jobs WHERE queue=?", (loser,))["attempts"] == 0
    winner = next(results[q] for q in ("source", "ops") if results[q] is not None)
    service.db.execute("UPDATE jobs SET state='complete',lease_until=NULL WHERE id=?", (winner["id"],))
    assert Worker(service, queue=loser).claim() is not None


def test_expired_heavy_lease_recovers_without_overlapping_new_live_lease(harness):
    _, service, _ = harness
    with service.db.transaction() as db:
        enqueue(db, "source:picks", {}, "expired-source", queue="source")
        enqueue(db, "diagnose", {}, "waiting-ops", queue="ops")
    source = Worker(service, queue="source").claim()
    service.db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time()-1, source["id"]))
    assert Worker(service, queue="ops").claim() is not None
    # Recovery requeues the crashed source job, but cannot admit it alongside ops.
    assert Worker(service, queue="source").claim() is None
    restored = service.db.one("SELECT state,attempts,lease_token FROM jobs WHERE id=?", (source["id"],))
    assert restored == {"state":"queued", "attempts":1, "lease_token":None}


def test_health_cli_deduplicates_diagnosis_resolves_and_tracks_recurrence(harness, monkeypatch, capsys):
    import hadan.cli as cli

    _, service, _ = harness
    service.settings.ops_ai = True
    service.settings.deepseek_key = ""
    status = {"healthy":False, "issues":["worker:source heartbeat missing/stale"], "at":time.time()}
    monkeypatch.setattr(cli, "load_dotenv", lambda **_kwargs: False)
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "Service", lambda _settings: service)
    monkeypatch.setattr("hadan.health.check", lambda _service: dict(status))
    monkeypatch.setattr(sys, "argv", ["hadan", "check-health"])
    for _ in range(2):
        with pytest.raises(SystemExit) as failure:
            cli.main()
        assert failure.value.code == 1
        assert json.loads(capsys.readouterr().out)["healthy"] is False
    first = service.db.one("SELECT * FROM incidents WHERE kind='health'")
    assert first["count"] == 2 and first["state"] == "open"
    jobs = service.db.all("SELECT * FROM jobs WHERE kind='diagnose'")
    assert len(jobs) == 1 and jobs[0]["queue"] == "ops"
    assert json.loads(jobs[0]["payload"])["incident_id"] == first["id"]
    status.update(healthy=True, issues=[])
    cli.main()
    assert json.loads(capsys.readouterr().out)["healthy"] is True
    assert service.db.one("SELECT state FROM incidents WHERE id=?", (first["id"],))["state"] == "resolved"
    status.update(healthy=False, issues=["worker:source heartbeat missing/stale"])
    with pytest.raises(SystemExit):
        cli.main()
    capsys.readouterr()
    rows = service.db.all("SELECT * FROM incidents WHERE kind='health'")
    assert len(rows) == 2 and sorted(r["state"] for r in rows) == ["open", "resolved"]
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='diagnose'")) == 2


def admin_fixture(tmp_path, uid):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    capture = tmp_path / "arguments.json"
    identity = binaries / "id"
    identity.write_text("#!/bin/sh\nprintf '%s\\n' " + str(uid) + "\n")
    identity.chmod(0o700)
    launcher = binaries / "systemd-run"
    launcher.write_text(
        "#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['ADMIN_REVIEW_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(23)\n"
    )
    launcher.chmod(0o700)
    env = {"PATH":str(binaries)+":/usr/bin:/bin", "ADMIN_REVIEW_CAPTURE":str(capture)}
    script = Path(__file__).resolve().parents[2] / "scripts/admin.sh"
    return script, env, capture


@pytest.mark.parametrize("source", [False, True])
def test_admin_wrapper_preserves_arguments_and_selects_private_environment(tmp_path, source):
    script, env, capture = admin_fixture(tmp_path, uid=0)
    marker = tmp_path / "should-not-execute"
    literal = "two words ; $(touch " + str(marker) + ")"
    args = ["issue-codes", "--days", "30", "--output", literal]
    result = subprocess.run(["/bin/sh",str(script),*(["--source"] if source else []),*args],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 23  # Real launcher is replaced; no unit was started.
    assert not marker.exists()
    actual = json.loads(capture.read_text())
    assert "--property=User=hadan" in actual and "--property=Group=hadan" in actual
    assert "--property=NoNewPrivileges=yes" in actual and "--property=UMask=0077" in actual
    assert "--property=EnvironmentFile=/etc/quant-subscribe/hadan.env" in actual
    assert ("--property=EnvironmentFile=/etc/quant-subscribe/source.env" in actual) is source
    assert actual[-len(args)-3:] == ["/opt/quant-subscribe/current/.venv/bin/python", "-m", "hadan.cli", *args]
    assert result.stdout == "" and result.stderr == ""


def test_admin_wrapper_rejects_nonroot_before_any_launch(tmp_path):
    script, env, capture = admin_fixture(tmp_path, uid=1000)
    result = subprocess.run(["/bin/sh",str(script),"check-health"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "root" in result.stderr
    assert not capture.exists()
