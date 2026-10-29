from garmin_mcp import analysis, db, demo
from garmin_mcp.cli import main


def _store(conn, **kwargs):
    report = demo.generate(conn, end="2026-08-02", **kwargs)
    return report


def _clear(conn):
    with conn.transaction():
        for table in (
            "daily_wellness", "sleep", "hrv", "training_status", "activities", "demo_info",
        ):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed test table names


def test_generates_every_table(postgres_connection):
    report = _store(postgres_connection, days=120)
    assert report["start"] == "2026-04-05"
    assert report["end"] == "2026-08-02"
    for table in ("daily_wellness", "hrv", "training_status"):
        assert report["rows"][table] == 120
    assert report["rows"]["sleep"] == 120 - len(report["missing_sleep"])
    assert report["rows"]["activities"] > 0


def test_deterministic_for_a_given_seed(postgres_connection):
    conn = postgres_connection
    _store(conn, days=60, seed=7)
    rows_a = conn.execute("SELECT * FROM daily_wellness ORDER BY date").fetchall()
    _clear(conn)
    _store(conn, days=60, seed=7)
    rows_b = conn.execute("SELECT * FROM daily_wellness ORDER BY date").fetchall()
    assert [tuple(r.values())[:-1] for r in rows_a] == [tuple(r.values())[:-1] for r in rows_b]


def test_different_seeds_differ(postgres_connection):
    conn = postgres_connection
    _store(conn, days=60, seed=1)
    a = [r["resting_hr"] for r in conn.execute("SELECT resting_hr FROM daily_wellness")]
    _clear(conn)
    _store(conn, days=60, seed=2)
    b = [r["resting_hr"] for r in conn.execute("SELECT resting_hr FROM daily_wellness")]
    assert a != b


def test_rejects_tiny_ranges(postgres_connection):
    try:
        demo.generate(postgres_connection, days=5)
    except ValueError as e:
        assert "at least 14" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_short_ranges_keep_the_illness_window_in_order(postgres_connection):
    for days in (14, 20, 30, 45):
        _clear(postgres_connection)
        report = _store(postgres_connection, days=days)
        start, end = report["illness_window"]
        assert start <= end, f"{days}d produced a backwards window {start}..{end}"
        assert report["start"] <= start and end <= report["end"]
        assert len(report["missing_sleep"]) >= 1


def test_marked_as_a_demo_store(postgres_connection):
    _store(postgres_connection, days=30)
    assert demo.is_demo(postgres_connection) is True
    _clear(postgres_connection)
    assert demo.is_demo(postgres_connection) is False


def test_sleep_stages_sum_to_duration(postgres_connection):
    _store(postgres_connection, days=90)
    for row in postgres_connection.execute("SELECT * FROM sleep"):
        stages = row["deep_min"] + row["light_min"] + row["rem_min"] + row["awake_min"]
        assert abs(stages - row["duration_min"]) <= 4


def test_values_stay_physiologically_plausible(postgres_connection):
    _store(postgres_connection, days=180)
    for row in postgres_connection.execute("SELECT * FROM daily_wellness"):
        assert 46 <= row["resting_hr"] <= 78
        assert 0 <= row["steps"] <= 21000
        assert 88 <= row["spo2_avg"] <= 100
    for row in postgres_connection.execute("SELECT * FROM hrv"):
        assert 16 <= row["last_night_avg"] <= 62


def test_analysis_tools_find_real_structure(postgres_connection):
    conn = postgres_connection
    report = _store(conn, days=180)
    start, end = report["start"], report["end"]
    hrv_rhr = analysis.correlate(conn, "hrv", "resting_hr", start, end)
    assert hrv_rhr["pearson_r"] < -0.3
    load_rhr = analysis.correlate(conn, "training_load", "resting_hr", start, end, scan_lags=True)
    assert load_rhr["best_lag"]["lag"] == 1
    assert load_rhr["best_lag"]["r"] > 0.2
    found = analysis.anomalies(conn, None, start, end)["anomalies"]
    ill_start, ill_end = report["illness_window"]
    during = {a["metric"] for a in found if ill_start <= a["date"] <= ill_end}
    assert {"resting_hr", "hrv", "skin_temp_dev_c"} <= during


def test_gaps_reports_the_missing_sleep_nights(postgres_connection):
    report = _store(postgres_connection, days=180)
    missing = analysis.gaps(
        postgres_connection, report["start"], report["end"]
    )["missing"]
    assert missing["sleep"] == report["missing_sleep"]
    assert missing["daily_wellness"] == []


def test_cli_refuses_to_overwrite_a_real_store(tmp_path, capsys, postgres_connection):
    db.upsert(
        postgres_connection, "daily_wellness", {"date": "2026-01-01", "resting_hr": 55}, ("date",)
    )
    assert main(["--data-dir", str(tmp_path), "demo"]) == 2
    assert "Refusing to overwrite real data" in capsys.readouterr().err
    row = postgres_connection.execute(
        "SELECT COUNT(*) AS count FROM daily_wellness"
    ).fetchone()
    assert row["count"] == 1


def test_cli_regenerates_an_existing_demo_store(tmp_path, postgres_connection):
    assert main(["--data-dir", str(tmp_path), "demo", "--days", "30"]) == 0
    assert main(["--data-dir", str(tmp_path), "demo", "--days", "40"]) == 0
    count = postgres_connection.execute(
        "SELECT COUNT(*) AS count FROM daily_wellness"
    ).fetchone()["count"]
    assert count == 40
