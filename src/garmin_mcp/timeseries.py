"""Bounded raw sample queries and fair comparisons of local-time windows."""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from pydantic import BaseModel, field_validator

from .metrics import SAMPLE_METRICS

MAX_RAW_ROWS = 5000


class TimeWindow(BaseModel):
    """A caller-named wall-clock interval; end before start means overnight."""

    name: str
    start_time: str
    end_time: str

    @field_validator("name")
    @classmethod
    def _nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("window name must not be empty")
        return value

    @field_validator("start_time", "end_time")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("window times must use HH:MM format") from exc
        if len(value) != 5 or parsed.strftime("%H:%M") != value:
            raise ValueError("window times must use HH:MM format")
        return value


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone {timezone!r}") from exc


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    """Interpret a naive wall-clock value; reject DST gaps, choose fold=0."""
    candidate = value.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != value:
        raise ValueError(f"Local datetime {value.isoformat()} does not exist in {zone.key}")
    return candidate


def _aware_bound(value: datetime, zone: ZoneInfo, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return _localize(value, zone).astimezone(UTC)
    return value.astimezone(UTC)


def _validate_metrics(metrics: list[str]) -> None:
    if not metrics:
        raise ValueError("At least one sample metric is required")
    unknown = sorted(set(metrics) - SAMPLE_METRICS.keys())
    if unknown:
        raise ValueError(
            f"Unknown sample metric(s): {', '.join(unknown)}. "
            f"Known metrics: {', '.join(sorted(SAMPLE_METRICS))}"
        )


def query_samples(
    conn: Connection,
    metrics: list[str],
    start: datetime,
    end: datetime,
    timezone: str,
    limit: int = 1000,
    offset: int = 0,
) -> dict:
    """Return a stable, paginated sample table over the half-open UTC range."""
    _validate_metrics(metrics)
    zone = _zone(timezone)
    start_utc = _aware_bound(start, zone, "start")
    end_utc = _aware_bound(end, zone, "end")
    if start_utc >= end_utc:
        raise ValueError("start must be before end")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    bounded_limit = min(limit, MAX_RAW_ROWS)
    placeholders = ", ".join("%s" for _ in metrics)
    where = f"metric IN ({placeholders}) AND observed_at >= %s AND observed_at < %s"
    params = [*metrics, start_utc, end_utc]
    total = conn.execute(f"SELECT COUNT(*) AS n FROM samples WHERE {where}", params).fetchone()["n"]
    rows = conn.execute(
        "SELECT observed_at, metric, value, unit, local_timestamp FROM samples "
        f"WHERE {where} ORDER BY observed_at, metric, source_endpoint, source_id, sample_index "
        "LIMIT %s OFFSET %s",
        [*params, bounded_limit, offset],
    ).fetchall()
    result_rows = []
    for row in rows:
        observed_at = row["observed_at"].astimezone(UTC)
        local_timestamp = row["local_timestamp"]
        if local_timestamp is None:
            local_timestamp = observed_at.astimezone(zone).isoformat()
        result_rows.append(
            [observed_at.isoformat(), row["metric"], row["value"], row["unit"], local_timestamp]
        )
    return {
        "cols": ["timestamp", "metric", "value", "unit", "local_timestamp"],
        "rows": result_rows,
        "total": total,
        "limit": bounded_limit,
        "offset": offset,
        "truncated": offset + len(result_rows) < total,
    }


def _window_bounds(day: date, window: TimeWindow, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start_clock = time.fromisoformat(window.start_time)
    end_clock = time.fromisoformat(window.end_time)
    start_naive = datetime.combine(day, start_clock)
    end_day = day + timedelta(days=end_clock <= start_clock)
    end_naive = datetime.combine(end_day, end_clock)
    return _localize(start_naive, zone).astimezone(UTC), _localize(end_naive, zone).astimezone(UTC)


def compare_time_windows(
    conn: Connection,
    metric: str,
    start_date: date,
    end_date: date,
    windows: list[TimeWindow],
    timezone: str,
) -> dict:
    """Compare daily means inside local-time windows, weighting each day once."""
    _validate_metrics([metric])
    zone = _zone(timezone)
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if not windows:
        raise ValueError("At least one time window is required")
    if len({window.name for window in windows}) != len(windows):
        raise ValueError("Window names must be unique")

    days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
    results = []
    for window in windows:
        daily_means: list[float | None] = []
        daily_counts: list[int] = []
        missing_days: list[str] = []
        for day in days:
            start_utc, end_utc = _window_bounds(day, window, zone)
            row = conn.execute(
                "SELECT AVG(value) AS mean, COUNT(*) AS n FROM samples "
                "WHERE metric=%s AND observed_at >= %s AND observed_at < %s",
                (metric, start_utc, end_utc),
            ).fetchone()
            count = row["n"]
            mean = float(row["mean"]) if count else None
            daily_means.append(mean)
            daily_counts.append(count)
            if count == 0:
                missing_days.append(day.isoformat())
        valid_means = [value for value in daily_means if value is not None]
        results.append(
            {
                "name": window.name,
                "daily": [
                    {"date": day.isoformat(), "mean": mean, "sample_count": count}
                    for day, mean, count in zip(days, daily_means, daily_counts, strict=True)
                ],
                "daily_means": daily_means,
                "daily_sample_counts": daily_counts,
                "mean": statistics.fmean(valid_means) if valid_means else None,
                "median": statistics.median(valid_means) if valid_means else None,
                "valid_days": len(valid_means),
                "sample_count": sum(daily_counts),
                "missing_days": missing_days,
            }
        )
    return {
        "metric": metric,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": timezone,
        "expected_days": len(days),
        "windows": results,
    }
