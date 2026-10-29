import psycopg

from garmin_mcp import db


def test_schema_created(postgres_connection):
    tables = {
        row["table_name"]
        for row in postgres_connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema()"
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
        "backfill_state",
        "worker_state",
    } <= tables
    version = postgres_connection.execute(
        "SELECT MAX(version) AS version FROM schema_version"
    ).fetchone()["version"]
    assert version == db.SCHEMA_VERSION


def test_migrate_idempotent(postgres_connection):
    db.migrate(postgres_connection)
    count = postgres_connection.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert count == len(db.MIGRATIONS)


def test_migration_v2_adds_fitness_age_columns(postgres_connection):
    cols = {
        row["column_name"]
        for row in postgres_connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='training_status'"
        )
    }
    assert {"fitness_age", "achievable_fitness_age"} <= cols
    assert db.SCHEMA_VERSION == 5
    db.upsert(
        postgres_connection,
        "training_status",
        {"date": "2026-07-01", "vo2max": 47.3, "fitness_age": 41.23},
        ("date",),
    )
    row = postgres_connection.execute("SELECT * FROM training_status").fetchone()
    assert row["vo2max"] == 47.3 and row["fitness_age"] == 41.23
    assert row["achievable_fitness_age"] is None


def test_upsert_overwrites_non_key_cols(postgres_connection):
    row = {"date": "2026-07-01", "resting_hr": 56, "steps": 5000, "source": "api"}
    db.upsert(postgres_connection, "daily_wellness", row, ("date",))
    db.upsert(postgres_connection, "daily_wellness", {**row, "resting_hr": 58}, ("date",))
    got = postgres_connection.execute("SELECT * FROM daily_wellness").fetchall()
    assert len(got) == 1
    assert got[0]["resting_hr"] == 58
    assert got[0]["steps"] == 5000


def test_source_check_constraint(postgres_connection):
    try:
        db.upsert(
            postgres_connection, "daily_wellness", {"date": "2026-07-01", "source": "chrome"},
            ("date",),
        )
        raised = False
    except psycopg.errors.CheckViolation:
        raised = True
    assert raised


def test_sync_state(postgres_connection):
    conn = postgres_connection
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "error", "boom")
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "ok")
    assert db.synced_ok(conn, "sleep", "2026-07-01")
    row = conn.execute("SELECT * FROM sync_state").fetchone()
    assert row["attempts"] == 2
    db.mark_sync(conn, "hrv", "2026-07-01", "empty")
    assert db.synced_ok(conn, "hrv", "2026-07-01")
