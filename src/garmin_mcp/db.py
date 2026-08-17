"""PostgreSQL store: schema, migrations, generic upserts.

The database is a derived index — raw JSON snapshots on disk are the source of
truth, and `reparse` can rebuild every table here from them offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

# Connection type used across the codebase (psycopg v3, dict rows).
Connection = psycopg.Connection

DEFAULT_DATABASE_URL = "postgresql://garmin:garmin@localhost:5432/garmin"

# Data tables; sync_state and raw_snapshots are bookkeeping, demo_info is an
# optional provenance marker created by the demo command.
TABLES = ("daily_wellness", "sleep", "hrv", "training_status", "activities")

# Each entry is the list of DDL statements bringing the schema from version-1
# to version. Additive changes only; anything structural goes through reparse.
MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE daily_wellness (
          date TEXT PRIMARY KEY,
          resting_hr INTEGER, min_hr INTEGER, max_hr INTEGER,
          steps INTEGER, distance_m DOUBLE PRECISION, floors_up INTEGER,
          stress_avg INTEGER, stress_max INTEGER,
          body_battery_high INTEGER, body_battery_low INTEGER,
          calories_total INTEGER, calories_active INTEGER,
          spo2_avg DOUBLE PRECISION, respiration_avg DOUBLE PRECISION,
          skin_temp_dev_c DOUBLE PRECISION,
          intensity_min_moderate INTEGER, intensity_min_vigorous INTEGER,
          source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT)
        """,
        """
        CREATE TABLE sleep (
          date TEXT PRIMARY KEY,
          score INTEGER, duration_min DOUBLE PRECISION,
          deep_min INTEGER, light_min INTEGER, rem_min INTEGER, awake_min INTEGER,
          start_ts TEXT, end_ts TEXT,
          avg_spo2 DOUBLE PRECISION, avg_respiration DOUBLE PRECISION,
          avg_stress INTEGER, restless_moments INTEGER,
          nap_min INTEGER,
          quality_flags TEXT,
          source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT)
        """,
        """
        CREATE TABLE hrv (
          date TEXT PRIMARY KEY,
          last_night_avg INTEGER, weekly_avg INTEGER, high_5min INTEGER,
          status TEXT,
          baseline_low INTEGER, baseline_high INTEGER,
          source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT)
        """,
        """
        CREATE TABLE activities (
          activity_id BIGINT PRIMARY KEY,
          date TEXT, start_ts TEXT, name TEXT, type TEXT,
          duration_s DOUBLE PRECISION, distance_m DOUBLE PRECISION,
          elevation_gain_m DOUBLE PRECISION,
          avg_hr INTEGER, max_hr INTEGER, calories INTEGER,
          avg_pace_s_per_km DOUBLE PRECISION,
          training_load DOUBLE PRECISION,
          aerobic_te DOUBLE PRECISION, anaerobic_te DOUBLE PRECISION,
          raw_path TEXT, fetched_at TEXT)
        """,
        "CREATE INDEX ix_activities_date ON activities(date)",
        "CREATE INDEX ix_activities_type ON activities(type)",
        """
        CREATE TABLE training_status (
          date TEXT PRIMARY KEY, status TEXT, vo2max DOUBLE PRECISION,
          acute_load DOUBLE PRECISION, load_ratio DOUBLE PRECISION, fetched_at TEXT)
        """,
        """
        CREATE TABLE raw_snapshots (
          path TEXT PRIMARY KEY, date TEXT, endpoint TEXT,
          fetched_at TEXT, bytes BIGINT, sha256 TEXT)
        """,
        """
        CREATE TABLE sync_state (
          endpoint TEXT, date TEXT,
          status TEXT CHECK(status IN ('ok','empty','error')),
          attempts INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT,
          PRIMARY KEY (endpoint, date))
        """,
    ],
    2: [
        "ALTER TABLE training_status ADD COLUMN fitness_age DOUBLE PRECISION",
        "ALTER TABLE training_status ADD COLUMN achievable_fitness_age DOUBLE PRECISION",
    ],
}

SCHEMA_VERSION = max(MIGRATIONS)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(database_url: str) -> Connection:
    """Open (migrating if needed) the warehouse database.

    autocommit=True matches the old per-statement commit semantics: single
    statements commit immediately, and multi-statement atomicity is opt-in via
    ``with conn.transaction():`` blocks.
    """
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=True)
    migrate(conn)
    return conn


def migrate(conn: Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    current = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()[
        "v"
    ]
    for version in sorted(MIGRATIONS):
        if version > current:
            with conn.transaction():
                for statement in MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (%s, %s)",
                    (version, utcnow()),
                )


def drop_all(conn: Connection) -> None:
    """Drop every warehouse table (used by demo regeneration and tests)."""
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS demo_info")
        conn.execute("DROP TABLE IF EXISTS schema_version")
        for table in (*TABLES, "raw_snapshots", "sync_state"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # noqa: S608 - fixed names


def has_any_data(conn: Connection) -> bool:
    """True when any data table holds at least one row."""
    for table in TABLES:
        row = conn.execute(f"SELECT 1 AS one FROM {table} LIMIT 1").fetchone()  # noqa: S608
        if row is not None:
            return True
    return False


def upsert(conn: Connection, table: str, row: dict, key_cols: tuple[str, ...]) -> None:
    """Insert or update one row. Non-key columns present in `row` are overwritten."""
    upsert_partial(conn, table, row, key_cols)


def upsert_partial(
    conn: Connection, table: str, row: dict, key_cols: tuple[str, ...]
) -> None:
    """Insert or update one row, setting only the columns present in `row` on conflict.

    Columns absent from `row` are left untouched, so several endpoints can each
    contribute their slice of the same row (e.g. usersummary and sleep both
    feeding daily_wellness) in any order without clobbering each other.
    """
    cols = list(row)
    placeholders = ", ".join("%s" for _ in cols)
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in key_cols)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key_cols)}) {action}"
    )
    conn.execute(sql, [row[c] for c in cols])


def mark_sync(
    conn: Connection,
    endpoint: str,
    date: str,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO sync_state (endpoint, date, status, attempts, last_error, updated_at)
           VALUES (%s, %s, %s, 1, %s, %s)
           ON CONFLICT(endpoint, date) DO UPDATE SET
             status=EXCLUDED.status, attempts=sync_state.attempts+1,
             last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at""",
        (endpoint, date, status, error, utcnow()),
    )


def synced_ok(conn: Connection, endpoint: str, date: str) -> bool:
    row = conn.execute(
        "SELECT status FROM sync_state WHERE endpoint=%s AND date=%s", (endpoint, date)
    ).fetchone()
    return row is not None and row["status"] in ("ok", "empty")
