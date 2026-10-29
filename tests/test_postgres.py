import tomllib
from pathlib import Path

import pytest

from conftest import _validated_test_database_url
from garmin_mcp import db


def test_schema_created(postgres_connection):
    conn = postgres_connection
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
    }
    assert {
        "schema_version",
        "daily_wellness",
        "sleep",
        "hrv",
        "activities",
        "training_status",
        "raw_snapshots",
        "sync_state",
    } <= tables
    version = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    assert version["version"] == db.SCHEMA_VERSION


def test_migrate_idempotent(postgres_connection):
    db.migrate(postgres_connection)
    count = postgres_connection.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert count == len(db.MIGRATIONS)


def test_upsert_partial_preserves_existing_columns(postgres_connection):
    db.upsert(
        postgres_connection,
        "daily_wellness",
        {"date": "2026-07-01", "steps": 5000, "resting_hr": 56},
        ("date",),
    )
    db.upsert_partial(
        postgres_connection,
        "daily_wellness",
        {"date": "2026-07-01", "resting_hr": 58},
        ("date",),
    )
    row = postgres_connection.execute(
        "SELECT steps, resting_hr FROM daily_wellness WHERE date=%s", ("2026-07-01",)
    ).fetchone()
    assert row == {"steps": 5000, "resting_hr": 58}


def test_sync_state_retry_count(postgres_connection):
    db.mark_sync(postgres_connection, "sleep", "2026-07-01", "error", "temporary")
    db.mark_sync(postgres_connection, "sleep", "2026-07-01", "ok")
    row = postgres_connection.execute(
        "SELECT attempts, status FROM sync_state WHERE endpoint=%s AND date=%s",
        ("sleep", "2026-07-01"),
    ).fetchone()
    assert row == {"attempts": 2, "status": "ok"}
    assert db.synced_ok(postgres_connection, "sleep", "2026-07-01")


def test_transaction_rolls_back(postgres_connection):
    with pytest.raises(RuntimeError, match="rollback"):
        with postgres_connection.transaction():
            db.upsert(
                postgres_connection,
                "daily_wellness",
                {"date": "2026-07-01", "steps": 5000},
                ("date",),
            )
            raise RuntimeError("rollback")

    count = postgres_connection.execute("SELECT COUNT(*) AS n FROM daily_wellness").fetchone()["n"]
    assert count == 0


def test_real_values_preserve_double_precision(postgres_connection):
    value = 12345.678901234567
    db.upsert(
        postgres_connection,
        "daily_wellness",
        {"date": "2026-07-01", "distance_m": value},
        ("date",),
    )
    stored = postgres_connection.execute(
        "SELECT distance_m FROM daily_wellness WHERE date=%s", ("2026-07-01",)
    ).fetchone()["distance_m"]
    assert stored == value


def test_test_database_url_is_safely_isolated(test_database_url):
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(test_database_url)["dbname"].endswith("_test")


def test_test_database_url_rejects_non_test_database(monkeypatch, request):
    monkeypatch.setenv(
        "GARMIN_MCP_TEST_DATABASE_URL", "postgresql://garmin:garmin@localhost:55432/garmin"
    )
    monkeypatch.delenv("GARMIN_MCP_DATABASE_URL", raising=False)
    with pytest.raises(pytest.fail.Exception, match="database name must end in _test"):
        request.getfixturevalue("test_database_url")


def test_test_database_url_rejects_application_database(monkeypatch, request):
    database_url = "postgresql://garmin:garmin@localhost:55432/garmin_test"
    monkeypatch.setenv("GARMIN_MCP_TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", database_url)
    with pytest.raises(pytest.fail.Exception, match="GARMIN_MCP_DATABASE_URL must be unset"):
        request.getfixturevalue("test_database_url")


def test_test_database_url_rejects_preexisting_application_url_across_aliases():
    environment = {
        "GARMIN_MCP_TEST_DATABASE_URL": (
            "postgresql://garmin:garmin@localhost:55432/garmin_test"
        ),
        "GARMIN_MCP_DATABASE_URL": "postgresql://garmin:garmin@postgres:5432/garmin_test",
    }
    with pytest.raises(ValueError, match="GARMIN_MCP_DATABASE_URL must be unset"):
        _validated_test_database_url(environment)


def test_runtime_dependency_floors():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert "mcp>=1.30,<2" in project["dependencies"]
    assert "garminconnect>=0.3.7,<0.4" in project["dependencies"]
