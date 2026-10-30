"""One-way, read-only migration from the legacy SQLite warehouse."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

from psycopg.types.json import Jsonb

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
OPTIONAL_SOURCE_TABLES = frozenset({"demo_info"})
APP_DATA_TABLES = (
    *TABLE_KEYS,
    "samples",
    "activity_detail_state",
    "backfill_state",
    "worker_state",
)
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


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


def _fingerprint(files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for name, path in files:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _target_has_data(target) -> bool:
    return any(
        target.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}") AS present').fetchone()[
            "present"
        ]
        for table in APP_DATA_TABLES
    )


def migrate_sqlite(source: Path, database_url: str, *, source_stopped: bool = False) -> dict:
    """Copy a quiescent legacy SQLite warehouse into an empty PostgreSQL target.

    The source and its SQLite journal sidecars are copied to a temporary
    directory before inspection, so SQLite cannot mutate the original files.
    """
    if source_stopped is not True:
        raise ValueError(
            "SQLite migration requires source_stopped=True; stop every SQLite writer first"
        )

    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"SQLite source is not a regular file: {source}")

    with tempfile.TemporaryDirectory(prefix="garmin-sqlite-import-") as temporary:
        snapshot = Path(temporary) / source.name
        source_files: list[tuple[str, Path]] = []
        for suffix in ("", *SQLITE_SIDECARS):
            original = Path(f"{source}{suffix}")
            if original.is_symlink():
                raise ValueError(f"SQLite source sidecar is not a regular file: {original}")
            if not original.exists():
                continue
            if not original.is_file():
                raise ValueError(f"SQLite source sidecar is not a regular file: {original}")
            copied = Path(f"{snapshot}{suffix}")
            shutil.copyfile(original, copied)
            source_files.append((suffix or "database", copied))

        fingerprint = _fingerprint(source_files)
        sqlite_conn = sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)
        sqlite_conn.row_factory = sqlite3.Row
        target = None
        try:
            target = db.connect(database_url)
            present = {
                row[0]
                for row in sqlite_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = set(TABLE_KEYS) - OPTIONAL_SOURCE_TABLES - present
            if missing:
                raise ValueError(f"SQLite database is missing tables: {', '.join(sorted(missing))}")

            with target.transaction():
                target.execute("SELECT pg_advisory_xact_lock(%s)", (0x4741524D53514C49,))
                previous = target.execute(
                    "SELECT source_fingerprint, report FROM sqlite_import_marker WHERE singleton"
                ).fetchone()
                if previous is not None:
                    if previous["source_fingerprint"] != fingerprint:
                        raise ValueError(
                            "A different SQLite source was already imported into this warehouse"
                        )
                    report = previous["report"]
                    report["migration"]["already_imported"] = True
                    return report

                target.execute(
                    "LOCK TABLE "
                    + ", ".join(f'"{table}"' for table in APP_DATA_TABLES)
                    + " IN SHARE ROW EXCLUSIVE MODE"
                )
                if _target_has_data(target):
                    raise ValueError(
                        "SQLite migration requires an empty PostgreSQL warehouse; "
                        "refusing to overwrite existing data"
                    )

                report: dict = {"tables": {}}
                for table, key in TABLE_KEYS.items():
                    if table not in present:
                        report["tables"][table] = {
                            "source_rows": 0,
                            "destination_rows": 0,
                            "coverage": None,
                            "present": False,
                        }
                        continue

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
                            + ", ".join(f'"{column}"=EXCLUDED."{column}"' for column in updates)
                            if updates
                            else "DO NOTHING"
                        )
                        insert_sql = (
                            f'INSERT INTO "{table}" ({quoted}) VALUES ({values_sql}) '
                            f"ON CONFLICT ({conflict}) {action}"
                        )
                    else:
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
                        "present": True,
                    }

                report["migration"] = {
                    "source_fingerprint": fingerprint,
                    "already_imported": False,
                }
                target.execute(
                    "INSERT INTO sqlite_import_marker "
                    "(singleton, source_fingerprint, imported_at, report) "
                    "VALUES (TRUE, %s, %s, %s)",
                    (fingerprint, db.utcnow(), Jsonb(report)),
                )
        finally:
            if target is not None:
                target.close()
            sqlite_conn.close()
    return report
