"""One-way, read-only migration from the legacy SQLite warehouse."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import db

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "daily_wellness": ("date",),
    "sleep": ("date",),
    "hrv": ("date",),
    "activities": ("activity_id",),
    "training_status": ("date",),
    "raw_snapshots": ("path",),
    "sync_state": ("endpoint", "date"),
    "demo_info": (),
}


def _source_coverage(source: sqlite3.Connection, table: str) -> dict[str, str | None] | None:
    columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
    if "date" not in columns:
        return None
    row = source.execute(f'SELECT MIN(date), MAX(date) FROM "{table}"').fetchone()
    return {"first": row[0], "last": row[1]}


def _destination_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def migrate_sqlite(source: Path, database_url: str) -> dict:
    """Copy legacy tables into PostgreSQL without writing to the SQLite source."""
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"SQLite source is not a regular file: {source}")

    # SQLite may update the WAL shared-memory lock bookkeeping even for a
    # read-only connection. Work from byte copies so the user's database and
    # any live WAL/SHM sidecars remain byte-for-byte untouched.
    with tempfile.TemporaryDirectory(prefix="garmin-sqlite-import-") as temporary:
        snapshot = Path(temporary) / source.name
        for suffix in ("", "-wal", "-shm", "-journal"):
            original = Path(f"{source}{suffix}")
            if not original.exists():
                continue
            if original.is_symlink() or not original.is_file():
                raise ValueError(f"SQLite source sidecar is not a regular file: {original}")
            shutil.copyfile(original, Path(f"{snapshot}{suffix}"))

        sqlite_conn = sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)
        sqlite_conn.row_factory = sqlite3.Row
        target = None
        report: dict = {"tables": {}}
        try:
            target = db.connect(database_url)
            present = {
                row[0]
                for row in sqlite_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = set(TABLE_KEYS) - present
            if missing:
                raise ValueError(f"SQLite database is missing tables: {', '.join(sorted(missing))}")

            with target.transaction():
                for table, key in TABLE_KEYS.items():
                    source_columns = [
                        row[1] for row in sqlite_conn.execute(f'PRAGMA table_info("{table}")')
                    ]
                    target_columns = _destination_columns(target, table)
                    columns = [column for column in source_columns if column in target_columns]
                    if not columns:
                        raise ValueError(f"No compatible columns found for SQLite table {table}")
                    quoted = ", ".join(f'"{column}"' for column in columns)
                    values_sql = ", ".join("%s" for _ in columns)
                    if key:
                        updates = [column for column in columns if column not in key]
                        conflict = ", ".join(f'"{column}"' for column in key)
                        action = (
                            "DO UPDATE SET "
                            + ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in updates)
                            if updates
                            else "DO NOTHING"
                        )
                        insert_sql = (
                            f'INSERT INTO "{table}" ({quoted}) VALUES ({values_sql}) '
                            f'ON CONFLICT ({conflict}) {action}'
                        )
                    else:
                        # demo_info has no key; replace it as a whole so retries are idempotent.
                        target.execute(f'DELETE FROM "{table}"')
                        insert_sql = f'INSERT INTO "{table}" ({quoted}) VALUES ({values_sql})'

                    source_rows = sqlite_conn.execute(f'SELECT {quoted} FROM "{table}"')
                    source_count = 0
                    for row in source_rows:
                        target.execute(insert_sql, tuple(row[column] for column in columns))
                        source_count += 1

                    destination_count = target.execute(
                        f'SELECT COUNT(*) AS count FROM "{table}"'
                    ).fetchone()["count"]
                    coverage = _source_coverage(sqlite_conn, table)
                    if coverage is not None:
                        destination_coverage = target.execute(
                            f'SELECT MIN(date) AS first, MAX(date) AS last FROM "{table}"'
                        ).fetchone()
                        coverage = {
                            "first": coverage["first"],
                            "last": coverage["last"],
                            "destination_first": destination_coverage["first"],
                            "destination_last": destination_coverage["last"],
                        }
                    report["tables"][table] = {
                        "source_rows": source_count,
                        "destination_rows": destination_count,
                        "coverage": coverage,
                    }
        finally:
            if target is not None:
                target.close()
            sqlite_conn.close()
    return report
