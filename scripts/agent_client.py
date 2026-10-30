#!/usr/bin/env python3
"""A reference adapter for an EXISTING agent runtime: stdlib only, no LLM account."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(origin, path, credential=None, payload=None):
    headers = {"Accept": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        origin + path,
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=25) as response:
        data = response.read(24 * 1024 * 1024 + 1)
        if len(data) > 24 * 1024 * 1024:
            raise ValueError("Server response exceeds limit")
        return data


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path, data, *, exclusive=False):
    missing = []
    directory = path.parent
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(directory.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            os.link(temporary, path)  # Never replace another credential or a symlink.
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def complete_pair(origin, credential, credential_file):
    """Test durable local receipt before replacing the previous default connection."""
    descriptor = os.open(credential_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as saved:
        info = os.fstat(saved.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Credential must be a private regular file with mode 0600")
        if saved.read(4097).strip() != credential.encode():
            raise ValueError("Saved credential does not match this connection")
        os.fsync(saved.fileno())
    sync_directory(credential_file.parent)
    body = request(origin, "/api/agent/test", credential)
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get("body"), str):
        raise ValueError("Invalid connection test response")
    attachment = request(origin, "/api/agent/test-file", credential)
    if not attachment:
        raise ValueError("Empty connection test attachment")
    directory = credential_file.with_name(credential_file.name + ".connection-test")
    save(directory / "body.json", body)
    save(directory / "connection-test.txt", attachment)
    request(origin, "/api/agent/test/complete", credential, {})


def consume(origin, credential, destination, ack=False):
    events = json.loads(request(origin, "/api/events?pending=true", credential))[
        "events"
    ]
    saved = 0
    for event in events:
        eid = event["id"]
        cid = event["content_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", eid) or (
            cid is not None and not re.fullmatch(r"[a-f0-9]{32}", cid)
        ):
            raise ValueError("Invalid event identity")
        folder = Path(destination) / (cid or "account") / eid
        if event["kind"].startswith("account:"):
            pass
        elif event["kind"] == "withdrawn" or event.get("withdrawn"):
            # Mark withdrawal; local retention/deletion follows the user's agent policy.
            save(
                Path(destination) / cid / "WITHDRAWN.json",
                json.dumps(event, ensure_ascii=False).encode(),
            )
        else:
            content = json.loads(
                request(
                    origin,
                    "/api/content/" + cid + "?version=" + str(event["version"]),
                    credential,
                )
            )
            for a in content["attachments"]:
                data = request(origin, "/api/files/" + a["id"], credential)
                if (
                    len(data) != a["size"]
                    or hashlib.sha256(data).hexdigest() != a["sha256"]
                ):
                    raise ValueError("Attachment verification failed")
                save(
                    folder / (a["id"] + "-" + Path(a["name"].replace("\\", "/")).name),
                    data,
                )
            save(
                folder / "content.json",
                json.dumps(content, ensure_ascii=False, indent=2).encode(),
            )
        save(folder / "event.json", json.dumps(event, ensure_ascii=False).encode())
        saved += 1
        if ack:
            request(origin, "/api/events/" + eid + "/ack", credential, {})
    return {
        "saved": saved,
        "acknowledged": ack,
        "processing_definition": "Durably saved to the local agent inbox; no financial/action automation",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    pairing = sub.add_parser("pair")
    pairing.add_argument("--name", default="My existing agent")
    sub.add_parser("complete-pair", help="Resume a connection test using an already saved credential")
    reading = sub.add_parser("consume")
    reading.add_argument("--directory", default="agent-inbox")
    reading.add_argument("--ack", action="store_true")
    reading.add_argument("--watch", action="store_true")
    reading.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    origin = args.url.rstrip("/")
    url = urllib.parse.urlsplit(origin)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("Use HTTPS or local test loopback")
    if url.username or url.query or url.fragment or url.path:
        raise ValueError("Supply only the service origin")
    file = Path(args.token_file)
    if args.command == "pair":
        if file.exists() or file.is_symlink():
            raise ValueError(
                "Existing credential file; use a new path when changing bindings"
            )
        result = json.loads(
            request(
                origin, "/api/agent/pair", payload={"name": args.name, "mode": "poll"}
            )
        )
        credential = result["device_secret"]
        print(
            "Open",
            result["verification_uri"],
            "and authorize code",
            result["user_code"],
            flush=True,
        )
        for _ in range(120):
            state = json.loads(
                request(
                    origin,
                    "/api/agent/pair/status",
                    payload={"pair_id": result["pair_id"], "device_secret": credential},
                )
            )
            if state["state"] == "approved":
                break
            time.sleep(5)
        else:
            raise ValueError("Pairing expired")
        save(file, credential.encode(), exclusive=True)
        complete_pair(origin, credential, file)
        print("Connected; credential saved with mode 0600.")
    else:
        if file.stat().st_mode & 0o077:
            raise ValueError("Credential file must have mode 0600")
        credential = file.read_text().strip()
        if args.command == "complete-pair":
            complete_pair(origin, credential, file)
            print("Connected; saved credential and test files verified.")
            return
        while True:
            print(
                json.dumps(
                    consume(origin, credential, args.directory, args.ack),
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not args.watch:
                break
            time.sleep(max(args.interval, 30))


if __name__ == "__main__":
    main()
