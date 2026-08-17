import psycopg
import pytest

from garmin_mcp import db


def test_schema_created(conn):
    tables = {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"
        ).fetchall()
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
    version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert version == db.SCHEMA_VERSION


def test_migrate_idempotent(db_url):
    db.connect(db_url).close()
    conn = db.connect(db_url)  # reopen: migrations must not re-apply
    count = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert count == len(db.MIGRATIONS)
    conn.close()


def test_migration_v2_adds_fitness_age_columns(db_url):
    conn = db.connect(db_url)
    cols = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='training_status'"
        )
    }
    assert {"fitness_age", "achievable_fitness_age"} <= cols
    assert db.SCHEMA_VERSION == 2
    # data written after migration survives a re-open (migrations never re-apply)
    db.upsert(
        conn,
        "training_status",
        {"date": "2026-07-01", "vo2max": 47.3, "fitness_age": 41.23},
        ("date",),
    )
    conn.close()
    conn = db.connect(db_url)
    row = conn.execute("SELECT * FROM training_status").fetchone()
    assert row["vo2max"] == 47.3 and row["fitness_age"] == 41.23
    assert row["achievable_fitness_age"] is None
    conn.close()


def test_upsert_overwrites_non_key_cols(conn):
    row = {"date": "2026-07-01", "resting_hr": 56, "steps": 5000, "source": "api"}
    db.upsert(conn, "daily_wellness", row, ("date",))
    db.upsert(conn, "daily_wellness", {**row, "resting_hr": 58}, ("date",))
    got = conn.execute("SELECT * FROM daily_wellness").fetchall()
    assert len(got) == 1
    assert got[0]["resting_hr"] == 58
    assert got[0]["steps"] == 5000


def test_source_check_constraint(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.upsert(
            conn, "daily_wellness", {"date": "2026-07-01", "source": "chrome"}, ("date",)
        )


def test_sync_state(conn):
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "error", "boom")
    assert not db.synced_ok(conn, "sleep", "2026-07-01")
    db.mark_sync(conn, "sleep", "2026-07-01", "ok")
    assert db.synced_ok(conn, "sleep", "2026-07-01")
    row = conn.execute("SELECT * FROM sync_state").fetchone()
    assert row["attempts"] == 2
    db.mark_sync(conn, "hrv", "2026-07-01", "empty")
    assert db.synced_ok(conn, "hrv", "2026-07-01")


def test_drop_all_and_migrate_recreates_empty_schema(conn):
    db.upsert(conn, "daily_wellness", {"date": "2026-07-01", "source": "api"}, ("date",))
    db.drop_all(conn)
    db.migrate(conn)
    assert db.has_any_data(conn) is False
    version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert version == db.SCHEMA_VERSION
