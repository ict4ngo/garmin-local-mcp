"""Sync engine tests: offline, driven by a fake client (no garminconnect import)."""

import json
from pathlib import Path

import pytest

from garmin_mcp import db, raw, sync
from garmin_mcp.auth import AuthError
from garmin_mcp.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

DAY1 = "2026-01-15"
DAY2 = "2026-01-16"

DATA_TABLES = ("daily_wellness", "sleep", "hrv", "training_status", "activities")


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    """Returns fixture payloads on DAY1 and empty payloads on other days."""

    def __init__(self):
        self.calls = []

    def _payload(self, endpoint, date, empty):
        self.calls.append((endpoint, date))
        return load(endpoint) if date == DAY1 else empty

    def get_stats(self, date):
        return self._payload("usersummary", date, {})

    def get_sleep_data(self, date):
        return self._payload("sleep", date, {})

    def get_hrv_data(self, date):
        return self._payload("hrv", date, None)

    def get_training_status(self, date):
        return self._payload("training_status", date, None)

    def get_fitnessage_data(self, date):
        return self._payload("fitnessage", date, None)

    def get_activities_by_date(self, start, end):
        return self._payload("activities", start, [])

    def get_stress_data(self, date):
        return self._payload("stress_samples", date, {})

    def get_body_battery(self, start, end):
        return self._payload("body_battery_samples", start, [])

    def get_heart_rates(self, date):
        return self._payload("heart_rate_samples", date, {})

    def get_spo2_data(self, date):
        return self._payload("spo2_samples", date, {})

    def get_respiration_data(self, date):
        return self._payload("respiration_samples", date, {})

    def get_daily_movement(self, date):
        return self._payload("daily_movement_samples", date, {})

    def get_training_readiness(self, date):
        return self._payload("training_readiness_samples", date, [])

    def get_activity_details(self, activity_id):
        return load("activity_detail_samples")


class GarminConnectTooManyRequestsError(Exception):
    """Same class *name* as garminconnect's - sync detects it without the import."""


class RateLimitedClient(FakeClient):
    def get_stats(self, date):
        raise GarminConnectTooManyRequestsError("Too many requests (429)")


@pytest.fixture
def config(tmp_path, postgres_connection, test_database_url):
    cfg = Config(
        data_dir=tmp_path,
        database_url=test_database_url,
        timezone="America/New_York",
        sync_times=("09:00",),
        units="metric",
        request_delay_seconds=0.0,
        baseline_window_days=28,
        backfill_start_date=None,
        backfill_enabled=True,
        backfill_empty_streak_days=90,
    )
    cfg.ensure_dirs()
    return cfg


def quiet(_msg):
    pass


def run(config, conn, client, **kwargs):
    kwargs.setdefault("progress", quiet)
    return sync.sync_range(config, conn, client, DAY1, DAY2, **kwargs)


def dump_db(conn):
    """Everything reparse must reproduce, minus volatile timestamps/counters."""
    out = {}
    for table in DATA_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
        out[table] = sorted(
            tuple((k, r[k]) for k in r.keys() if k != "fetched_at") for r in rows
        )
    out["sync_state"] = sorted(
        tuple(r.values()) for r in conn.execute("SELECT endpoint, date, status FROM sync_state")
    )
    out["raw_snapshots"] = sorted(
        tuple(r.values())
        for r in conn.execute("SELECT path, date, endpoint, bytes, sha256 FROM raw_snapshots")
    )
    out["samples"] = sorted(
        tuple(r.values())
        for r in conn.execute(
            "SELECT source_endpoint, source_id, metric, observed_at, source_date, sample_index, "
            "value, unit, local_timestamp FROM samples"
        )
    )
    out["activity_detail_state"] = sorted(
        tuple(r.values())
        for r in conn.execute(
            "SELECT activity_id, sha256, raw_path FROM activity_detail_state"
        )
    )
    return out


def test_sync_range_rows_land(config):
    conn = db.connect(config.database_url)
    client = FakeClient()
    report = run(config, conn, client)

    wellness = conn.execute("SELECT * FROM daily_wellness WHERE date=%s", (DAY1,)).fetchone()
    assert wellness["resting_hr"] == 55
    assert wellness["skin_temp_dev_c"] == -0.3  # sleep's partial merged in, nothing clobbered
    assert wellness["source"] == "api"
    assert conn.execute("SELECT score FROM sleep WHERE date=%s", (DAY1,)).fetchone()["score"] == 82
    hrv = conn.execute("SELECT status FROM hrv WHERE date=%s", (DAY1,)).fetchone()
    assert hrv["status"] == "balanced"
    training = conn.execute("SELECT * FROM training_status WHERE date=%s", (DAY1,)).fetchone()
    assert training["status"] == "productive_1"  # from the training_status endpoint
    assert training["fitness_age"] == 41.23  # fitnessage's partial merged in, nothing clobbered
    assert training["achievable_fitness_age"] == 40.99
    activity = conn.execute("SELECT * FROM activities").fetchone()
    assert activity["activity_id"] == 90000000001
    assert activity["raw_path"] == "activities/90000000001.json"

    # snapshots on disk, including the per-activity one
    assert raw.daily_path(config.raw_dir, DAY1, "sleep").is_file()
    assert raw.daily_path(config.raw_dir, DAY2, "hrv").is_file()  # "null" is still history
    assert raw.activity_path(config.raw_dir, 90000000001).is_file()

    # sync_state: ok on the full day, empty where the API had nothing
    assert db.synced_ok(conn, "usersummary", DAY1)
    state = {
        (r["endpoint"], r["date"]): r["status"]
        for r in conn.execute("SELECT * FROM sync_state")
    }
    assert state[("sleep", DAY1)] == "ok"
    assert state[("hrv", DAY2)] == "empty"
    assert state[("activities", DAY2)] == "empty"

    assert report["aborted"] is None and not report["resumable"]
    assert report["requests"] == 26  # 13 date-scoped endpoints x 2 days
    assert report["endpoints"]["usersummary"] == {"ok": 1, "empty": 1, "skipped": 0, "error": 0}


def test_rerun_is_idempotent(config):
    conn = db.connect(config.database_url)
    client = FakeClient()
    run(config, conn, client)
    calls = len(client.calls)
    snapshot = raw.daily_path(config.raw_dir, DAY1, "sleep")
    content = snapshot.read_bytes()

    report = run(config, conn, client)
    assert len(client.calls) == calls  # no new fetches
    assert report["requests"] == 0
    assert all(s == {"ok": 0, "empty": 0, "skipped": 2, "error": 0}
               for s in report["endpoints"].values())
    assert snapshot.read_bytes() == content

    # even with sync_state cleared, existing snapshots are never overwritten
    with conn.transaction():
        conn.execute("DELETE FROM sync_state")
    run(config, conn, client)
    assert snapshot.read_bytes() == content


def test_rate_limit_backs_off_then_aborts(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    conn = db.connect(config.database_url)
    report = run(config, conn, RateLimitedClient())

    assert sleeps == [30, 60, 120]
    assert report["aborted"] == "rate_limited"
    assert report["resumable"] is True
    assert report["requests"] == 0
    row = conn.execute(
        "SELECT status, last_error FROM sync_state WHERE endpoint='usersummary' AND date=%s",
        (DAY1,),
    ).fetchone()
    assert row["status"] == "error" and "rate limited" in row["last_error"]
    # aborted before any other endpoint was attempted
    assert conn.execute("SELECT COUNT(*) AS count FROM sync_state").fetchone()["count"] == 1


def test_auth_error_aborts_with_hint(config):
    class AuthFailClient(FakeClient):
        def get_stats(self, date):
            raise AuthError("Stored tokens were rejected")

    conn = db.connect(config.database_url)
    report = run(config, conn, AuthFailClient())
    assert report["aborted"] == "auth"
    assert report["resumable"] is True
    assert "login" in report["auth_error"]["hint"]


def test_endpoint_error_continues(config):
    class FlakyClient(FakeClient):
        def get_sleep_data(self, date):
            raise RuntimeError("boom")

    conn = db.connect(config.database_url)
    report = run(config, conn, FlakyClient())
    assert report["aborted"] is None
    assert report["endpoints"]["sleep"] == {"ok": 0, "empty": 0, "skipped": 0, "error": 2}
    assert report["endpoints"]["hrv"]["ok"] == 1  # later endpoints still ran
    row = conn.execute(
        "SELECT last_error FROM sync_state WHERE endpoint='sleep' AND date=%s", (DAY1,)
    ).fetchone()
    assert "boom" in row["last_error"]


def test_partial_upsert_survives_either_order(config):
    conn = db.connect(config.database_url)
    client = FakeClient()
    run(config, conn, client, endpoints=["sleep"])
    row = conn.execute("SELECT * FROM daily_wellness WHERE date=%s", (DAY1,)).fetchone()
    assert row["skin_temp_dev_c"] == -0.3 and row["resting_hr"] is None

    run(config, conn, client, endpoints=["usersummary"])
    row = conn.execute("SELECT * FROM daily_wellness WHERE date=%s", (DAY1,)).fetchone()
    assert row["skin_temp_dev_c"] == -0.3  # untouched by the later usersummary upsert
    assert row["resting_hr"] == 55


def test_throttle_between_requests(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    delayed = Config(
        data_dir=config.data_dir,
        database_url=config.database_url,
        timezone=config.timezone,
        sync_times=config.sync_times,
        units=config.units,
        request_delay_seconds=1.5,
        baseline_window_days=28,
        backfill_start_date=config.backfill_start_date,
        backfill_enabled=config.backfill_enabled,
        backfill_empty_streak_days=config.backfill_empty_streak_days,
    )
    conn = db.connect(delayed.database_url)
    run(delayed, conn, FakeClient())
    assert len(sleeps) == 26  # includes the nested activity-detail request
    assert all(1.47 <= delay <= 1.5 for delay in sleeps)


def test_throttle_continues_across_sync_calls(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    delayed = Config(
        data_dir=config.data_dir,
        database_url=config.database_url,
        timezone=config.timezone,
        sync_times=config.sync_times,
        units=config.units,
        request_delay_seconds=1.5,
        baseline_window_days=config.baseline_window_days,
        backfill_start_date=config.backfill_start_date,
        backfill_enabled=config.backfill_enabled,
        backfill_empty_streak_days=config.backfill_empty_streak_days,
    )
    conn = db.connect(delayed.database_url)

    for day in (DAY1, DAY2):
        sync.sync_range(
            delayed, conn, FakeClient(), day, day, endpoints=["usersummary"],
            progress=quiet,
        )

    assert len(sleeps) == 1
    assert 1.47 <= sleeps[0] <= 1.5


def test_reparse_rebuilds_identical_db(config):
    conn = db.connect(config.database_url)
    run(config, conn, FakeClient())
    expected = dump_db(conn)
    conn.close()

    conn = db.connect(config.database_url)
    with conn.transaction():
        for table in (
            *DATA_TABLES, "sync_state", "raw_snapshots", "samples", "activity_detail_state"
        ):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed test table names
    report = sync.reparse(config, conn, progress=quiet)
    assert report["daily_snapshots"] == 26
    assert report["activity_snapshots"] == 1
    assert dump_db(conn) == expected


def test_yesterday_and_default_range(config):
    end = sync.yesterday(config)
    start, end2 = sync.default_range(config)
    assert end2 == end
    assert len(start) == 10 and start < end


def test_unknown_endpoint_rejected(config):
    conn = db.connect(config.database_url)
    with pytest.raises(ValueError, match="Unknown endpoints"):
        run(config, conn, FakeClient(), endpoints=["not_an_endpoint"])


def test_current_day_sample_refresh_is_idempotent(config, monkeypatch):
    monkeypatch.setattr(sync, "_today", lambda _config: DAY1, raising=False)

    class SampleClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.sample_calls = []

        def get_stress_data(self, date):
            self.sample_calls.append(("stress", date))
            payload = load("stress_samples")
            if len(self.sample_calls) > 2:
                payload["stressValuesArray"] = payload["stressValuesArray"][:1]
            return payload

        def get_body_battery(self, start, end):
            self.sample_calls.append(("body_battery", start, end))
            return load("body_battery_samples")

    client = SampleClient()
    conn = db.connect(config.database_url)
    for _ in range(2):
        sync.sync_range(
            config, conn, client, DAY1, DAY1, endpoints=["stress", "body_battery", "sleep"],
            progress=quiet
        )

    assert client.sample_calls == [
        ("stress", DAY1), ("body_battery", DAY1, DAY1),
        ("stress", DAY1), ("body_battery", DAY1, DAY1),
    ]
    assert sum(call[0] == "sleep" for call in client.calls) == 2
    assert conn.execute("SELECT COUNT(*) AS count FROM samples").fetchone()["count"] == 15
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM samples WHERE source_endpoint='stress'"
    ).fetchone()["count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint IN ('stress','body_battery')"
    ).fetchone()["count"] == 4


def test_reparse_uses_latest_snapshot_version(config):
    conn = db.connect(config.database_url)
    first = load("stress_samples")
    latest = {**first, "stressValuesArray": [[1768478400000, 51]]}
    raw.write_snapshot(
        conn, config.raw_dir, raw.daily_path(config.raw_dir, DAY1, "stress"), first, DAY1,
        "stress"
    )
    raw.write_versioned_snapshot(
        conn, config.raw_dir, latest, DAY1, "stress", "2026-01-16T00:00:00.000001Z"
    )

    sync.reparse(config, conn, progress=quiet)

    samples = conn.execute(
        "SELECT value FROM samples WHERE source_endpoint='stress' ORDER BY sample_index"
    ).fetchall()
    assert [row["value"] for row in samples] == [51.0]
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint='stress'"
    ).fetchone()["count"] == 2


def test_activity_detail_fetch_is_not_repeated(config):
    class DetailClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.detail_calls = []

        def get_activity_details(self, activity_id):
            self.detail_calls.append(activity_id)
            return load("activity_detail_samples")

    client = DetailClient()
    conn = db.connect(config.database_url)
    for _ in range(2):
        sync.sync_range(
            config, conn, client, DAY1, DAY1, endpoints=["activities"], force=True,
            progress=quiet
        )

    assert client.detail_calls == [90000000001]
    state = conn.execute("SELECT * FROM activity_detail_state").fetchall()
    assert len(state) == 1 and state[0]["activity_id"] == 90000000001
    assert conn.execute("SELECT COUNT(*) AS count FROM samples").fetchone()["count"] == 4
    assert len(list(raw.iter_activity_snapshots(config.raw_dir))) == 2
    assert len(list(raw.iter_activity_detail_snapshots(config.raw_dir))) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint='activities'"
    ).fetchone()["count"] == 4


def test_activity_detail_rate_limit_retries_without_duplicate_data(config, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sync, "_sleep", sleeps.append)
    monkeypatch.setattr(sync, "BACKOFF_SECONDS", (3, 6))

    class RateLimitedDetailClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.detail_calls = 0

        def get_activity_details(self, activity_id):
            self.detail_calls += 1
            if self.detail_calls == 1:
                raise GarminConnectTooManyRequestsError("429 rate limited")
            return load("activity_detail_samples")

    conn = db.connect(config.database_url)
    client = RateLimitedDetailClient()
    report = sync.sync_range(
        config, conn, client, DAY1, DAY1, endpoints=["activities"], progress=quiet
    )

    assert sleeps == [3]
    assert client.detail_calls == 2
    assert report["aborted"] is None
    assert report["endpoints"]["activities"]["ok"] == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM activities").fetchone()["count"] == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM samples").fetchone()["count"] == 4
    assert (
        conn.execute("SELECT COUNT(*) AS count FROM activity_detail_state").fetchone()["count"]
        == 1
    )
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint='activity_details'"
    ).fetchone()["count"] == 1
