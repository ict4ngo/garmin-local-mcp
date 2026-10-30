"""Offline ingest of decoded FIT bundles into the warehouse.

FIT-sourced rows are the fallback layer: they fill days the API hasn't (or
can't) provide, but an existing API-sourced row is authoritative and is never
overwritten unless the caller forces it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from psycopg import Connection

from . import raw
from .db import upsert
from .fitdecode import decode_bundle, to_rows


def import_bundle(
    conn: Connection, folder: Path, force: bool = False, raw_dir: Path | None = None
) -> dict:
    """Archive and decode one export bundle, then upsert its rows."""
    folder = Path(folder)
    if raw_dir is None:
        from .config import DEFAULT_DATA_DIR, _resolve_path

        data_dir = _resolve_path(os.environ.get("GARMIN_MCP_DATA_DIR", DEFAULT_DATA_DIR))
        raw_dir = (data_dir or DEFAULT_DATA_DIR) / "raw"
    archive = raw.archive_fit_bundle(raw_dir, folder)
    decoded = decode_bundle(archive or folder)
    date = decoded.get("date")
    if not date:
        raise ValueError(
            f"Could not determine the bundle's date from {folder} "
            "(no timestamps decoded and no YYYY-MM-DD in the folder name)"
        )
    if archive is not None:
        raw.finalize_fit_archive(raw_dir, archive, date)

    rows = to_rows(decoded)
    report: dict = {"date": date, "imported": [], "skipped": [], "quality_flags": []}
    for table, row in rows.items():
        if row is None or all(v is None for k, v in row.items() if k not in ("date", "source")):
            continue
        existing = conn.execute(
            f"SELECT source FROM {table} WHERE date=%s", (date,)  # noqa: S608 - fixed table names
        ).fetchone()
        if existing and existing["source"] == "api" and not force:
            report["skipped"].append(f"{table} (api row exists; use force to overwrite)")
            continue
        upsert(conn, table, row, ("date",))
        report["imported"].append(table)

    sleep_row = rows.get("sleep") or {}
    if sleep_row.get("quality_flags"):
        report["quality_flags"] = json.loads(sleep_row["quality_flags"])
    return report
