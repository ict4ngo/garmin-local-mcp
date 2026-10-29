import os

import pytest


@pytest.fixture(scope="session")
def test_database_url() -> str:
    value = os.environ.get("GARMIN_MCP_TEST_DATABASE_URL")
    if not value:
        pytest.fail("GARMIN_MCP_TEST_DATABASE_URL is required for PostgreSQL tests")
    return value


@pytest.fixture
def postgres_connection(test_database_url, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", test_database_url)
    from garmin_mcp import db

    conn = db.connect(test_database_url)
    conn.execute(
        "TRUNCATE TABLE demo_info, raw_snapshots, sync_state, activities, training_status, "
        "hrv, sleep, daily_wellness RESTART IDENTITY CASCADE"
    )
    try:
        yield conn
    finally:
        conn.close()
