#!/usr/bin/env python3
"""Measure an isolated local web/worker pair. Stops every process it starts."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def memory(pid):
    values = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        if ":" in line
    }
    return {
        "rss_mib": round(int(values["VmRSS"].split()[0]) / 1024, 2),
        "peak_rss_mib": round(int(values["VmHWM"].split()[0]) / 1024, 2),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="hadan-runtime-measure-") as directory:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = os.environ.copy()
        env.update(
            {
                "HADAN_DATA_DIR": directory,
                "HADAN_ENV": "development",
                "HADAN_BASE_URL": f"http://127.0.0.1:{port}",
                "HADAN_SECRET": "runtime-fixture-" * 5,
                "HADAN_ADMIN_EMAILS": "",
                "MAIL_BACKEND": "file",
                "QUANTGT_ENABLED": "0",
                "OFFICIAL_MAIL_ENABLED": "0",
                "OPS_AI_ENABLED": "0",
                "STRIPE_SECRET_KEY": "",
                "DEEPSEEK_API_KEY": "",
            }
        )
        subprocess.run(
            [sys.executable, "-m", "hadan.cli", "init"],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        web = subprocess.Popen(
            [sys.executable, "-m", "hadan.cli", "serve", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        worker = subprocess.Popen(
            [sys.executable, "-m", "hadan.cli", "worker"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            origin = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urllib.request.urlopen(
                        origin + "/health/live", timeout=1
                    ) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Local web startup failed")
            times = []
            for _ in range(20):
                start = time.perf_counter()
                with urllib.request.urlopen(origin, timeout=5) as r:
                    assert r.status == 200
                    r.read()
                times.append((time.perf_counter() - start) * 1000)
            time.sleep(2)
            print(
                json.dumps(
                    {
                        "web": memory(web.pid),
                        "main_worker": memory(worker.pid),
                        "home_http_median_ms": round(sorted(times)[len(times) // 2], 2),
                        "samples": 20,
                        "scope": "Local warm homepage + idle file-mail main worker; no browser, source, external SMTP or model",
                    },
                    indent=2,
                )
            )
        finally:
            for p in (web, worker):
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    main()
