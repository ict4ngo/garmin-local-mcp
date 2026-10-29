"""Sync engine: incremental, resumable, rate-limit-aware.

For each date x endpoint: skip if already synced, fetch, snapshot the raw
payload (immutable), parse, upsert, record sync_state. Interrupted or
rate-limited runs resume from sync_state on the next invocation. `reparse`
rebuilds the whole database from raw snapshots without any network access.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from zoneinfo import ZoneInfo

from psycopg import Connection

from . import db, raw
from . import endpoints as endpoint_module
from .auth import AuthError
from .config import Config
from .endpoints import (
    ENDPOINTS,
    SAMPLE_ENDPOINTS,
    TABLE_KEYS,
    Endpoint,
    SampleEndpoint,
    activity_date,
)
from .samples import replace_samples

# Backoff waits between retries after a rate-limit error; a rate limit hit
# after the last wait aborts the run (cleanly - sync_state makes it resumable).
BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600)

_sleep = time.sleep  # module-level so tests can stub it out

Progress = Callable[[str], None]


class RateLimitAbort(Exception):
    """Raised internally when backoff is exhausted; never escapes sync_range."""


def yesterday(config: Config) -> str:
    """Yesterday's date in the configured timezone (fallback: system timezone)."""
    tz = ZoneInfo(config.timezone) if config.timezone else None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return (now.date() - timedelta(days=1)).isoformat()


def _today(config: Config) -> str:
    tz = ZoneInfo(config.timezone) if config.timezone else None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return now.date().isoformat()


def default_range(config: Config, days: int = 7) -> tuple[str, str]:
    """Last `days` days ending yesterday."""
    end = yesterday(config)
    start = (date_type.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    return start, end


def _dates(start: str, end: str) -> list[str]:
    d0, d1 = date_type.fromisoformat(start), date_type.fromisoformat(end)
    if d0 > d1:
        raise ValueError(f"start {start} is after end {end}")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _exception_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _is_rate_limit(exc: BaseException) -> bool:
    if "GarminConnectTooManyRequestsError" in _exception_names(exc):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc)


def _is_auth(exc: BaseException) -> bool:
    return "GarminConnectAuthenticationError" in _exception_names(exc)


def _fetch_with_backoff(endpoint: Endpoint, client, day: str, progress: Progress):
    """Fetch one payload, backing off on rate limits; AuthError on auth failure."""
    for wait in (*BACKOFF_SECONDS, None):
        try:
            return endpoint.fetch(client, day)
        except AuthError:
            raise
        except Exception as e:
            if _is_auth(e):
                raise AuthError(f"Garmin rejected the session mid-sync: {e}") from e
            if not _is_rate_limit(e):
                raise
            if wait is None:
                raise RateLimitAbort(str(e)) from e
            progress(f"rate limited; backing off {wait}s")
            _sleep(wait)


def _store(
    config: Config,
    conn: Connection,
    endpoint: Endpoint | SampleEndpoint,
    day: str,
    payload,
    client,
) -> int:
    """Snapshot the payload, parse it, upsert rows. Returns the row count."""
    fetched_at = datetime.now(UTC)
    fetched_at_text = fetched_at.isoformat().replace("+00:00", "Z")
    if endpoint.name in SAMPLE_ENDPOINTS:
        raw.write_versioned_snapshot(
            conn, config.raw_dir, payload, day, endpoint.name, fetched_at_text
        )
        parsed_samples = endpoint.parse(payload, day)
        return replace_samples(conn, endpoint.name, day, parsed_samples, fetched_at)

    snap_path = raw.daily_path(config.raw_dir, day, endpoint.name)
    if not snap_path.exists():
        raw.write_snapshot(conn, config.raw_dir, snap_path, payload, day, endpoint.name)
    else:
        raw.index_snapshot(conn, config.raw_dir, snap_path, day, endpoint.name)
        raw.write_versioned_snapshot(
            conn, config.raw_dir, payload, day, endpoint.name, fetched_at_text
        )

    if endpoint.name == "activities" and isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or item.get("activityId") is None:
                continue
            activity_id = int(item["activityId"])
            activity_day = activity_date(item, day)
            act_path = raw.activity_path(config.raw_dir, activity_id)
            if not act_path.exists():
                raw.write_snapshot(conn, config.raw_dir, act_path, item, activity_day, "activities")
            else:
                raw.index_snapshot(
                    conn, config.raw_dir, act_path, activity_day, "activities"
                )
                raw.write_versioned_snapshot(
                    conn, config.raw_dir, item, activity_day, "activities", fetched_at_text,
                    activity_id=activity_id
                )

    rows = endpoint.parse(payload, day)
    for table, row in rows:
        db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at_text}, TABLE_KEYS[table])
    row_count = len(rows)
    if endpoint.name == "sleep":
        parsed_samples = endpoint_module.parse_sleep_samples(payload, day)
        row_count += replace_samples(conn, "sleep", day, parsed_samples, fetched_at)
    elif endpoint.name == "activities" and isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or item.get("activityId") is None:
                continue
            activity_id = int(item["activityId"])
            known = conn.execute(
                "SELECT 1 FROM activity_detail_state WHERE activity_id=%s", (activity_id,)
            ).fetchone()
            if known:
                continue
            details = client.get_activity_details(activity_id)
            activity_day = activity_date(item, day) or day
            detail_path = raw.write_versioned_snapshot(
                conn, config.raw_dir, details, activity_day, "activity_details", fetched_at_text,
                activity_id=activity_id, details=True
            )
            detail_bytes = detail_path.read_bytes()
            detail_hash = hashlib.sha256(detail_bytes).hexdigest()
            detail_samples = endpoint_module.parse_activity_detail_samples(
                activity_id, details, activity_day
            )
            row_count += replace_samples(
                conn, "activity_details", str(activity_id), detail_samples, fetched_at
            )
            with conn.transaction():
                conn.execute(
                    """INSERT INTO activity_detail_state (activity_id, sha256, raw_path, fetched_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT(activity_id) DO UPDATE SET sha256=excluded.sha256,
                         raw_path=excluded.raw_path, fetched_at=excluded.fetched_at""",
                    (
                        activity_id,
                        detail_hash,
                        str(detail_path.relative_to(config.raw_dir)).replace("\\", "/"),
                        fetched_at,
                    ),
                )
    return row_count


def sync_range(
    config: Config,
    conn: Connection,
    client,
    start: str,
    end: str,
    endpoints: list[str] | None = None,
    force: bool = False,
    progress: Progress = print,
) -> dict:
    """Sync [start, end] (inclusive, oldest first). Returns a report dict.

    Rate-limit exhaustion and auth failures abort cleanly with resumable=True;
    any other per-(date, endpoint) error is recorded in sync_state and skipped.
    """
    names = list(endpoints) if endpoints else [*ENDPOINTS, *SAMPLE_ENDPOINTS]
    known_endpoints = {**ENDPOINTS, **SAMPLE_ENDPOINTS}
    unknown = [n for n in names if n not in known_endpoints]
    if unknown:
        raise ValueError(f"Unknown endpoints: {unknown}. Known: {list(known_endpoints)}")

    report: dict = {
        "start": start,
        "end": end,
        "requests": 0,
        "rows": 0,
        "aborted": None,
        "resumable": False,
        "endpoints": {n: {"ok": 0, "empty": 0, "skipped": 0, "error": 0} for n in names},
    }
    made_request = False
    for day in _dates(start, end):
        for name in names:
            endpoint = known_endpoints[name]
            stats = report["endpoints"][name]
            refresh_today = day == _today(config) and (
                name in SAMPLE_ENDPOINTS or name == "sleep"
            )
            if not force and not refresh_today and db.synced_ok(conn, name, day):
                stats["skipped"] += 1
                continue
            if made_request and config.request_delay_seconds > 0:
                _sleep(config.request_delay_seconds)
            made_request = True
            try:
                payload = _fetch_with_backoff(endpoint, client, day, progress)
                report["requests"] += 1
            except RateLimitAbort as e:
                db.mark_sync(conn, name, day, "error", f"rate limited: {e}")
                stats["error"] += 1
                report["aborted"] = "rate_limited"
                report["resumable"] = True
                progress(f"{day} {name}: rate limited - aborting; re-run to resume")
                return report
            except AuthError as e:
                db.mark_sync(conn, name, day, "error", str(e))
                stats["error"] += 1
                report["aborted"] = "auth"
                report["resumable"] = True
                report["auth_error"] = e.to_dict()
                progress(f"{day} {name}: auth error - aborting ({e.hint})")
                return report
            except Exception as e:
                db.mark_sync(conn, name, day, "error", str(e))
                stats["error"] += 1
                progress(f"{day} {name}: error - {e}")
                continue
            try:
                n_rows = _store(config, conn, endpoint, day, payload, client)
            except Exception as e:
                db.mark_sync(conn, name, day, "error", f"parse/store: {e}")
                stats["error"] += 1
                progress(f"{day} {name}: parse/store error - {e}")
                continue
            status = "ok" if n_rows else "empty"
            db.mark_sync(conn, name, day, status)
            stats[status] += 1
            report["rows"] += n_rows
            progress(f"{day} {name}: {status}" + (f" ({n_rows} rows)" if n_rows else ""))
    return report


def reparse(config: Config, conn: Connection, progress: Progress = print) -> dict:
    """Rebuild database tables from raw snapshots on disk. Fully offline.

    Re-runs every parse function, re-registers snapshots in raw_snapshots, and
    restores sync_state coverage so the next `sync` does not re-fetch.
    """
    report = {
        "daily_snapshots": 0,
        "activity_snapshots": 0,
        "activity_detail_snapshots": 0,
        "fit_bundles": 0,
        "rows": 0,
        "unknown_endpoints": 0,
    }
    all_daily = list(raw.iter_daily_snapshots(config.raw_dir))
    for day, name, path in all_daily:
        raw.index_snapshot(conn, config.raw_dir, path, day, name)
    for day, name, path in raw.latest_daily_snapshots(config.raw_dir):
        payload = raw.read_snapshot(path)
        sample_endpoint = SAMPLE_ENDPOINTS.get(name)
        if sample_endpoint is not None:
            parsed_samples = sample_endpoint.parse(payload, day)
            report["rows"] += replace_samples(
                conn, name, day, parsed_samples, datetime.now(UTC)
            )
            db.mark_sync(conn, name, day, "ok" if parsed_samples else "empty")
            report["daily_snapshots"] += 1
            continue
        endpoint = ENDPOINTS.get(name)
        if endpoint is None:
            report["unknown_endpoints"] += 1
            continue
        rows = endpoint.parse(payload, day)
        fetched_at = db.utcnow()
        for table, row in rows:
            db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at}, TABLE_KEYS[table])
        if name == "sleep":
            parsed_samples = endpoint_module.parse_sleep_samples(payload, day)
            report["rows"] += replace_samples(
                conn, "sleep", day, parsed_samples, datetime.now(UTC)
            )
        db.mark_sync(conn, name, day, "ok" if rows else "empty")
        report["daily_snapshots"] += 1
        report["rows"] += len(rows)

    from .importer import import_bundle

    for _, path in raw.iter_fit_archives(config.raw_dir):
        imported = import_bundle(conn, path, raw_dir=config.raw_dir)
        report["fit_bundles"] += 1
        report["rows"] += len(imported["imported"])

    parse = ENDPOINTS["activities"].parse
    all_activities = list(raw.iter_activity_snapshots(config.raw_dir))
    for path in all_activities:
        item = raw.read_snapshot(path)
        if isinstance(item, dict):
            raw.index_snapshot(conn, config.raw_dir, path, activity_date(item), "activities")
    for path in raw.latest_activity_snapshots(config.raw_dir):
        item = raw.read_snapshot(path)
        if not isinstance(item, dict):
            continue
        day = activity_date(item)
        rows = parse([item], day or "")
        fetched_at = db.utcnow()
        for table, row in rows:
            db.upsert_partial(conn, table, {**row, "fetched_at": fetched_at}, TABLE_KEYS[table])
        report["activity_snapshots"] += 1
        report["rows"] += len(rows)

    activity_details: dict[int, object] = {}
    for activity_id, path in raw.iter_activity_detail_snapshots(config.raw_dir):
        activity = conn.execute(
            "SELECT date FROM activities WHERE activity_id=%s", (activity_id,)
        ).fetchone()
        day = activity["date"] if activity else None
        raw.index_snapshot(conn, config.raw_dir, path, day, "activity_details")
        if activity_id not in activity_details or path.name > activity_details[activity_id].name:
            activity_details[activity_id] = path
    for activity_id, path in sorted(activity_details.items()):
        details = raw.read_snapshot(path)
        activity = conn.execute(
            "SELECT date FROM activities WHERE activity_id=%s", (activity_id,)
        ).fetchone()
        day = activity["date"] if activity and activity["date"] else ""
        parsed_samples = endpoint_module.parse_activity_detail_samples(activity_id, details, day)
        fetched_at = datetime.now(UTC)
        report["rows"] += replace_samples(
            conn, "activity_details", str(activity_id), parsed_samples, fetched_at
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with conn.transaction():
            conn.execute(
                """INSERT INTO activity_detail_state (activity_id, sha256, raw_path, fetched_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT(activity_id) DO UPDATE SET sha256=excluded.sha256,
                     raw_path=excluded.raw_path, fetched_at=excluded.fetched_at""",
                (
                    activity_id,
                    digest,
                    str(path.relative_to(config.raw_dir)).replace("\\", "/"),
                    fetched_at,
                ),
            )
        report["activity_detail_snapshots"] += 1

    progress(
        f"reparse: {report['daily_snapshots']} daily + {report['activity_snapshots']} activity "
        f"+ {report['activity_detail_snapshots']} activity-detail "
        f"+ {report['fit_bundles']} FIT bundles "
        f"snapshots -> {report['rows']} rows"
    )
    return report
