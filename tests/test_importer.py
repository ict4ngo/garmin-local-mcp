import pytest

from garmin_mcp import db, importer


@pytest.fixture
def decoded(monkeypatch):
    """Bypass FIT parsing: importer tests exercise the upsert/precedence logic."""
    payload = {
        "date": "2026-06-24",
        "sleep_score": 84,
        "sleep": {"start": "2026-06-23T22:50:00", "end": "2026-06-24T06:09:00",
                  "duration_min": 440},
        "sleep_stages": {"deep_min": 52, "light_min": 219, "rem_min": 92, "awake_min": 74,
                         "max_logging_gap_min": 60},
        "hrv": {"last_night_avg": 33, "weekly_avg": 33, "high_5min": 50,
                "baseline_low": 29, "baseline_high": 35, "status": "balanced"},
        "skin_temp": {"nightly_c": 35.8, "deviation_c": -0.07, "avg_dev_7d": None},
        "heart_rate": {"min": 53, "avg": 74, "max": 166},
        "steps": 4773, "distance_m": 3728.8,
        "stress": {"avg": 28, "max": 99},
        "rhr_on_device": 69,
        "naps": [],
    }
    monkeypatch.setattr(importer, "decode_bundle", lambda folder: payload)
    return payload


def test_import_bundle(tmp_path, decoded, postgres_connection):
    conn = postgres_connection
    report = importer.import_bundle(conn, tmp_path)
    assert report["date"] == "2026-06-24"
    assert set(report["imported"]) == {"sleep", "hrv", "daily_wellness"}
    assert "rhr_far_above_hr_floor" in report["quality_flags"]

    row = conn.execute("SELECT * FROM daily_wellness WHERE date='2026-06-24'").fetchone()
    assert row["resting_hr"] is None  # flagged on-device RHR is withheld
    assert row["min_hr"] == 53
    assert row["source"] == "fit"
    sleep = conn.execute("SELECT * FROM sleep WHERE date='2026-06-24'").fetchone()
    assert sleep["score"] == 84


def test_api_rows_not_overwritten(tmp_path, decoded, postgres_connection):
    source = tmp_path / "2026-06-24"
    source.mkdir()
    (source / "sleep.fit").write_bytes(b"fit input")
    conn = postgres_connection
    db.upsert(conn, "sleep", {"date": "2026-06-24", "score": 99, "source": "api"}, ("date",))
    raw_dir = tmp_path / "persistent" / "raw"
    report = importer.import_bundle(conn, source, raw_dir=raw_dir)
    assert any(s.startswith("sleep") for s in report["skipped"])
    assert conn.execute("SELECT score FROM sleep").fetchone()["score"] == 99

    report = importer.import_bundle(conn, source, force=True, raw_dir=raw_dir)
    assert "sleep" in report["imported"]
    assert conn.execute("SELECT score FROM sleep").fetchone()["score"] == 84


def test_import_archives_fit_source_once(tmp_path, decoded, postgres_connection, monkeypatch):
    source = tmp_path / "export" / "2026-06-24"
    source.mkdir(parents=True)
    (source / "wellness.fit").write_bytes(b"original FIT bytes")
    raw_dir = tmp_path / "data" / "raw"
    decoded_folders = []

    def decode(folder):
        decoded_folders.append(folder)
        assert (folder / "wellness.fit").read_bytes() == b"original FIT bytes"
        return decoded

    monkeypatch.setattr(importer, "decode_bundle", decode)
    conn = postgres_connection
    importer.import_bundle(conn, source, raw_dir=raw_dir)
    importer.import_bundle(conn, source, raw_dir=raw_dir)

    archives = list((raw_dir / "fit" / "2026-06-24").glob("*"))
    assert len(archives) == 1
    assert (archives[0] / "wellness.fit").read_bytes() == b"original FIT bytes"
    assert decoded_folders == [archives[0], archives[0]]


def test_missing_date_raises(tmp_path, monkeypatch, postgres_connection):
    monkeypatch.setattr(importer, "decode_bundle", lambda folder: {"date": None})
    conn = postgres_connection
    with pytest.raises(ValueError, match="date"):
        importer.import_bundle(conn, tmp_path)
