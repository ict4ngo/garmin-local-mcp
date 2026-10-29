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
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from datetime import date as date_type
from pathlib import Path

from psycopg import Connection

from .db import upsert, utcnow

_FIT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    if Path(raw_dir).is_symlink():
        return
    daily = raw_dir / "daily"
    if daily.is_symlink() or not daily.is_dir():
        return
    for year_dir in sorted(daily.iterdir()):
        if year_dir.is_symlink() or not year_dir.is_dir() or len(year_dir.name) != 4:
            continue
        for day_dir in sorted(year_dir.iterdir()):
            if (
                day_dir.is_symlink()
                or not day_dir.is_dir()
                or _date_for_folder(day_dir) is None
                or day_dir.name[:4] != year_dir.name
            ):
                continue
            for path in sorted(day_dir.iterdir()):
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    continue
                yield day_dir.name, path.stem, path


def iter_activity_snapshots(raw_dir: Path) -> Iterator[Path]:
    """Yield regular activity JSON files without traversing symlinks."""
    if Path(raw_dir).is_symlink():
        return
    directory = raw_dir / "activities"
    if directory.is_symlink() or not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not path.is_symlink() and path.is_file() and path.suffix == ".json":
            yield path


def _safe_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"FIT archive path must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"FIT archive path is not a safe directory: {path}")


def _date_for_folder(folder: Path) -> str | None:
    candidate = folder.name
    if not _FIT_DATE.fullmatch(candidate):
        return None
    try:
        return date_type.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _managed_archive(raw_dir: Path, folder: Path) -> bool:
    try:
        relative = folder.absolute().relative_to((raw_dir / "fit").absolute())
    except ValueError:
        return False
    return len(relative.parts) == 2


def archive_fit_bundle(raw_dir: Path, source: Path) -> Path | None:
    """Copy regular FIT inputs to an immutable, managed archive before parsing."""
    raw_dir, source = Path(raw_dir), Path(source)
    if _managed_archive(raw_dir, source):
        fit_root = raw_dir / "fit"
        date_dir = source.parent
        if (
            raw_dir.is_symlink()
            or fit_root.is_symlink()
            or date_dir.is_symlink()
            or source.is_symlink()
            or not _date_for_folder(date_dir)
            or not source.is_dir()
        ):
            raise ValueError(f"FIT archive is not a regular directory: {source}")
        for child in source.iterdir():
            if child.suffix.lower() == ".fit" and (child.is_symlink() or not child.is_file()):
                raise ValueError(f"FIT archive contains an unsafe file: {child}")
        return source
    absolute_source = source.absolute()
    if any(part.is_symlink() for part in (absolute_source, *absolute_source.parents)):
        raise ValueError(f"FIT source path must not traverse symlinks: {source}")
    if not source.is_dir():
        raise ValueError(f"FIT source must be a regular directory: {source}")

    payloads: list[tuple[str, bytes]] = []
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        if Path(entry.name).suffix.lower() != ".fit":
            continue
        if not entry.is_file(follow_symlinks=False):
            raise ValueError(f"FIT source contains a non-regular file: {entry.path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(entry.path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"FIT source contains a non-regular file: {entry.path}")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
        finally:
            os.close(descriptor)
        payloads.append((entry.name, payload))
    if not payloads:
        return None

    digest = hashlib.sha256()
    for name, payload in payloads:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
    bundle_id = digest.hexdigest()
    date = _date_for_folder(source)
    fit_root = raw_dir / "fit"
    _safe_directory(raw_dir)
    _safe_directory(fit_root)
    parent = fit_root / (date or ".pending")
    _safe_directory(parent)
    destination = parent / bundle_id
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(f"FIT archive destination is unsafe: {destination}")
        return destination

    temporary = Path(tempfile.mkdtemp(prefix=".archive-", dir=parent))
    try:
        for name, payload in payloads:
            target = temporary / name
            with target.open("xb") as stream:
                stream.write(payload)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def finalize_fit_archive(raw_dir: Path, archive: Path, date: str) -> Path:
    """Place an initially undated archive at its decoded date's canonical path."""
    date = date_type.fromisoformat(date).isoformat()
    archive = Path(archive)
    if archive.parent.name == date:
        return archive
    fit_root = Path(raw_dir) / "fit"
    _safe_directory(fit_root)
    destination_parent = fit_root / date
    _safe_directory(destination_parent)
    destination = destination_parent / archive.name
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(f"FIT archive destination is unsafe: {destination}")
        shutil.rmtree(archive)
    else:
        os.replace(archive, destination)
    return destination


def iter_fit_archives(raw_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield managed, dated FIT bundles without traversing symlinked paths."""
    if Path(raw_dir).is_symlink():
        return
    fit_root = Path(raw_dir) / "fit"
    if fit_root.is_symlink() or not fit_root.is_dir():
        return
    for day_dir in sorted(fit_root.iterdir()):
        if day_dir.is_symlink() or not day_dir.is_dir() or not _date_for_folder(day_dir):
            continue
        for bundle in sorted(day_dir.iterdir()):
            if bundle.is_symlink() or not bundle.is_dir():
                continue
            files = list(bundle.iterdir())
            if any(
                not path.is_symlink() and path.is_file() and path.suffix.lower() == ".fit"
                for path in files
            ):
                yield day_dir.name, bundle
