import pytest

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
