"""PostgreSQL store: schema migrations and generic upsert helpers.

The database is a derived index — raw JSON snapshots on disk are the source of
truth, and `reparse` can rebuild every table here from them offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

# Each entry is a full DDL script bringing the schema from version-1 to version.
# Additive changes only; anything structural goes through reparse.
MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE daily_wellness (
      date TEXT PRIMARY KEY,
      resting_hr INT, min_hr INT, max_hr INT,
      steps INT, distance_m REAL, floors_up INT,
      stress_avg INT, stress_max INT,
      body_battery_high INT, body_battery_low INT,
      calories_total INT, calories_active INT,
      spo2_avg REAL, respiration_avg REAL,
      skin_temp_dev_c REAL,
      intensity_min_moderate INT, intensity_min_vigorous INT,
      source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT
    );

    CREATE TABLE sleep (
      date TEXT PRIMARY KEY,
      score INT, duration_min REAL,
      deep_min INT, light_min INT, rem_min INT, awake_min INT,
      start_ts TEXT, end_ts TEXT,
      avg_spo2 REAL, avg_respiration REAL, avg_stress INT, restless_moments INT,
      nap_min INT,
      quality_flags TEXT,
      source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT
    );

    CREATE TABLE hrv (
      date TEXT PRIMARY KEY,
      last_night_avg INT, weekly_avg INT, high_5min INT,
      status TEXT,
      baseline_low INT, baseline_high INT,
      source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT
    );

    CREATE TABLE activities (
      activity_id BIGINT PRIMARY KEY,
      date TEXT, start_ts TEXT, name TEXT, type TEXT,
      duration_s REAL, distance_m REAL, elevation_gain_m REAL,
      avg_hr INT, max_hr INT, calories INT, avg_pace_s_per_km REAL,
      training_load REAL, aerobic_te REAL, anaerobic_te REAL,
      raw_path TEXT, fetched_at TEXT
    );
    CREATE INDEX ix_activities_date ON activities(date);
    CREATE INDEX ix_activities_type ON activities(type);

    CREATE TABLE training_status (
      date TEXT PRIMARY KEY, status TEXT, vo2max REAL,
      acute_load REAL, load_ratio REAL, fetched_at TEXT
    );

    CREATE TABLE raw_snapshots (
      path TEXT PRIMARY KEY, date TEXT, endpoint TEXT,
      fetched_at TEXT, bytes INT, sha256 TEXT
    );

    CREATE TABLE sync_state (
      endpoint TEXT, date TEXT,
      status TEXT CHECK(status IN ('ok','empty','error')),
      attempts INT DEFAULT 0, last_error TEXT, updated_at TEXT,
      PRIMARY KEY (endpoint, date)
    );

    CREATE TABLE demo_info (
      generated_at TEXT, days INT, "start" TEXT, "end" TEXT, seed INT
    );
    """,
    2: """
    ALTER TABLE training_status ADD COLUMN fitness_age REAL;
    ALTER TABLE training_status ADD COLUMN achievable_fitness_age REAL;
    """,
}

SCHEMA_VERSION = max(MIGRATIONS)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(database_url: str) -> psycopg.Connection:
    """Open the configured PostgreSQL warehouse, creating/migrating as needed."""
    conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def migrate(conn: psycopg.Connection) -> None:
    # App and worker may initialize simultaneously on first deployment.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x4741524D494E,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
        ).fetchone()["version"]
        for version in sorted(MIGRATIONS):
            if version > current:
                for statement in MIGRATIONS[version].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (%s, %s)",
                    (version, utcnow()),
                )


def upsert(conn: psycopg.Connection, table: str, row: dict, key_cols: tuple[str, ...]) -> None:
    """Insert or update one row. Non-key columns present in `row` are overwritten."""
    upsert_partial(conn, table, row, key_cols)


def upsert_partial(
    conn: psycopg.Connection, table: str, row: dict, key_cols: tuple[str, ...]
) -> None:
    """Insert or update one row, setting only the columns present in `row` on conflict.

    Columns absent from `row` are left untouched, so several endpoints can each
    contribute their slice of the same row (e.g. usersummary and sleep both
    feeding daily_wellness) in any order without clobbering each other.
    """
    cols = list(row)
    placeholders = ", ".join("%s" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key_cols)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key_cols)}) {action}"
    )
    with conn.transaction():
        conn.execute(sql, [row[c] for c in cols])


def mark_sync(
    conn: psycopg.Connection,
    endpoint: str,
    date: str,
    status: str,
    error: str | None = None,
) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO sync_state (endpoint, date, status, attempts, last_error, updated_at)
               VALUES (%s, %s, %s, 1, %s, %s)
               ON CONFLICT(endpoint, date) DO UPDATE SET
                 status=excluded.status, attempts=sync_state.attempts+1,
                 last_error=excluded.last_error, updated_at=excluded.updated_at""",
            (endpoint, date, status, error, utcnow()),
        )


def synced_ok(conn: psycopg.Connection, endpoint: str, date: str) -> bool:
    row = conn.execute(
        "SELECT status FROM sync_state WHERE endpoint=%s AND date=%s", (endpoint, date)
    ).fetchone()
    return row is not None and row["status"] in ("ok", "empty")
