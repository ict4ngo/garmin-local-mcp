"""Immutable raw JSON snapshot layer.

Layout:
    <data_dir>/raw/daily/YYYY/YYYY-MM-DD/<endpoint>.json
    <data_dir>/raw/activities/<activity_id>.json

Snapshots are the source of truth and are never overwritten (existing files are
skipped), so history survives API breakage and parser bugs alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

from .db import Connection, upsert, utcnow


def daily_path(raw_dir: Path, date: str, endpoint: str) -> Path:
    return raw_dir / "daily" / date[:4] / date / f"{endpoint}.json"


def activity_path(raw_dir: Path, activity_id: int) -> Path:
    return raw_dir / "activities" / f"{activity_id}.json"


def write_snapshot(
    conn: Connection,
    raw_dir: Path,
    path: Path,
    payload: object,
    date: str | None,
    endpoint: str,
) -> bool:
    """Write one snapshot and index it. Returns False (skip) if it already exists."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    index_snapshot(conn, raw_dir, path, date, endpoint)
    return True


def index_snapshot(
    conn: Connection,
    raw_dir: Path,
    path: Path,
    date: str | None,
    endpoint: str,
) -> None:
    """(Re-)register an existing snapshot file in raw_snapshots (used by reparse)."""
    data = path.read_bytes()
    upsert(
        conn,
        "raw_snapshots",
        {
            "path": str(path.relative_to(raw_dir)).replace("\\", "/"),
            "date": date,
            "endpoint": endpoint,
            "fetched_at": utcnow(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        ("path",),
    )


def read_snapshot(path: Path) -> object:
    with path.open("rb") as f:
        return json.load(f)


def iter_daily_snapshots(raw_dir: Path) -> Iterator[tuple[str, str, Path]]:
    """Yield (date, endpoint, path) for every daily snapshot on disk, sorted by date."""
    daily = raw_dir / "daily"
    if not daily.is_dir():
        return
    for day_dir in sorted(daily.glob("*/????-??-??")):
        for f in sorted(day_dir.glob("*.json")):
            yield day_dir.name, f.stem, f


def prune_snapshots(
    conn: Connection, raw_dir: Path, cutoff: str, dry_run: bool = False
) -> dict:
    """Delete raw snapshots (files + index rows) older than `cutoff` (YYYY-MM-DD).

    Snapshots without a known date are kept. The warehouse tables are
    untouched: pruned history stays queryable, it can only no longer be
    rebuilt by `reparse`.
    """
    rows = conn.execute(
        "SELECT path FROM raw_snapshots WHERE date IS NOT NULL AND date < %s "
        "ORDER BY path",
        (cutoff,),
    ).fetchall()
    removed_files = removed_rows = already_gone = 0
    touched_dirs: set[Path] = set()
    for row in rows:
        path = raw_dir / row["path"]
        if path.is_file():
            if not dry_run:
                path.unlink()
            removed_files += 1
            touched_dirs.add(path.parent)
        else:
            already_gone += 1
        if not dry_run:
            conn.execute("DELETE FROM raw_snapshots WHERE path=%s", (row["path"],))
        removed_rows += 1
    if not dry_run:
        # Tidy up directories left empty by the prune (date dirs, then year dirs).
        for d in sorted(touched_dirs, reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                parent = d.parent
                if parent != raw_dir and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
    return {
        "cutoff": cutoff,
        "dry_run": dry_run,
        "removed_files": removed_files,
        "removed_rows": removed_rows,
        "already_gone": already_gone,
    }
