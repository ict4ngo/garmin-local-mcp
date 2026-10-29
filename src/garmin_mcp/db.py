"""PostgreSQL store: schema migrations and generic upsert helpers.

The database is a derived index — raw JSON snapshots on disk are the source of
truth, and `reparse` can rebuild every table here from them offline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

# Each entry is a DDL script bringing the schema from version-1 to version.
# Additive changes and lossless widening conversions only; destructive changes
# go through reparse.
MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE daily_wellness (
      date TEXT PRIMARY KEY,
      resting_hr INT, min_hr INT, max_hr INT,
      steps INT, distance_m DOUBLE PRECISION, floors_up INT,
      stress_avg INT, stress_max INT,
      body_battery_high INT, body_battery_low INT,
      calories_total INT, calories_active INT,
      spo2_avg DOUBLE PRECISION, respiration_avg DOUBLE PRECISION,
      skin_temp_dev_c DOUBLE PRECISION,
      intensity_min_moderate INT, intensity_min_vigorous INT,
      source TEXT CHECK(source IN ('api','fit')), fetched_at TEXT
    );

    CREATE TABLE sleep (
      date TEXT PRIMARY KEY,
      score INT, duration_min DOUBLE PRECISION,
      deep_min INT, light_min INT, rem_min INT, awake_min INT,
      start_ts TEXT, end_ts TEXT,
      avg_spo2 DOUBLE PRECISION, avg_respiration DOUBLE PRECISION,
      avg_stress INT, restless_moments INT,
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
      duration_s DOUBLE PRECISION, distance_m DOUBLE PRECISION,
      elevation_gain_m DOUBLE PRECISION,
      avg_hr INT, max_hr INT, calories INT, avg_pace_s_per_km DOUBLE PRECISION,
      training_load DOUBLE PRECISION, aerobic_te DOUBLE PRECISION,
      anaerobic_te DOUBLE PRECISION,
      raw_path TEXT, fetched_at TEXT
    );
    CREATE INDEX ix_activities_date ON activities(date);
    CREATE INDEX ix_activities_type ON activities(type);

    CREATE TABLE training_status (
      date TEXT PRIMARY KEY, status TEXT, vo2max DOUBLE PRECISION,
      acute_load DOUBLE PRECISION, load_ratio DOUBLE PRECISION, fetched_at TEXT
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
    ALTER TABLE training_status ADD COLUMN fitness_age DOUBLE PRECISION;
    ALTER TABLE training_status ADD COLUMN achievable_fitness_age DOUBLE PRECISION;
    """,
    3: """
    ALTER TABLE daily_wellness ALTER COLUMN distance_m TYPE DOUBLE PRECISION
      USING distance_m::DOUBLE PRECISION;
    ALTER TABLE daily_wellness ALTER COLUMN spo2_avg TYPE DOUBLE PRECISION
      USING spo2_avg::DOUBLE PRECISION;
    ALTER TABLE daily_wellness ALTER COLUMN respiration_avg TYPE DOUBLE PRECISION
      USING respiration_avg::DOUBLE PRECISION;
    ALTER TABLE daily_wellness ALTER COLUMN skin_temp_dev_c TYPE DOUBLE PRECISION
      USING skin_temp_dev_c::DOUBLE PRECISION;
    ALTER TABLE sleep ALTER COLUMN duration_min TYPE DOUBLE PRECISION
      USING duration_min::DOUBLE PRECISION;
    ALTER TABLE sleep ALTER COLUMN avg_spo2 TYPE DOUBLE PRECISION
      USING avg_spo2::DOUBLE PRECISION;
    ALTER TABLE sleep ALTER COLUMN avg_respiration TYPE DOUBLE PRECISION
      USING avg_respiration::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN duration_s TYPE DOUBLE PRECISION
      USING duration_s::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN distance_m TYPE DOUBLE PRECISION
      USING distance_m::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN elevation_gain_m TYPE DOUBLE PRECISION
      USING elevation_gain_m::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN avg_pace_s_per_km TYPE DOUBLE PRECISION
      USING avg_pace_s_per_km::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN training_load TYPE DOUBLE PRECISION
      USING training_load::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN aerobic_te TYPE DOUBLE PRECISION
      USING aerobic_te::DOUBLE PRECISION;
    ALTER TABLE activities ALTER COLUMN anaerobic_te TYPE DOUBLE PRECISION
      USING anaerobic_te::DOUBLE PRECISION;
    ALTER TABLE training_status ALTER COLUMN vo2max TYPE DOUBLE PRECISION
      USING vo2max::DOUBLE PRECISION;
    ALTER TABLE training_status ALTER COLUMN acute_load TYPE DOUBLE PRECISION
      USING acute_load::DOUBLE PRECISION;
    ALTER TABLE training_status ALTER COLUMN load_ratio TYPE DOUBLE PRECISION
      USING load_ratio::DOUBLE PRECISION;
    ALTER TABLE training_status ALTER COLUMN fitness_age TYPE DOUBLE PRECISION
      USING fitness_age::DOUBLE PRECISION;
    ALTER TABLE training_status ALTER COLUMN achievable_fitness_age TYPE DOUBLE PRECISION
      USING achievable_fitness_age::DOUBLE PRECISION;
    """,
    4: """
    CREATE TABLE samples (
      source_endpoint TEXT NOT NULL,
      source_id TEXT NOT NULL,
      metric TEXT NOT NULL,
      observed_at TIMESTAMPTZ NOT NULL,
      source_date TEXT NOT NULL,
      sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
      value DOUBLE PRECISION NOT NULL,
      unit TEXT,
      local_timestamp TEXT,
      fetched_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY (source_endpoint, source_id, metric, observed_at, sample_index)
    );
    CREATE INDEX ix_samples_observed_at ON samples(observed_at);
    CREATE INDEX ix_samples_metric_observed_at ON samples(metric, observed_at);

    CREATE TABLE activity_detail_state (
      activity_id BIGINT PRIMARY KEY,
      sha256 TEXT NOT NULL,
      raw_path TEXT NOT NULL,
      fetched_at TIMESTAMPTZ NOT NULL
    );
    """,
    5: """
    CREATE TABLE backfill_state (
      endpoint TEXT PRIMARY KEY,
      cursor_date TEXT,
      last_date TEXT,
      empty_streak INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL CHECK(status IN ('pending','running','complete','error')),
      last_error TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE worker_state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
    6: """
    CREATE TABLE sqlite_import_marker (
      singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
      source_fingerprint TEXT NOT NULL UNIQUE,
      imported_at TIMESTAMPTZ NOT NULL,
      report JSONB NOT NULL
    );
    """,
}

SCHEMA_VERSION = max(MIGRATIONS)
SYNC_ADVISORY_LOCK_KEY = 0x4741524D53594E43


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_precise() -> str:
    return datetime.now(UTC).isoformat()


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


@contextmanager
def sync_advisory_lock(conn: psycopg.Connection) -> Iterator[None]:
    """Serialize every manual, scheduled, and backfill Garmin sync on this DB."""
    conn.execute("SELECT pg_advisory_lock(%s)", (SYNC_ADVISORY_LOCK_KEY,))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (SYNC_ADVISORY_LOCK_KEY,))


def set_worker_state(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO worker_state (key, value, updated_at) VALUES (%s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            (key, value, utcnow()),
        )


def get_worker_state(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM worker_state WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else None


def throttle_garmin_request(
    conn: psycopg.Connection, delay_seconds: float, sleep: Callable[[float], None]
) -> None:
    """Enforce the configured spacing across separate sync calls and processes."""
    now = datetime.now(UTC)
    previous = get_worker_state(conn, "last_garmin_request_at")
    if previous is not None:
        previous_at = datetime.fromisoformat(previous)
        elapsed = (now - previous_at).total_seconds()
        remaining = delay_seconds - elapsed
        if remaining > 0:
            sleep(remaining)
            now = datetime.now(UTC)
    set_worker_state(conn, "last_garmin_request_at", now.isoformat())
