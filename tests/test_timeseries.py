from __future__ import annotations

from datetime import UTC, datetime

from garmin_mcp import timeseries
from garmin_mcp.samples import Sample, upsert_samples


def _sample(metric: str, timestamp: str, value: float, index: int = 0) -> Sample:
    observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return Sample(
        source_endpoint="test",
        source_id=timestamp,
        metric=metric,
        observed_at=observed_at,
        source_date=observed_at.date().isoformat(),
        sample_index=index,
        value=value,
        unit="bpm",
        local_timestamp=None,
    )


def _seed(conn, *samples: Sample) -> None:
    upsert_samples(conn, samples, datetime(2026, 10, 1, tzinfo=UTC))


def test_query_samples_uses_half_open_range(postgres_connection):
    _seed(
        postgres_connection,
        _sample("heart_rate", "2026-06-01T09:59:59Z", 59),
        _sample("heart_rate", "2026-06-01T10:00:00Z", 60),
        _sample("heart_rate", "2026-06-01T10:59:59Z", 61),
        _sample("heart_rate", "2026-06-01T11:00:00Z", 62),
    )

    result = timeseries.query_samples(
        postgres_connection,
        ["heart_rate"],
        datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
        datetime.fromisoformat("2026-06-01T11:00:00+00:00"),
        "UTC",
    )

    assert [row[2] for row in result["rows"]] == [60, 61]
    assert result["total"] == 2 and result["truncated"] is False
    assert result["rows"][0][0] == "2026-06-01T10:00:00+00:00"


def test_query_samples_local_time_handles_dst(postgres_connection):
    # On the spring transition, 01:30 EST and 03:30 EDT are one hour apart.
    _seed(
        postgres_connection,
        _sample("heart_rate", "2026-03-08T06:30:00Z", 70),
        _sample("heart_rate", "2026-03-08T07:30:00Z", 80),
    )

    result = timeseries.query_samples(
        postgres_connection,
        ["heart_rate"],
        datetime.fromisoformat("2026-03-08T01:00:00"),
        datetime.fromisoformat("2026-03-08T04:00:00"),
        "America/New_York",
    )

    assert [row[0] for row in result["rows"]] == [
        "2026-03-08T06:30:00+00:00",
        "2026-03-08T07:30:00+00:00",
    ]
    assert [row[4] for row in result["rows"]] == [
        "2026-03-08T01:30:00-05:00",
        "2026-03-08T03:30:00-04:00",
    ]


def test_compare_windows_midday_vs_evening(postgres_connection):
    _seed(
        postgres_connection,
        _sample("heart_rate", "2026-06-01T12:00:00Z", 60, 0),
        _sample("heart_rate", "2026-06-01T12:01:00Z", 60, 1),
        _sample("heart_rate", "2026-06-01T12:02:00Z", 60, 2),
        _sample("heart_rate", "2026-06-01T20:00:00Z", 80, 3),
        _sample("heart_rate", "2026-06-02T12:00:00Z", 70, 0),
        _sample("heart_rate", "2026-06-02T20:00:00Z", 90, 1),
    )

    result = timeseries.compare_time_windows(
        postgres_connection,
        "heart_rate",
        datetime.fromisoformat("2026-06-01").date(),
        datetime.fromisoformat("2026-06-02").date(),
        [
            timeseries.TimeWindow(name="midday", start_time="12:00", end_time="13:00"),
            timeseries.TimeWindow(name="evening", start_time="20:00", end_time="21:00"),
        ],
        "UTC",
    )

    midday, evening = result["windows"]
    assert midday["daily_means"] == [60, 70]
    assert evening["daily_means"] == [80, 90]
    assert midday["mean"] == 65 and evening["mean"] == 85
    assert midday["sample_count"] == 4 and evening["sample_count"] == 2
    assert midday["valid_days"] == evening["valid_days"] == 2


def test_compare_windows_reports_missing_days(postgres_connection):
    _seed(
        postgres_connection,
        _sample("heart_rate", "2026-06-01T12:00:00Z", 60),
        _sample("heart_rate", "2026-06-03T12:00:00Z", 80),
    )

    result = timeseries.compare_time_windows(
        postgres_connection,
        "heart_rate",
        datetime.fromisoformat("2026-06-01").date(),
        datetime.fromisoformat("2026-06-03").date(),
        [timeseries.TimeWindow(name="midday", start_time="12:00", end_time="13:00")],
        "UTC",
    )

    window = result["windows"][0]
    assert window["daily_means"] == [60, None, 80]
    assert window["valid_days"] == 2
    assert window["missing_days"] == ["2026-06-02"]
    assert window["sample_count"] == 2


def test_compare_windows_supports_overnight_windows(postgres_connection):
    _seed(
        postgres_connection,
        _sample("heart_rate", "2026-06-02T01:00:00Z", 60),
        _sample("heart_rate", "2026-06-02T23:00:00Z", 80),
        _sample("heart_rate", "2026-06-03T01:00:00Z", 100),
    )

    result = timeseries.compare_time_windows(
        postgres_connection,
        "heart_rate",
        datetime.fromisoformat("2026-06-01").date(),
        datetime.fromisoformat("2026-06-02").date(),
        [timeseries.TimeWindow(name="overnight", start_time="22:00", end_time="02:00")],
        "UTC",
    )

    window = result["windows"][0]
    assert window["daily_means"] == [60, 90]
    assert window["daily_sample_counts"] == [1, 2]
    assert window["missing_days"] == []


def test_query_samples_caps_and_paginates(postgres_connection):
    _seed(
        postgres_connection,
        *[
            _sample(
                "heart_rate",
                f"2026-06-01T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
                index,
            )
            for index in range(5002)
        ],
    )

    result = timeseries.query_samples(
        postgres_connection,
        ["heart_rate"],
        datetime.fromisoformat("2026-06-01T00:00:00+00:00"),
        datetime.fromisoformat("2026-06-02T00:00:00+00:00"),
        "UTC",
        limit=10_000,
        offset=1,
    )

    assert result["total"] == 5002
    assert len(result["rows"]) == 5000
    assert result["rows"][0][2] == 1 and result["rows"][-1][2] == 5000
    assert result["limit"] == 5000 and result["offset"] == 1
    assert result["truncated"] is True
