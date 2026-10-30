#!/usr/bin/env python3
"""Local concurrent delivery/crash exercise. Uses only temporary synthetic data."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from hadan.cli import grant
from hadan.config import Settings
from hadan.service import Service


def run(seconds):
    with tempfile.TemporaryDirectory(prefix="hadan-soak-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        secret = "local-soak-fixture-" * 5
        settings = Settings(
            data_dir=Path(directory),
            secret=secret,
            production=False,
            base_url=origin,
            mail_backend="file",
            admin_emails=(),
        )
        service = Service(settings)
        service.seed()
        for i in range(10):
            grant(service, f"fixture{i}@example.invalid", "notes", email_consent=True)
        env = {
            **os.environ,
            "HADAN_DATA_DIR": directory,
            "HADAN_ENV": "development",
            "HADAN_BASE_URL": origin,
            "HADAN_SECRET": secret,
            "HADAN_ADMIN_EMAILS": "",
            "MAIL_BACKEND": "file",
            "QUANTGT_ENABLED": "0",
            "OFFICIAL_MAIL_ENABLED": "0",
            "OPS_AI_ENABLED": "0",
            "STRIPE_SECRET_KEY": "",
            "DEEPSEEK_API_KEY": "",
        }

        def spawn(*args):
            return subprocess.Popen(
                [sys.executable, "-m", "hadan.cli", *args],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        web = spawn("serve", "--port", str(port))
        worker = spawn("worker", "--queue", "main")
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(origin + "/health/live", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Local web startup failed")
            started = time.monotonic()
            publish_at = started
            crashes = [started + seconds / 3, started + 2 * seconds / 3]
            crash_count = reclaimed = published = 0
            latencies = []
            while time.monotonic() - started < seconds:
                now = time.monotonic()
                if now >= publish_at:
                    service.publish(
                        "notes",
                        f"run-{published}",
                        f"Soak {published}",
                        "Synthetic private body",
                        files=[
                            {
                                "name": "fixture.txt",
                                "data": f"attachment {published}".encode(),
                            }
                        ],
                    )
                    published += 1
                    publish_at = now + 1
                if crashes and now >= crashes[0]:
                    crashes.pop(0)
                    worker.kill()
                    worker.wait(timeout=5)
                    # Fast-forward only this temporary file-mail queue's lease clock.
                    # Real production recovery waits for the normal lease expiry.
                    reclaimed += service.db.execute(
                        "UPDATE jobs SET lease_until=0 WHERE state='running' AND queue='main'"
                    )
                    worker = spawn("worker", "--queue", "main")
                    crash_count += 1
                request_at = time.perf_counter()
                with urllib.request.urlopen(
                    origin + "/health/ready", timeout=5
                ) as response:
                    assert response.status == 200
                    response.read()
                latencies.append((time.perf_counter() - request_at) * 1000)
                assert web.poll() is None and worker.poll() is None
                time.sleep(0.1)
            until = time.monotonic() + 60
            while time.monotonic() < until:
                remaining = service.db.one(
                    "SELECT COUNT(*) n FROM jobs WHERE kind='deliver' AND state!='complete'"
                )["n"]
                if not remaining:
                    break
                time.sleep(0.2)
            counts = service.db.all(
                "SELECT state,COUNT(*) n FROM deliveries GROUP BY state"
            )
            assert counts == [{"state": "accepted", "n": published * 10}], counts
            files = len(list((settings.data_dir / "mail").glob("*.eml")))
            assert files == published * 10
            assert service.db.one("PRAGMA integrity_check")["integrity_check"] == "ok"
            return {
                "exercise_seconds": seconds,
                "published_versions": published,
                "accepted_deliveries": files,
                "forced_worker_deaths": crash_count,
                "expired_fixture_leases": reclaimed,
                "http_checks": len(latencies),
                "http_median_ms": round(sorted(latencies)[len(latencies) // 2], 2),
                "database_integrity": "ok",
                "all_deliveries_drained": True,
                "scope": "Real local web/worker processes, synthetic file mail only; lease time fast-forwarded after SIGKILL. Not a long-term or external-provider availability proof.",
            }
        finally:
            for proc in (web, worker):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 3600:
        parser.error("Use 15–3600 seconds")
    print(json.dumps(run(args.seconds), indent=2))
