from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = self.connect()
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise RuntimeError(
                    "Unsupported database schema; restore matching application version"
                )
            if (
                db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower()
                != "delete"
            ):
                raise RuntimeError(
                    "Could not enable required SQLite DELETE journal mode"
                )
        finally:
            db.close()
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(
                    "Unsupported database schema; restore matching application version"
                )
            # DELETE avoids depending on the deployment's SQLite WAL race patch level.
            db.executescript(Path(__file__).with_name("schema.sql").read_text())
            if "cancel_requested" not in {
                r[1] for r in db.execute("PRAGMA table_info(subscriptions)")
            }:
                db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "cancel_request_id" not in {
                r[1] for r in db.execute("PRAGMA table_info(subscriptions)")
            }:
                db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN cancel_request_id TEXT"
                )
            db.execute("PRAGMA user_version=1")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql, params=()):
        db = self.connect()
        try:
            return [dict(row) for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.transaction() as db:
            return db.execute(sql, params).rowcount


def enqueue(db, kind, payload, dedupe, *, queue="main", available_at=None):
    now = time.time()
    job_id = uid()
    db.execute(
        "INSERT OR IGNORE INTO jobs(id,kind,queue,payload,dedupe,available_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            job_id,
            kind,
            queue,
            json.dumps(payload),
            dedupe,
            now if available_at is None else available_at,
            now,
        ),
    )
    return job_id


def audit(db, actor, action, object_id, details=None):
    db.execute(
        "INSERT INTO audit(actor,action,object_id,details,created_at) VALUES (?,?,?,?,?)",
        (
            actor,
            action,
            object_id,
            json.dumps(details or {}, ensure_ascii=False),
            time.time(),
        ),
    )
