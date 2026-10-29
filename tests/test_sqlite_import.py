import sqlite3

import psycopg
import pytest

from garmin_mcp import db, sqlite_import


@pytest.fixture
def legacy_database(tmp_path):
    source = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(source)
    for statement in db.MIGRATIONS[1].split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("ALTER TABLE training_status ADD COLUMN fitness_age DOUBLE PRECISION")
    connection.execute(
        "ALTER TABLE training_status ADD COLUMN achievable_fitness_age DOUBLE PRECISION"
    )
    rows = {
        "daily_wellness": {"date": "2026-06-24", "steps": 4321, "source": "fit"},
        "sleep": {"date": "2026-06-24", "score": 84, "source": "fit"},
        "hrv": {"date": "2026-06-24", "last_night_avg": 35, "source": "fit"},
        "activities": {"activity_id": 123, "date": "2026-06-24", "name": "Run"},
        "training_status": {"date": "2026-06-24", "status": "productive"},
        "raw_snapshots": {
            "path": "daily/2026/2026-06-24/sleep.json",
            "date": "2026-06-24",
            "endpoint": "sleep",
            "bytes": 10,
            "sha256": "abc",
        },
        "sync_state": {
            "endpoint": "sleep",
            "date": "2026-06-24",
            "status": "ok",
            "attempts": 1,
        },
        "demo_info": {
            "generated_at": "now",
            "days": 1,
            "start": "2026-06-24",
            "end": "2026-06-24",
            "seed": 4,
        },
    }
    for table, row in rows.items():
        columns = ", ".join(f'"{column}"' for column in row)
        marks = ", ".join("?" for _ in row)
        connection.execute(
            f'INSERT INTO "{table}" ({columns}) VALUES ({marks})', tuple(row.values())
        )
    connection.commit()
    connection.close()
    return source, rows


def _source_bytes(source):
    return {path.name: path.read_bytes() for path in source.parent.glob(source.name + "*")}


def test_sqlite_migration_copies_every_table(
    legacy_database, test_database_url, postgres_connection
):
    source, expected = legacy_database
    before = _source_bytes(source)
    report = sqlite_import.migrate_sqlite(source, test_database_url)
    conn = db.connect(test_database_url)
    try:
        assert set(report["tables"]) == set(expected)
        for table, source_row in expected.items():
            row = conn.execute(f'SELECT * FROM "{table}"').fetchone()
            assert row is not None
            for column, value in source_row.items():
                assert row[column] == value
            assert report["tables"][table]["source_rows"] == 1
            assert report["tables"][table]["destination_rows"] == 1
        assert conn.execute("SELECT source FROM daily_wellness").fetchone()["source"] == "fit"
        assert report["tables"]["daily_wellness"]["coverage"] == {
            "first": "2026-06-24",
            "last": "2026-06-24",
            "destination_first": "2026-06-24",
            "destination_last": "2026-06-24",
        }
    finally:
        conn.close()
    assert _source_bytes(source) == before


def test_sqlite_migration_is_idempotent(legacy_database, test_database_url, postgres_connection):
    source, _ = legacy_database
    first = sqlite_import.migrate_sqlite(source, test_database_url)
    second = sqlite_import.migrate_sqlite(source, test_database_url)
    assert second == first


def test_sqlite_migration_failure_keeps_source_unchanged(
    legacy_database, test_database_url, postgres_connection
):
    source, _ = legacy_database
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE daily_wellness SET steps=?", (2**40,))
    connection.commit()
    before = _source_bytes(source)

    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        sqlite_import.migrate_sqlite(source, test_database_url)

    conn = db.connect(test_database_url)
    try:
        assert conn.execute("SELECT COUNT(*) AS count FROM daily_wellness").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM sleep").fetchone()["count"] == 0
    finally:
        conn.close()
    assert _source_bytes(source) == before
    connection.close()
