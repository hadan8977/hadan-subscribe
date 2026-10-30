#!/usr/bin/env python3
"""Repeatable synthetic local measurement; no legacy data or external calls."""

import json
import resource
import tempfile
import time
from pathlib import Path
from hadan.config import Settings
from hadan.db import uid
from hadan.service import Service
from hadan.worker import Worker


def run(count=1000):
    with tempfile.TemporaryDirectory(prefix="hadan-benchmark-") as directory:
        s = Service(
            Settings(data_dir=Path(directory), secret="fixture-benchmark-key-" * 4)
        )
        s.seed()
        with s.db.transaction() as db:
            for i in range(count):
                user, sid = uid(), uid()
                db.execute(
                    "INSERT INTO users VALUES(?,?,?)",
                    (user, f"fixture{i}@example.invalid", time.time()),
                )
                db.execute(
                    "INSERT INTO subscriptions(id,user_id,product_id,created_at) VALUES(?,?,?,?)",
                    (sid, user, "notes", time.time()),
                )
        start = time.perf_counter()
        s.publish(
            "notes",
            "benchmark",
            "Synthetic benchmark",
            "No real content",
            files=[{"name": "fixture.txt", "data": b"fixture" * 100}],
        )
        publish = time.perf_counter() - start
        worker = Worker(s)
        start = time.perf_counter()
        for _ in range(min(count, 100)):
            worker.run_once()
        delivery = time.perf_counter() - start
        status = {
            line.split(":")[0]: line.split(":")[1].strip()
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        }
        return {
            "subscribers": count,
            "publish_seconds": round(publish, 4),
            "file_mail_100_seconds": round(delivery, 4),
            "database_bytes": s.settings.db_path.stat().st_size,
            "peak_process_rss_mib": round(int(status["VmHWM"].split()[0]) / 1024, 2),
            "current_rss_mib": round(int(status["VmRSS"].split()[0]) / 1024, 2),
            "conditions": "Synthetic SQLite DELETE+FULL, local filesystem sink, Linux /proc memory, no HTTP/browser/model/SMTP; not a network delivery SLA",
        }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
