from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from garmin_mcp import db, sync, worker
from garmin_mcp.cli import build_parser
from garmin_mcp.config import load


class EmptyClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fetch(*args):
            self.calls.append((name, args))
            if name == "get_activities_by_date":
                return []
            if name == "get_body_battery":
                return []
            if name == "get_training_readiness":
                return []
            return {}

        return fetch


class TooManyRequestsError(Exception):
    pass


class RateLimitedClient(EmptyClient):
    def get_stats(self, date):
        self.calls.append(("get_stats", (date,)))
        raise TooManyRequestsError("429 rate limited")


@pytest.fixture
def config(tmp_path, monkeypatch, postgres_connection, test_database_url):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", test_database_url)
    monkeypatch.setenv("GARMIN_MCP_SYNC_TIMES", "09:00,18:30")
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "America/New_York")
    monkeypatch.setenv("GARMIN_MCP_REQUEST_DELAY_SECONDS", "0")
    cfg = load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def stop_after_iteration(monkeypatch):
    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "_sleep", stop)


def test_worker_runs_at_configured_times(config, monkeypatch):
    now = datetime(2026, 6, 3, 9, 0, tzinfo=ZoneInfo(config.timezone))
    client = EmptyClient()
    stop_after_iteration(monkeypatch)

    assert worker.run_worker(config, client_factory=lambda _tokens: client, clock=lambda: now) == 0

    assert client.calls
    assert client.calls[0][1][0] == "2026-06-02"
    conn = db.connect(config.database_url)
    assert db.get_worker_state(conn, "last_scheduled_run") == now.isoformat()
    conn.close()


def test_missed_schedule_occurrences_coalesce_after_restart(config):
    timezone = ZoneInfo(config.timezone)
    now = datetime(2026, 6, 3, 19, 0, tzinfo=timezone)
    previous_run = datetime(2026, 6, 2, 20, 0, tzinfo=timezone).isoformat()

    assert worker._schedule_is_due(now, config.sync_times, previous_run)
    assert not worker._schedule_is_due(now, config.sync_times, now.isoformat())


def test_restart_before_first_configured_time_catches_up_once(config):
    timezone = ZoneInfo(config.timezone)
    now = datetime(2026, 6, 3, 7, 0, tzinfo=timezone)
    previous_run = datetime(2026, 6, 2, 9, 0, tzinfo=timezone).isoformat()

    assert worker._schedule_is_due(now, config.sync_times, previous_run)
    assert not worker._schedule_is_due(now, config.sync_times, now.isoformat())


def test_worker_requires_configured_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", "postgresql://garmin:garmin@localhost/garmin_test")
    monkeypatch.delenv("GARMIN_MCP_SYNC_TIMES", raising=False)
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "America/New_York")

    with pytest.raises(ValueError, match="GARMIN_MCP_SYNC_TIMES"):
        load(tmp_path)


def test_worker_cli_command_is_available():
    args = build_parser().parse_args(["worker"])
    assert args.func.__name__ == "cmd_worker"


def test_backfill_resumes_after_restart(config, postgres_connection):
    client = EmptyClient()
    conn = db.connect(config.database_url)
    assert worker.run_backfill_step(
        config, conn, client, endpoint="usersummary", today="2026-06-03"
    )
    conn.close()

    restarted_conn = db.connect(config.database_url)
    worker.run_backfill_step(
        config, restarted_conn, client, endpoint="usersummary", today="2026-06-03"
    )
    state = restarted_conn.execute(
        "SELECT cursor_date, empty_streak FROM backfill_state WHERE endpoint='usersummary'"
    ).fetchone()
    assert state["cursor_date"] == "2026-05-31"
    assert state["empty_streak"] == 2
    assert [args[0] for name, args in client.calls if name == "get_stats"] == [
        "2026-06-02", "2026-06-01"
    ]
    restarted_conn.close()


def test_backfill_rate_limit_preserves_cursor(config, postgres_connection, monkeypatch):
    monkeypatch.setattr(sync, "_sleep", lambda _seconds: None)
    conn = db.connect(config.database_url)
    client = RateLimitedClient()

    assert not worker.run_backfill_step(
        config, conn, client, endpoint="usersummary", today="2026-06-03"
    )
    state = conn.execute(
        "SELECT cursor_date, empty_streak, status FROM backfill_state "
        "WHERE endpoint='usersummary'"
    ).fetchone()
    assert state == {"cursor_date": "2026-06-02", "empty_streak": 0, "status": "error"}
    conn.close()


def test_failed_backfill_endpoint_does_not_block_other_cursors(
    config, postgres_connection, monkeypatch
):
    monkeypatch.setattr(sync, "_sleep", lambda _seconds: None)
    conn = db.connect(config.database_url)
    worker._initialize_backfill(conn, "2026-06-03")
    conn.execute(
        "UPDATE backfill_state SET cursor_date='2026-06-01' WHERE endpoint != 'usersummary'"
    )
    client = RateLimitedClient()

    worker.run_backfill_step(
        config, conn, client, endpoint="usersummary", today="2026-06-03"
    )
    worker.run_backfill_step(config, conn, client, today="2026-06-03")

    assert sum(name == "get_stats" for name, _ in client.calls) == 4
    assert len(client.calls) > 4  # a different endpoint progressed after the 429
    conn.close()


def test_backfill_stops_after_configured_empty_streak(config, postgres_connection):
    limited = replace(config, backfill_empty_streak_days=2)
    conn = db.connect(config.database_url)
    client = EmptyClient()

    worker.run_backfill_step(limited, conn, client, endpoint="usersummary", today="2026-06-03")
    worker.run_backfill_step(limited, conn, client, endpoint="usersummary", today="2026-06-03")

    state = conn.execute(
        "SELECT cursor_date, empty_streak, status FROM backfill_state "
        "WHERE endpoint='usersummary'"
    ).fetchone()
    assert state == {"cursor_date": None, "empty_streak": 2, "status": "complete"}
    assert not worker.run_backfill_step(
        limited, conn, client, endpoint="usersummary", today="2026-06-03"
    )
    conn.close()


def test_backfill_includes_explicit_lower_bound(config, postgres_connection):
    bounded = replace(config, backfill_start_date="2026-06-01")
    conn = db.connect(config.database_url)
    client = EmptyClient()

    worker.run_backfill_step(bounded, conn, client, endpoint="usersummary", today="2026-06-03")
    worker.run_backfill_step(bounded, conn, client, endpoint="usersummary", today="2026-06-03")

    state = conn.execute(
        "SELECT cursor_date, last_date, status FROM backfill_state "
        "WHERE endpoint='usersummary'"
    ).fetchone()
    assert state == {"cursor_date": None, "last_date": "2026-06-01", "status": "complete"}
    conn.close()


def test_disabled_backfill_does_not_initialize_cursor(config, postgres_connection):
    disabled = replace(config, backfill_enabled=False)
    conn = db.connect(config.database_url)

    assert not worker.run_backfill_step(
        disabled, conn, EmptyClient(), today="2026-06-03"
    )
    assert conn.execute("SELECT COUNT(*) AS count FROM backfill_state").fetchone()["count"] == 0
    conn.close()


def test_due_recent_sync_preempts_backfill(config, postgres_connection, monkeypatch):
    now = datetime(2026, 6, 3, 9, 0, tzinfo=ZoneInfo(config.timezone))
    client = EmptyClient()
    stop_after_iteration(monkeypatch)

    worker.run_worker(config, client_factory=lambda _tokens: client, clock=lambda: now)

    assert client.calls[0][1][0] == "2026-06-02"
    assert not any(args and args[0] == "2026-06-01" for _, args in client.calls)


def test_scheduled_pass_refreshes_samples_for_today_and_yesterday(config, postgres_connection):
    conn = db.connect(config.database_url)
    client = EmptyClient()

    worker._run_scheduled_sync(config, conn, client, "2026-06-03")
    worker._run_scheduled_sync(config, conn, client, "2026-06-03")

    assert sum(name == "get_stats" for name, _ in client.calls) == 2
    assert sum(name == "get_stress_data" for name, _ in client.calls) == 4
    assert sum(name == "get_heart_rates" for name, _ in client.calls) == 4
    conn.close()


def test_sync_advisory_lock_serializes_callers(config, postgres_connection, monkeypatch):
    first_entered = threading.Event()
    allow_first = threading.Event()
    second_entered = threading.Event()

    class BlockingClient(EmptyClient):
        def get_stats(self, date):
            first_entered.set()
            assert allow_first.wait(timeout=5)
            return {}

    class SecondClient(EmptyClient):
        def get_stats(self, date):
            second_entered.set()
            return {}

    monkeypatch.setattr(sync, "_sleep", lambda _seconds: None)
    first_conn = db.connect(config.database_url)
    second_conn = db.connect(config.database_url)
    failures = []

    def invoke(conn, client):
        try:
            sync.sync_range(
                config, conn, client, "2026-06-01", "2026-06-01",
                endpoints=["usersummary"], force=True, progress=lambda _message: None,
            )
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=invoke, args=(first_conn, BlockingClient()))
    second = threading.Thread(target=invoke, args=(second_conn, SecondClient()))
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    assert not second_entered.wait(timeout=0.2)
    allow_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert second_entered.is_set()
    first_conn.close()
    second_conn.close()
