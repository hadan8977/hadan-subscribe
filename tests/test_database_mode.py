import sqlite3

import pytest

from hadan.db import Database


def test_initialization_changes_previous_wal_database_without_losing_records(tmp_path):
    path = tmp_path / "test.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE preserved(value TEXT)")
        db.execute("INSERT INTO preserved VALUES('fixture')")
        db.execute("PRAGMA user_version=1")
    db.close()
    service_db = Database(path)
    service_db.initialize()
    assert service_db.one("PRAGMA journal_mode")["journal_mode"] == "delete"
    assert service_db.one("SELECT value FROM preserved")["value"] == "fixture"


def test_unsupported_schema_is_rejected_before_journal_change(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA user_version=77")
    db.close()
    with pytest.raises(RuntimeError, match="Unsupported"):
        Database(path).initialize()
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert check.execute("PRAGMA user_version").fetchone()[0] == 77
    check.close()
