import json
from datetime import UTC, datetime
from pathlib import Path

from garmin_mcp import endpoints, samples

FIXTURES = Path(__file__).parent / "fixtures"
DATE = "2026-01-15"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_stress_samples():
    parsed = endpoints.parse_stress_samples(load("stress_samples.json"), DATE)
    assert [(item.observed_at, item.value, item.unit) for item in parsed] == [
        (datetime(2026, 1, 15, 12, 0, tzinfo=UTC), 35.0, "stress_level"),
        (datetime(2026, 1, 15, 12, 15, tzinfo=UTC), 42.0, "stress_level"),
    ]
    assert [item.sample_index for item in parsed] == [0, 1]
    assert all(item.source_date == DATE for item in parsed)


def test_parse_body_battery_samples():
    parsed = endpoints.parse_body_battery_samples(load("body_battery_samples.json"), DATE)
    assert [(item.observed_at.isoformat(), item.value) for item in parsed] == [
        ("2026-01-15T12:00:00+00:00", 66.0),
        ("2026-01-15T12:15:00+00:00", 65.0),
    ]
    assert all(item.unit == "body_battery" for item in parsed)


def test_parse_sleep_samples():
    parsed = endpoints.parse_sleep_samples(load("sleep.json"), DATE)
    assert [(item.metric, item.observed_at.isoformat(), item.value) for item in parsed] == [
        ("sleep_heart_rate", "2026-01-15T03:45:00+00:00", 52.0),
        ("sleep_heart_rate", "2026-01-15T03:47:00+00:00", 49.0),
        ("sleep_hrv", "2026-01-15T03:50:00+00:00", 42.0),
        ("sleep_hrv", "2026-01-15T03:55:00+00:00", 38.0),
        ("sleep_stress", "2026-01-15T03:45:00+00:00", 18.0),
        ("sleep_stress", "2026-01-15T03:48:00+00:00", 14.0),
        ("sleep_stage", "2026-01-15T03:45:00+00:00", 1.0),
        ("sleep_stage", "2026-01-15T04:15:00+00:00", 3.0),
        ("sleep_movement", "2026-01-15T03:45:00+00:00", 1.2),
        ("sleep_movement", "2026-01-15T03:46:00+00:00", 0.4),
        ("sleep_restless", "2026-01-15T04:02:00+00:00", 1.0),
        ("sleep_restless", "2026-01-15T05:31:00+00:00", 2.0),
    ]
    assert [sample.sample_index for sample in parsed] == [0, 1] * 6
    assert endpoints.parse_sleep_samples({"dailySleepDTO": {}}, DATE) == []
    assert endpoints.parse_sleep_samples({"dailySleepDTO": None}, DATE) == []


def test_parse_activity_detail_samples():
    parsed = endpoints.parse_activity_detail_samples(
        90000000001, load("activity_detail_samples.json"), DATE
    )
    assert [(item.metric, item.observed_at.isoformat(), item.value) for item in parsed] == [
        ("heart_rate", "2026-01-15T12:00:00+00:00", 145.0),
        ("speed", "2026-01-15T12:00:00+00:00", 3.2),
        ("heart_rate", "2026-01-15T12:00:05+00:00", 148.0),
        ("speed", "2026-01-15T12:00:05+00:00", 3.4),
    ]
    assert all(item.source_id == "90000000001" for item in parsed)


def test_parse_other_timestamped_health_samples():
    parser_cases = [
        ("parse_heart_rate_samples", "heart_rate_samples.json", "heart_rate", "bpm", 73.0),
        ("parse_spo2_samples", "spo2_samples.json", "spo2", "percent", 97.0),
        (
            "parse_respiration_samples",
            "respiration_samples.json",
            "respiration",
            "breaths_per_minute",
            14.2,
        ),
        (
            "parse_daily_movement_samples",
            "daily_movement_samples.json",
            "movement",
            None,
            1.25,
        ),
        (
            "parse_training_readiness_samples",
            "training_readiness_samples.json",
            "training_readiness",
            "score",
            72.0,
        ),
    ]
    for function_name, fixture_name, metric, unit, value in parser_cases:
        parsed = getattr(endpoints, function_name)(load(fixture_name), DATE)
        assert parsed[0].metric == metric
        assert parsed[0].unit == unit
        assert parsed[0].value == value
        assert parsed[0].observed_at == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        assert getattr(endpoints, function_name)({}, DATE) == []

    heart_rates = endpoints.parse_heart_rate_samples(load("heart_rate_samples.json"), DATE)
    assert heart_rates[-1].observed_at == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    assert heart_rates[-1].local_timestamp == "2026-01-15T07:30:00-05:00"

    readiness = endpoints.parse_training_readiness_samples(
        load("training_readiness_samples.json"), DATE
    )
    assert readiness[0].local_timestamp == "2026-01-15T07:00:00-05:00"


def test_sample_endpoint_fetchers_are_date_scoped():
    calls = []

    class Client:
        def __getattr__(self, name):
            def fetch(*args):
                calls.append((name, *args))
                return []

            return fetch

    client = Client()
    for endpoint in endpoints.SAMPLE_ENDPOINTS.values():
        endpoint.fetch(client, DATE)
    assert calls == [
        ("get_stress_data", DATE),
        ("get_body_battery", DATE, DATE),
        ("get_heart_rates", DATE),
        ("get_spo2_data", DATE),
        ("get_respiration_data", DATE),
        ("get_daily_movement", DATE),
        ("get_training_readiness", DATE),
    ]


def test_sample_upsert_is_idempotent(postgres_connection):
    sample = samples.Sample(
        source_endpoint="stress",
        source_id=DATE,
        metric="stress_level",
        observed_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        source_date=DATE,
        sample_index=0,
        value=35.0,
        unit="stress_level",
        local_timestamp=None,
    )
    fetched_at = datetime(2026, 1, 15, 13, tzinfo=UTC)
    assert samples.upsert_samples(postgres_connection, [sample], fetched_at) == 1
    assert samples.upsert_samples(postgres_connection, [sample], fetched_at) == 1
    rows = postgres_connection.execute("SELECT * FROM samples").fetchall()
    assert len(rows) == 1 and rows[0]["value"] == 35.0

    corrected = sample.__class__(**{**sample.__dict__, "value": 36.0})
    samples.upsert_samples(postgres_connection, [corrected], fetched_at)
    row = postgres_connection.execute("SELECT * FROM samples").fetchone()
    assert row["value"] == 36.0
