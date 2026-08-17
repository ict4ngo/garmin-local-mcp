from garmin_mcp import analysis, db, demo
from garmin_mcp.cli import main

END = "2026-08-02"


def _store(conn, **kwargs):
    report = demo.generate(conn, end=END, **kwargs)
    return report


def test_generates_every_table(conn):
    report = _store(conn, days=120)
    assert report["start"] == "2026-04-05"
    assert report["end"] == END
    for table in ("daily_wellness", "hrv", "training_status"):
        assert report["rows"][table] == 120
    # Sleep deliberately leaves a few nights out so `gaps` has something to find.
    assert report["rows"]["sleep"] == 120 - len(report["missing_sleep"])
    assert report["rows"]["activities"] > 0


def test_deterministic_for_a_given_seed(conn):
    _store(conn, days=60, seed=7)
    rows_a = conn.execute("SELECT * FROM daily_wellness ORDER BY date").fetchall()
    db.drop_all(conn)
    db.migrate(conn)
    _store(conn, days=60, seed=7)
    rows_b = conn.execute("SELECT * FROM daily_wellness ORDER BY date").fetchall()
    assert [tuple(r.values())[:-1] for r in rows_a] == [tuple(r.values())[:-1] for r in rows_b]


def test_different_seeds_differ(conn):
    _store(conn, days=60, seed=1)
    a = [r["resting_hr"] for r in conn.execute("SELECT resting_hr FROM daily_wellness")]
    db.drop_all(conn)
    db.migrate(conn)
    _store(conn, days=60, seed=2)
    b = [r["resting_hr"] for r in conn.execute("SELECT resting_hr FROM daily_wellness")]
    assert a != b


def test_rejects_tiny_ranges(conn):
    try:
        demo.generate(conn, days=5)
    except ValueError as e:
        assert "at least 14" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_short_ranges_keep_the_illness_window_in_order(conn):
    """days // 2 - 20 goes negative under ~40 days and used to index backwards."""
    for days in (14, 20, 30, 45):
        db.drop_all(conn)
        db.migrate(conn)
        report = _store(conn, days=days)
        start, end = report["illness_window"]
        assert start <= end, f"{days}d produced a backwards window {start}..{end}"
        assert report["start"] <= start and end <= report["end"]
        assert len(report["missing_sleep"]) >= 1


def test_marked_as_a_demo_store(conn):
    _store(conn, days=30)
    assert demo.is_demo(conn) is True
    db.drop_all(conn)
    db.migrate(conn)
    assert demo.is_demo(conn) is False


def test_sleep_stages_sum_to_duration(conn):
    _store(conn, days=90)
    for row in conn.execute("SELECT * FROM sleep"):
        stages = row["deep_min"] + row["light_min"] + row["rem_min"] + row["awake_min"]
        # Stages are stored as ints, so allow rounding slack against the float total.
        assert abs(stages - row["duration_min"]) <= 4


def test_values_stay_physiologically_plausible(conn):
    _store(conn, days=180)
    for row in conn.execute("SELECT * FROM daily_wellness"):
        assert 46 <= row["resting_hr"] <= 78
        assert 0 <= row["steps"] <= 21000
        assert 88 <= row["spo2_avg"] <= 100
    for row in conn.execute("SELECT * FROM hrv"):
        assert 16 <= row["last_night_avg"] <= 62


def test_analysis_tools_find_real_structure(conn):
    """The point of the demo store: the tools must return signal, not noise."""
    report = _store(conn, days=180)
    start, end = report["start"], report["end"]

    # A latent recovery factor drives HRV and resting HR in opposite directions.
    hrv_rhr = analysis.correlate(conn, "hrv", "resting_hr", start, end)
    assert hrv_rhr["pearson_r"] < -0.3

    # Training load shows up in the *next* day's resting HR, not the same day.
    load_rhr = analysis.correlate(
        conn, "training_load", "resting_hr", start, end, scan_lags=True
    )
    assert load_rhr["best_lag"]["lag"] == 1
    assert load_rhr["best_lag"]["r"] > 0.2

    # The seeded illness window is detectable across several metrics at once.
    found = analysis.anomalies(conn, None, start, end)["anomalies"]
    ill_start, ill_end = report["illness_window"]
    during = {a["metric"] for a in found if ill_start <= a["date"] <= ill_end}
    assert {"resting_hr", "hrv", "skin_temp_dev_c"} <= during


def test_gaps_reports_the_missing_sleep_nights(conn):
    report = _store(conn, days=180)
    missing = analysis.gaps(conn, report["start"], report["end"])["missing"]
    assert missing["sleep"] == report["missing_sleep"]
    assert missing["daily_wellness"] == []


def test_cli_refuses_to_overwrite_a_real_store(conn, db_url, tmp_path, monkeypatch):
    db.upsert(conn, "daily_wellness", {"date": "2026-01-01", "resting_hr": 55}, ("date",))
    conn.close()
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", db_url)

    assert main(["--data-dir", str(tmp_path), "demo"]) == 2

    # The real row must still be there.
    check = db.connect(db_url)
    assert check.execute("SELECT COUNT(*) AS n FROM daily_wellness").fetchone()["n"] == 1
    check.close()


def test_cli_regenerates_an_existing_demo_store(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", fresh_db)
    assert main(["--data-dir", str(tmp_path), "demo", "--days", "30"]) == 0
    assert main(["--data-dir", str(tmp_path), "demo", "--days", "40"]) == 0
    conn = db.connect(fresh_db)
    assert conn.execute("SELECT COUNT(*) AS n FROM daily_wellness").fetchone()["n"] == 40
    conn.close()
