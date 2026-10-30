#!/usr/bin/env python3
"""Atomic code release pointer only. Operator stops services and handles DB compatibility."""

import argparse
import json
import os
import uuid
from pathlib import Path


def switch(root, release):
    root = Path(root).resolve()
    release = Path(release).resolve()
    releases = root / "releases"
    current = root / "current"
    if (
        release.parent != releases
        or not (release / "src/hadan/cli.py").is_file()
        or not (release / "uv.lock").is_file()
    ):
        raise ValueError("Release must be a prepared child of releases/")
    if current.exists() and not current.is_symlink():
        raise ValueError("current must be a symlink, never overwrite a directory")
    old = str(current.resolve()) if current.is_symlink() else None
    temp = root / (".current-" + uuid.uuid4().hex)
    try:
        temp.symlink_to(release, target_is_directory=True)
        os.replace(temp, current)
    finally:
        temp.unlink(missing_ok=True)
    if old:
        previous = root / (".previous-" + uuid.uuid4().hex)
        try:
            previous.symlink_to(old, target_is_directory=True)
            os.replace(previous, root / "previous")
        finally:
            previous.unlink(missing_ok=True)
    return {"current": str(release), "previous": old, "database_changed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("release")
    parser.add_argument("--root", default="/opt/quant-subscribe")
    parser.add_argument("--services-stopped", action="store_true")
    args = parser.parse_args()
    if not args.services_stopped:
        parser.error(
            "Stop services and confirm compatible schema/backup, then use --services-stopped"
        )
    print(json.dumps(switch(args.root, args.release), indent=2))
