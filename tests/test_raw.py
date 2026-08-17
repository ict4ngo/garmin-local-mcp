from garmin_mcp import raw


def test_write_and_skip(conn, tmp_path):
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


def test_iter_daily_snapshots(conn, tmp_path):
    raw_dir = tmp_path / "raw"
    for date, ep in [("2026-07-02", "sleep"), ("2026-07-01", "hrv"), ("2026-07-01", "sleep")]:
        raw.write_snapshot(conn, raw_dir, raw.daily_path(raw_dir, date, ep), {}, date, ep)
    got = [(d, e) for d, e, _ in raw.iter_daily_snapshots(raw_dir)]
    assert got == [("2026-07-01", "hrv"), ("2026-07-01", "sleep"), ("2026-07-02", "sleep")]


def test_iter_empty(tmp_path):
    assert list(raw.iter_daily_snapshots(tmp_path / "raw")) == []


def _seed_snapshots(conn, raw_dir, dates):
    for date in dates:
        raw.write_snapshot(
            conn, raw_dir, raw.daily_path(raw_dir, date, "sleep"), {"d": date}, date, "sleep"
        )


def test_prune_snapshots_removes_old_only(conn, tmp_path):
    raw_dir = tmp_path / "raw"
    _seed_snapshots(conn, raw_dir, ["2020-05-01", "2024-12-31", "2026-01-10"])

    report = raw.prune_snapshots(conn, raw_dir, "2021-01-01")
    assert report["removed_files"] == 1 and report["removed_rows"] == 1

    assert not raw.daily_path(raw_dir, "2020-05-01", "sleep").exists()
    assert raw.daily_path(raw_dir, "2024-12-31", "sleep").is_file()
    assert raw.daily_path(raw_dir, "2026-01-10", "sleep").is_file()
    # emptied date/year dirs are tidied away
    assert not (raw_dir / "daily" / "2020").exists()
    assert (raw_dir / "daily" / "2024").is_dir()

    remaining = conn.execute("SELECT path FROM raw_snapshots").fetchall()
    assert len(remaining) == 2
    assert all(r["path"] != "daily/2020/2020-05-01/sleep.json" for r in remaining)


def test_prune_dry_run_touches_nothing(conn, tmp_path):
    raw_dir = tmp_path / "raw"
    _seed_snapshots(conn, raw_dir, ["2020-05-01"])

    report = raw.prune_snapshots(conn, raw_dir, "2021-01-01", dry_run=True)
    assert report["dry_run"] is True and report["removed_files"] == 1
    assert raw.daily_path(raw_dir, "2020-05-01", "sleep").is_file()
    assert conn.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()["n"] == 1


def test_prune_keeps_rows_without_a_date(conn, tmp_path):
    raw_dir = tmp_path / "raw"
    path = raw.activity_path(raw_dir, 42)
    raw.write_snapshot(conn, raw_dir, path, {"x": 1}, None, "activities")

    report = raw.prune_snapshots(conn, raw_dir, "2099-01-01")
    assert report["removed_rows"] == 0
    assert path.is_file()
