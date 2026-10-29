import sqlite3
from types import SimpleNamespace

import psycopg
import pytest

from garmin_mcp import cli, db, sqlite_import


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


@pytest.fixture
def legacy_warehouse_without_demo_info(legacy_database):
    source, expected = legacy_database
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE demo_info")
    expected.pop("demo_info")
    return source, expected


def _source_bytes(source):
    return {path.name: path.read_bytes() for path in source.parent.glob(source.name + "*")}


def test_sqlite_migration_copies_every_table(
    legacy_database, test_database_url, postgres_connection
):
    source, expected = legacy_database
    before = _source_bytes(source)
    report = sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)
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


def test_sqlite_migration_is_idempotent_without_overwriting_newer_rows(
    legacy_database, test_database_url, postgres_connection
):
    source, _ = legacy_database
    first = sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)
    postgres_connection.execute("UPDATE daily_wellness SET steps=9999 WHERE date='2026-06-24'")
    second = sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)
    assert first["migration"]["already_imported"] is False
    assert second["migration"]["already_imported"] is True
    marker = postgres_connection.execute(
        "SELECT source_fingerprint, report FROM sqlite_import_marker WHERE singleton"
    ).fetchone()
    assert marker["source_fingerprint"] == first["migration"]["source_fingerprint"]
    assert marker["report"]["migration"]["already_imported"] is False
    assert postgres_connection.execute(
        "SELECT steps FROM daily_wellness WHERE date='2026-06-24'"
    ).fetchone()["steps"] == 9999


def test_sqlite_migration_allows_missing_demo_info(
    legacy_warehouse_without_demo_info, test_database_url, postgres_connection
):
    source, expected = legacy_warehouse_without_demo_info

    report = sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)

    assert set(report["tables"]) == set(expected) | {"demo_info"}
    assert report["tables"]["demo_info"] == {
        "source_rows": 0,
        "destination_rows": 0,
        "coverage": None,
        "present": False,
    }


@pytest.mark.parametrize(
    "table,insert_sql",
    [
        ("daily_wellness", "INSERT INTO daily_wellness (date,steps) VALUES ('2026-01-01',1)"),
        (
            "samples",
            "INSERT INTO samples VALUES ('stress','x','stress',NOW(),'2026-01-01',"
            "0,1,NULL,NULL,NOW())",
        ),
        ("activity_detail_state", "INSERT INTO activity_detail_state VALUES (1,'x','x',NOW())"),
        (
            "backfill_state",
            "INSERT INTO backfill_state VALUES ('stress',NULL,NULL,0,'pending',NULL,NOW()::text)",
        ),
        ("worker_state", "INSERT INTO worker_state VALUES ('last_sync','now',NOW()::text)"),
    ],
)
def test_sqlite_migration_refuses_nonempty_app_tables(
    legacy_database, test_database_url, postgres_connection, table, insert_sql
):
    source, _ = legacy_database
    postgres_connection.execute(insert_sql)

    with pytest.raises(ValueError, match="empty PostgreSQL warehouse"):
        sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)


def test_sqlite_migration_rejects_a_different_source_after_import(
    legacy_database, tmp_path, test_database_url, postgres_connection
):
    source, _ = legacy_database
    sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)
    other = tmp_path / "different.sqlite"
    other.write_bytes(source.read_bytes() + b"different source")

    with pytest.raises(ValueError, match="different SQLite source"):
        sqlite_import.migrate_sqlite(other, test_database_url, source_stopped=True)


def test_sqlite_migration_requires_stopped_source(legacy_database, test_database_url):
    source, _ = legacy_database

    with pytest.raises(ValueError, match="source_stopped=True"):
        sqlite_import.migrate_sqlite(source, test_database_url)


def test_migrate_sqlite_cli_requires_source_stopped_flag():
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["migrate-sqlite", "--from", "legacy.sqlite"])
    assert error.value.code == 2


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
        sqlite_import.migrate_sqlite(source, test_database_url, source_stopped=True)

    conn = db.connect(test_database_url)
    try:
        assert conn.execute("SELECT COUNT(*) AS count FROM daily_wellness").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM sleep").fetchone()["count"] == 0
    finally:
        conn.close()
    assert _source_bytes(source) == before
    connection.close()


def test_migrate_sqlite_cli_redacts_exception_details(monkeypatch, capsys):
    secret = "postgresql://garmin:recognizable-password@db.example/warehouse"

    def fail_migration(source, database_url, *, source_stopped=False):
        raise RuntimeError(f"connection failed for {secret}")

    monkeypatch.setattr(sqlite_import, "migrate_sqlite", fail_migration)
    args = SimpleNamespace(
        source="legacy.sqlite", source_stopped=True, config=SimpleNamespace(database_url=secret)
    )

    assert cli.cmd_migrate_sqlite(args) == 1
    stderr = capsys.readouterr().err
    assert "SQLite migration failed" in stderr
    assert "Verify the source file" in stderr
    assert secret not in stderr
    assert "recognizable-password" not in stderr
