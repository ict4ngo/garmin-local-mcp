"""Timestamped Garmin samples stored independently from daily summaries."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import Connection


@dataclass(frozen=True)
class Sample:
    source_endpoint: str
    source_id: str
    metric: str
    observed_at: datetime
    source_date: str
    sample_index: int
    value: float
    unit: str | None
    local_timestamp: str | None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.sample_index < 0:
            raise ValueError("sample_index must be non-negative")
        if not math.isfinite(self.value):
            raise ValueError("sample value must be finite")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


def upsert_samples(conn: Connection, samples: Sequence[Sample], fetched_at: datetime) -> int:
    """Insert samples or update the materialized value for the same source identity."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    rows = _sample_rows(samples, fetched_at)
    if not rows:
        return 0
    with conn.transaction():
        _upsert_rows(conn, rows)
    return len(rows)


def replace_samples(
    conn: Connection,
    source_endpoint: str,
    source_id: str,
    samples: Sequence[Sample],
    fetched_at: datetime,
) -> int:
    """Make one fetched source's materialized points match its latest payload."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    rows = _sample_rows(samples, fetched_at)
    with conn.transaction():
        conn.execute(
            "DELETE FROM samples WHERE source_endpoint=%s AND source_id=%s",
            (source_endpoint, source_id),
        )
        _upsert_rows(conn, rows)
    return len(rows)


def _sample_rows(samples: Sequence[Sample], fetched_at: datetime) -> list[tuple]:
    rows = [
        (
            item.source_endpoint,
            item.source_id,
            item.metric,
            item.observed_at,
            item.source_date,
            item.sample_index,
            item.value,
            item.unit,
            item.local_timestamp,
            fetched_at.astimezone(UTC),
        )
        for item in samples
    ]
    return rows


def _upsert_rows(conn: Connection, rows: Sequence[tuple]) -> None:
    for row in rows:
        conn.execute(
            """INSERT INTO samples
               (source_endpoint, source_id, metric, observed_at, source_date, sample_index,
                value, unit, local_timestamp, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(source_endpoint, source_id, metric, observed_at, sample_index)
               DO UPDATE SET source_date=excluded.source_date, value=excluded.value,
                 unit=excluded.unit, local_timestamp=excluded.local_timestamp,
                 fetched_at=excluded.fetched_at""",
            row,
        )
