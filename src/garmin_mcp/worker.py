"""Long-running timezone-aware scheduler and resumable history collector."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as time_type
from zoneinfo import ZoneInfo

from psycopg import Connection

from . import auth, db, sync
from .config import Config
from .endpoints import ENDPOINTS, SAMPLE_ENDPOINTS

_sleep = time.sleep
_BACKFILL_ENDPOINTS = (*ENDPOINTS, *SAMPLE_ENDPOINTS)
Clock = Callable[[], datetime]


def _coerce_local(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _scheduled_occurrence(now: datetime, times: tuple[str, ...]) -> datetime | None:
    occurrences = [
        datetime.combine(now.date(), time_type.fromisoformat(value), tzinfo=now.tzinfo)
        for value in times
    ]
    due = [value for value in occurrences if value <= now]
    if due:
        return max(due)
    previous_day = now.date() - timedelta(days=1)
    return max(
        datetime.combine(previous_day, time_type.fromisoformat(value), tzinfo=now.tzinfo)
        for value in times
    )


def _schedule_is_due(now: datetime, times: tuple[str, ...], last_run: str | None) -> bool:
    occurrence = _scheduled_occurrence(now, times)
    if occurrence is None:
        return False
    if last_run is None:
        return occurrence.date() == now.date()
    previous = datetime.fromisoformat(last_run)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=now.tzinfo)
    return previous.astimezone(now.tzinfo) < occurrence


def _run_scheduled_sync(config: Config, conn: Connection, client, today: str) -> dict:
    yesterday = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
    return sync.sync_range(
        config,
        conn,
        client,
        yesterday,
        today,
        force_endpoints=set(SAMPLE_ENDPOINTS),
        progress=lambda message: print(message, file=sys.stderr),
    )


def _initialize_backfill(conn: Connection, today: str) -> None:
    newest = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
    with conn.transaction():
        for endpoint in _BACKFILL_ENDPOINTS:
            conn.execute(
                """INSERT INTO backfill_state
                   (endpoint, cursor_date, empty_streak, status, updated_at)
                   VALUES (%s, %s, 0, 'pending', %s)
                   ON CONFLICT(endpoint) DO NOTHING""",
                (endpoint, newest, db.utcnow_precise()),
            )


def _run_backfill_step(
    config: Config,
    conn: Connection,
    client,
    *,
    endpoint: str | None = None,
    today: str,
) -> bool:
    """Fetch one endpoint/date pair and durably advance its reverse cursor.

    Returns False when no work is available or the request failed. Failed work
    retains its date cursor and is eligible for a later retry.
    """
    if not config.backfill_enabled:
        return False
    _initialize_backfill(conn, today)
    if endpoint is None:
        row = conn.execute(
            """SELECT endpoint, cursor_date, empty_streak FROM backfill_state
               WHERE status != 'complete' AND cursor_date IS NOT NULL
               ORDER BY updated_at ASC, cursor_date DESC, endpoint ASC LIMIT 1"""
        ).fetchone()
    else:
        if endpoint not in _BACKFILL_ENDPOINTS:
            raise ValueError(f"Unknown backfill endpoint: {endpoint}")
        row = conn.execute(
            "SELECT endpoint, cursor_date, empty_streak, status FROM backfill_state "
            "WHERE endpoint=%s",
            (endpoint,),
        ).fetchone()
    if row is None or row["cursor_date"] is None or row.get("status") == "complete":
        return False

    name = row["endpoint"]
    day = row["cursor_date"]
    cutoff = config.backfill_start_date
    if cutoff is not None and day < cutoff:
        with conn.transaction():
            conn.execute(
                "UPDATE backfill_state SET cursor_date=NULL, status='complete', updated_at=%s "
                "WHERE endpoint=%s",
                (db.utcnow_precise(), name),
            )
        return True

    with conn.transaction():
        conn.execute(
            "UPDATE backfill_state SET status='running', last_error=NULL, updated_at=%s "
            "WHERE endpoint=%s",
            (db.utcnow_precise(), name),
        )
    result = sync.sync_range(
        config, conn, client, day, day, endpoints=[name], progress=lambda _message: None
    )
    state = conn.execute(
        "SELECT status, last_error FROM sync_state WHERE endpoint=%s AND date=%s",
        (name, day),
    ).fetchone()
    if result["aborted"] or state is None or state["status"] == "error":
        reason = result["aborted"] or "endpoint sync error"
        with conn.transaction():
            conn.execute(
                "UPDATE backfill_state SET status='error', last_error=%s, updated_at=%s "
                "WHERE endpoint=%s",
                (reason, db.utcnow_precise(), name),
            )
        return False

    empty_streak = row["empty_streak"] + 1 if state["status"] == "empty" else 0
    previous = datetime.fromisoformat(day).date()
    next_day = (previous - timedelta(days=1)).isoformat()
    reached_cutoff = cutoff is not None and day <= cutoff
    reached_empty_limit = (
        cutoff is None and empty_streak >= config.backfill_empty_streak_days
    )
    complete = reached_cutoff or reached_empty_limit
    with conn.transaction():
        conn.execute(
            """UPDATE backfill_state SET cursor_date=%s, last_date=%s, empty_streak=%s,
               status=%s, last_error=NULL, updated_at=%s WHERE endpoint=%s""",
            (
                None if complete else next_day,
                day,
                empty_streak,
                "complete" if complete else "pending",
                db.utcnow_precise(),
                name,
            ),
        )
    return True


def run_backfill_step(
    config: Config,
    conn: Connection,
    client,
    *,
    endpoint: str | None = None,
    today: str,
) -> bool:
    """Run one persistent backfill step under the shared sync lock."""
    with db.sync_advisory_lock(conn):
        return _run_backfill_step(config, conn, client, endpoint=endpoint, today=today)


def run_worker(
    config: Config,
    client_factory=auth.get_client,
    clock: Clock = lambda: datetime.now().astimezone(),
) -> int:
    """Run scheduled/recent syncs and backfill until interrupted."""
    config.ensure_dirs()
    try:
        client = client_factory(config.tokens_dir)
    except auth.AuthError as exc:
        print(f"{exc} ({exc.hint})", file=sys.stderr)
        return 1

    conn = db.connect(config.database_url)
    timezone = ZoneInfo(config.timezone)
    try:
        while True:
            with db.sync_advisory_lock(conn):
                now = _coerce_local(clock(), timezone)
                today = now.date().isoformat()
                last_run = db.get_worker_state(conn, "last_scheduled_run")
                if _schedule_is_due(now, config.sync_times, last_run):
                    try:
                        _run_scheduled_sync(config, conn, client, today)
                        db.set_worker_state(conn, "last_scheduled_run", now.isoformat())
                    except Exception as exc:
                        print(f"scheduled sync failed: {exc}", file=sys.stderr)
                elif config.backfill_enabled:
                    try:
                        _run_backfill_step(config, conn, client, today=today)
                    except Exception as exc:
                        print(f"backfill step failed: {exc}", file=sys.stderr)
            _sleep(max(1.0, config.request_delay_seconds))
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
