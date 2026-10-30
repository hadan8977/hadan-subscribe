from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .db import Database

MAGIC = b"HADAN-BACKUP-1\n"


def hash_file(path):
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def key_bytes(path):
    raw = Path(path).read_bytes().strip()
    key = base64.urlsafe_b64decode(raw)
    if len(key) != 32:
        raise ValueError("Backup key must be 32 random bytes, base64 encoded")
    return key


def crypt_file(source, target, key, *, decrypt=False):
    with open(source, "rb") as src, open(target, "xb") as out:
        os.chmod(target, 0o600)
        if decrypt:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError("Not a Hadan encrypted backup")
            nonce = src.read(12)
            src.seek(-16, 2)
            tag = src.read(16)
            remaining = src.tell() - len(MAGIC) - 12 - 16
            src.seek(len(MAGIC) + 12)
            context = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        else:
            nonce = os.urandom(12)
            out.write(MAGIC + nonce)
            context = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            remaining = Path(source).stat().st_size
        context.authenticate_additional_data(MAGIC)
        while remaining:
            chunk = src.read(min(65536, remaining))
            if not chunk:
                raise ValueError("Truncated backup")
            out.write(context.update(chunk))
            remaining -= len(chunk)
        out.write(context.finalize())
        if not decrypt:
            out.write(context.tag)
        out.flush()
        os.fsync(out.fileno())


def backup(service, destination, key_file):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = key_bytes(key_file)
    with tempfile.TemporaryDirectory(
        prefix="hadan-backup-", dir=service.settings.data_dir
    ) as tmp:
        tmp = Path(tmp)
        snapshot = tmp / "hadan.sqlite3"
        source = service.db.connect()
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target, pages=256, sleep=0.05)
        finally:
            source.close()
            target.close()
        rows = Database(snapshot).all(
            "SELECT DISTINCT storage_key,sha256,size FROM attachments"
        )
        manifest = {
            "format": 1,
            "schema": 1,
            "created_at": time.time(),
            "database_sha256": hash_file(snapshot),
            "files": rows,
        }
        archive = tmp / "snapshot.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="hadan.sqlite3", recursive=False)
            for a in rows:
                path = service.settings.data_dir / "files" / a["storage_key"]
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or hash_file(path) != a["sha256"]
                ):
                    raise ValueError("Attachment integrity check failed")
                tar.add(path, arcname="files/" + a["storage_key"], recursive=False)
            mpath = tmp / "manifest.json"
            mpath.write_text(json.dumps(manifest))
            tar.add(mpath, arcname="manifest.json")
        import uuid

        temporary = destination.with_name(
            destination.name + "." + uuid.uuid4().hex + ".partial"
        )
        try:
            crypt_file(archive, temporary, key)
            # Atomic no-clobber installation: a concurrent backup owns its own destination.
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    service.db.execute(
        "INSERT INTO meta VALUES('last_backup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(time.time()),),
    )
    return {
        "backup": str(destination),
        "files": len(rows),
        "bytes": destination.stat().st_size,
    }


def restore(archive, destination, key_file):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(
            "Restore requires a new empty directory; never overwrite a running service"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hadan-restore-", dir=destination.parent
    ) as tmp:
        tmp = Path(tmp)
        plain = tmp / "snapshot.tar.gz"
        stage = tmp / "restored"
        stage.mkdir(mode=0o700)
        crypt_file(archive, plain, key_bytes(key_file), decrypt=True)
        with tarfile.open(plain, "r:gz") as tar:
            members = []
            seen = set()
            total = 0
            for member in tar:
                import re

                total += member.size
                if (
                    len(members) >= 100000
                    or total > 16 * 1024**3
                    or member.size > 1024**3
                    or (member.name == "manifest.json" and member.size > 16 * 1024**2)
                ):
                    raise ValueError("Backup size limits exceeded")
                if (
                    member.name in seen
                    or not member.isfile()
                    or not (
                        member.name in {"hadan.sqlite3", "manifest.json"}
                        or re.fullmatch(r"files/[a-f0-9]{64}", member.name)
                    )
                ):
                    raise ValueError("Unsafe archive member")
                seen.add(member.name)
                members.append(member)
            for member in members:
                path = stage / member.name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as src, open(path, "xb") as out:
                    shutil.copyfileobj(src, out, 65536)
                path.chmod(0o600)
        manifest = json.loads((stage / "manifest.json").read_text())
        if manifest.get("format") != 1 or manifest.get("schema") != 1:
            raise ValueError("Unsupported backup version")
        if hash_file(stage / "hadan.sqlite3") != manifest["database_sha256"]:
            raise ValueError("Database checksum mismatch")
        if not isinstance(manifest.get("files"), list):
            raise ValueError("Invalid attachment manifest")
        manifest_set = set()
        for a in manifest["files"]:
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("storage_key"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", a["storage_key"])
                or a.get("sha256") != a["storage_key"]
                or type(a.get("size")) is not int
                or not 0 <= a["size"] <= 16 * 1024 * 1024
            ):
                raise ValueError("Invalid attachment manifest entry")
            item = (a["storage_key"], a["sha256"], a["size"])
            if item in manifest_set:
                raise ValueError("Duplicate attachment manifest entry")
            manifest_set.add(item)
            path = stage / "files" / a["storage_key"]
            if (
                not path.is_file()
                or path.stat().st_size != a["size"]
                or hash_file(path) != a["sha256"]
            ):
                raise ValueError("Restored attachment checksum mismatch")
        database = Database(stage / "hadan.sqlite3")
        if database.one("PRAGMA user_version")["user_version"] != 1:
            raise ValueError("Database schema version mismatch")
        referenced = {
            tuple(x.values())
            for x in database.all(
                "SELECT DISTINCT storage_key,sha256,size FROM attachments"
            )
        }
        if referenced != manifest_set or seen != {
            "hadan.sqlite3",
            "manifest.json",
            *("files/" + a[0] for a in manifest_set),
        }:
            raise ValueError("Database, manifest and archive attachment sets differ")
        if database.one("PRAGMA integrity_check")[
            "integrity_check"
        ] != "ok" or database.all("PRAGMA foreign_key_check"):
            raise ValueError("Database integrity check failed")
        with database.transaction() as db:
            db.execute(
                "INSERT INTO meta VALUES('delivery_paused','1') ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            db.execute(
                "INSERT INTO meta VALUES('restored_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
            for table in ("sessions", "otp", "pairings"):
                db.execute("DELETE FROM " + table)
            db.execute("UPDATE bindings SET state='revoked'")
            db.execute("UPDATE deliveries SET state='uncertain' WHERE state='sending'")
            db.execute(
                "UPDATE jobs SET state='uncertain' WHERE json_extract(payload,'$.delivery_id') IN (SELECT id FROM deliveries WHERE state='uncertain')"
            )
            db.execute(
                "UPDATE jobs SET state='queued',lease_until=NULL,lease_token=NULL WHERE state='running'"
            )
            db.execute("DELETE FROM jobs WHERE kind IN ('login_mail','webhook_test')")
        if destination.exists():
            destination.rmdir()
        os.replace(stage, destination)
    return {
        "directory": str(destination),
        "delivery_paused": True,
        "bindings_revoked": True,
        "integrity": "ok",
    }
