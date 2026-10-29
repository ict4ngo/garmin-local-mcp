import os
from collections.abc import Mapping

import pytest
from psycopg.conninfo import conninfo_to_dict


def _validated_test_database_url(environment: Mapping[str, str]) -> str:
    if "GARMIN_MCP_DATABASE_URL" in environment:
        raise ValueError("GARMIN_MCP_DATABASE_URL must be unset before PostgreSQL tests")

    value = environment.get("GARMIN_MCP_TEST_DATABASE_URL", "").strip()
    if not value:
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL is required for PostgreSQL tests")
    try:
        test_params = conninfo_to_dict(value)
    except Exception as exc:
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL must be a valid PostgreSQL URL") from exc
    database_name = test_params.get("dbname")
    if not database_name or not database_name.endswith("_test"):
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL database name must end in _test")

    return value


@pytest.fixture
def test_database_url() -> str:
    try:
        return _validated_test_database_url(os.environ)
    except ValueError as exc:
        pytest.fail(str(exc))


@pytest.fixture
def postgres_connection(test_database_url, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", test_database_url)
    monkeypatch.setenv("GARMIN_MCP_SYNC_TIMES", "09:00")
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "UTC")
    from garmin_mcp import db

    conn = db.connect(test_database_url)
    conn.execute(
        "TRUNCATE TABLE demo_info, worker_state, backfill_state, activity_detail_state, samples, "
        "raw_snapshots, sync_state, "
        "activities, training_status, "
        "hrv, sleep, daily_wellness RESTART IDENTITY CASCADE"
    )
    try:
        yield conn
    finally:
        conn.close()
