import hashlib
import json

import pytest

from garmin_mcp import raw


def test_write_and_skip(tmp_path, postgres_connection):
    conn = postgres_connection
    raw_dir = tmp_path / "raw"
    p = raw.daily_path(raw_dir, "2026-07-02", "sleep")
    assert p == raw_dir / "daily" / "2026" / "2026-07-02" / "sleep.json"

    assert raw.write_snapshot(conn, raw_dir, p, {"score": 86}, "2026-07-02", "sleep")
    assert raw.read_snapshot(p) == {"score": 86}

    # immutability: second write is skipped, content unchanged
    assert not raw.write_snapshot(conn, raw_dir, p, {"score": 0}, "2026-07-02", "sleep")
    assert raw.read_snapshot(p) == {"score": 86}

    row = conn.execute("SELECT * FROM raw_snapshots").fetchone()
    assert row["path"] == "daily/2026/2026-07-02/sleep.json"
    assert row["endpoint"] == "sleep"
    assert row["bytes"] > 0


def test_iter_daily_snapshots(tmp_path, postgres_connection):
    conn = postgres_connection
    raw_dir = tmp_path / "raw"
    for date, ep in [("2026-07-02", "sleep"), ("2026-07-01", "hrv"), ("2026-07-01", "sleep")]:
        raw.write_snapshot(conn, raw_dir, raw.daily_path(raw_dir, date, ep), {}, date, ep)
    got = [(d, e) for d, e, _ in raw.iter_daily_snapshots(raw_dir)]
    assert got == [("2026-07-01", "hrv"), ("2026-07-01", "sleep"), ("2026-07-02", "sleep")]


def test_iter_empty(tmp_path):
    assert list(raw.iter_daily_snapshots(tmp_path / "raw")) == []


def test_snapshot_refresh_keeps_prior_version(tmp_path, postgres_connection):
    conn = postgres_connection
    raw_dir = tmp_path / "raw"
    assert raw.write_snapshot(
        conn, raw_dir, raw.daily_path(raw_dir, "2026-07-02", "stress"), {"value": 1},
        "2026-07-02", "stress"
    )
    first = raw.write_versioned_snapshot(
        conn, raw_dir, {"value": 2}, "2026-07-02", "stress", "2026-07-02T12:00:00.000001Z"
    )
    second = raw.write_versioned_snapshot(
        conn, raw_dir, {"value": 3}, "2026-07-02", "stress", "2026-07-02T12:00:01.000001Z"
    )

    assert raw.read_snapshot(raw.daily_path(raw_dir, "2026-07-02", "stress")) == {"value": 1}
    assert raw.read_snapshot(first) == {"value": 2}
    assert raw.read_snapshot(second) == {"value": 3}
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint='stress'"
    ).fetchone()["count"] == 3


def test_latest_snapshot_orders_fractional_timestamps(tmp_path, postgres_connection):
    conn = postgres_connection
    raw_dir = tmp_path / "raw"
    raw.write_snapshot(
        conn, raw_dir, raw.daily_path(raw_dir, "2026-07-02", "stress"), {"value": 1},
        "2026-07-02", "stress"
    )
    raw.write_versioned_snapshot(
        conn, raw_dir, {"value": 2}, "2026-07-02", "stress", "2026-07-02T12:00:00Z"
    )
    raw.write_versioned_snapshot(
        conn, raw_dir, {"value": 3}, "2026-07-02", "stress",
        "2026-07-02T12:00:00.000001Z"
    )

    [(day, endpoint, path)] = raw.latest_daily_snapshots(raw_dir)
    assert (day, endpoint, raw.read_snapshot(path)) == ("2026-07-02", "stress", {"value": 3})


def test_failed_atomic_snapshot_replace_never_exposes_partial_destination(
    tmp_path, postgres_connection, monkeypatch
):
    conn = postgres_connection
    raw_dir = tmp_path / "raw"
    payload = {"value": "complete snapshot"}
    fetched_at = "2026-07-02T12:00:00.000001Z"
    data = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    destination = raw.versioned_daily_path(
        raw_dir, "2026-07-02", "stress", fetched_at, digest
    )

    def fail_replace(source, target):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(raw.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        raw.write_versioned_snapshot(
            conn, raw_dir, payload, "2026-07-02", "stress", fetched_at
        )

    assert not destination.exists()
    assert not list(destination.parent.glob("*.json"))
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM raw_snapshots WHERE endpoint='stress'"
    ).fetchone()["count"] == 0


def test_iter_fit_archives_ignores_symlinks(tmp_path):
    raw_dir = tmp_path / "raw"
    archive = raw_dir / "fit" / "2026-06-24" / "bundle-1"
    archive.mkdir(parents=True)
    (archive / "sleep.fit").write_bytes(b"fit")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.fit").write_bytes(b"outside")
    (archive / "external.fit").symlink_to(outside / "external.fit")
    (raw_dir / "fit" / "2026-06-25").symlink_to(outside, target_is_directory=True)

    assert list(raw.iter_fit_archives(raw_dir)) == [("2026-06-24", archive)]
